"""
FixGenerationAgent — fetches the affected file from GitHub, uses LLM calls to
generate the fix, then creates a GitHub Issue and PR via the GitHub API.

Flow:
  1. LLM call     → resolve affected file path + function name from incident
  2. GitHub API   → fetch the file
  3. LLM call     → extract old function + generate fixed version
  4. GitHub API   → create Issue
  5. GitHub API   → create branch, commit fix
  5b. LLM call    → generate test, commit to same branch
  6. GitHub API   → open PR

Output (FixResult):
  issue_url, pr_url, pr_number, branch, fix_description, files_changed, test_added
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.agents.base import BaseAgent
from app.core.config import settings
from app.models.events import IncidentState
from app.services.blast_radius import BlastRadiusGuard
from app.services.github import GitHubError, GitHubService
from app.services.llm import HAIKU_MODEL, LLMService
from app.services.session_logger import session_logger

logger = logging.getLogger(__name__)

PR_BASE = "staging"  # all fix PRs target this branch; fix branches are created from its tip


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class FixResult:
    issue_url: str | None
    pr_url: str | None
    pr_number: int | None
    branch: str
    fix_description: str
    files_changed: list[str] = field(default_factory=list)
    test_added: bool = False
    commit_sha: str | None = None
    blast_radius_violation: bool = False
    blast_radius_violations: list[str] = field(default_factory=list)
    target_file: str | None = None
    target_function: str | None = None


# ---------------------------------------------------------------------------
# FixGenerationAgent
# ---------------------------------------------------------------------------

class FixGenerationAgent(BaseAgent):
    """
    Generates and commits an AI fix for a diagnosed incident.

    Uses direct GitHub API calls + focused LLM calls instead of a ReAct loop —
    the steps are deterministic so an agent loop adds no value.

    Usage:
        agent = FixGenerationAgent()
        result = await agent.fix(incident)
        print(result.pr_url)
    """

    def __init__(self, github: GitHubService | None = None) -> None:
        super().__init__(llm=LLMService())
        self._llm_haiku = LLMService(model=HAIKU_MODEL)
        self._github = github or GitHubService()
        self._owner, self._repo = settings.fix_target_repo.split("/", 1)
        self._rag = None
        try:
            from app.services.rag import RAGService
            self._rag = RAGService()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fix(self, incident: IncidentState) -> FixResult:
        """Generate a fix and open a GitHub PR."""
        result, _ = await self.fix_with_steps(incident)
        return result

    async def commit_approved_fix(self, incident: IncidentState) -> tuple[FixResult, list[str]]:
        """
        Commit a previously generated and human-approved diff to GitHub and open a PR.
        Called after the human approves the pending diff via POST /incidents/{id}/approve-fix.
        """
        steps: list[str] = []
        event = incident.error_event

        def _fail(desc: str, issue_url: str | None = None, branch: str = "") -> tuple[FixResult, list[str]]:
            return FixResult(issue_url=issue_url, pr_url=None, pr_number=None, branch=branch,
                             fix_description=desc), steps

        file_path = incident.pending_fix_file
        old_function = incident.pending_fix_old
        new_function = incident.pending_fix_new
        branch_name = incident.pending_fix_branch
        issue_url = incident.pending_fix_issue_url
        issue_number = incident.pending_fix_issue_number
        function_name = incident.pending_fix_function or "the function handling this error"

        if not all([file_path, old_function, new_function, branch_name]):
            return _fail("No pending fix found — generate a fix first")

        try:
            content, file_sha = await self._github.get_file_contents(
                self._owner, self._repo, file_path, ref=PR_BASE
            )
        except GitHubError as exc:
            return _fail(f"Could not fetch {file_path}: {exc}", issue_url, branch_name)

        if old_function in content:
            new_content = content.replace(old_function, new_function, 1)
        elif old_function.strip() in content:
            new_content = content.replace(old_function.strip(), new_function.strip(), 1)
        else:
            return _fail("old_function no longer found verbatim — file may have changed", issue_url, branch_name)

        pr_body = (
            f"## Summary\n"
            f"- **Root cause:** {incident.diagnosis}\n"
            f"- **Fix:** Updated `{function_name}` in `{file_path}`\n"
            f"- **Human approved:** yes\n\n"
            f"{f'Fixes #{issue_number}' if issue_number else ''}\n\n"
            f"**Incident ID:** {incident.id}  \n"
            f"**Agent confidence:** {incident.confidence:.0%}"
        )

        try:
            base_sha = await self._github.get_branch_sha(self._owner, self._repo, PR_BASE)
            await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            steps.append(f"✓ Created branch {branch_name}")

            commit_sha = await self._github.update_file(
                self._owner, self._repo, file_path, new_content,
                f"fix: {function_name} in {file_path.split('/')[-1]}\n\n{f'Fixes #{issue_number}' if issue_number else ''}",
                branch_name, file_sha,
            )
            steps.append(f"✓ Committed fix (sha={commit_sha[:8]})")

            pr_number, pr_url = await self._github.create_pull_request(
                self._owner, self._repo,
                title=f"fix({file_path.split('/')[-1]}): {event.title}",
                body=pr_body,
                head=branch_name,
                base="staging",
                labels=["bug", "ai-generated-fix", "awaiting-review"],
            )
            steps.append(f"✓ Created PR #{pr_number}: {pr_url}")
            logger.info("[FixGen] Approved fix committed — PR #%d: %s", pr_number, pr_url)

        except GitHubError as exc:
            steps.append(f"✗ GitHub error: {exc}")
            return _fail(str(exc), issue_url, branch_name)

        return FixResult(
            issue_url=issue_url,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch_name,
            fix_description=f"Fix applied to {function_name} in {file_path}",
            files_changed=[file_path],
            test_added=False,
            commit_sha=commit_sha,
        ), steps

    async def fix_with_steps(self, incident: IncidentState) -> tuple[FixResult, list[str]]:
        """
        Same as fix() but also returns a list of step strings for debugging.
        Each string describes what happened at that step (✓ success / ✗ failure).
        """
        steps: list[str] = []
        event = incident.error_event
        today = datetime.utcnow().strftime("%Y-%m-%d")
        sev = str(event.severity).split(".")[-1] if event.severity else "P2"

        def _fail(desc: str, issue_url: str | None = None, branch: str = "") -> tuple[FixResult, list[str]]:
            return FixResult(
                issue_url=issue_url,
                pr_url=None,
                pr_number=None,
                branch=branch,
                fix_description=desc,
            ), steps

        # ── 1. Resolve target file + function via LLM ──────────────────
        file_path, function_name = await self._resolve_target(incident)
        if not file_path or not function_name:
            steps.append("✗ Could not resolve target file/function from incident")
            return _fail("Could not determine which file/function to fix")

        steps.append(f"✓ Target: {file_path} → {function_name}")
        logger.info("[FixGen] Target: %s / %s", file_path, function_name)

        branch_name = (
            f"fix/{event.error_type or 'incident'}-{incident.id[:8]}".lower()
            .replace("_", "-")
        )

        # ── 2. Fetch the file from PR_BASE (staging) ───────────────────
        try:
            content, file_sha = await self._github.get_file_contents(
                self._owner, self._repo, file_path, ref=PR_BASE
            )
            steps.append(f"✓ Fetched {file_path} from {PR_BASE} (sha={file_sha[:8]}, {len(content)} chars)")
            logger.info("[FixGen] Fetched %s (%d chars) from %s", file_path, len(content), PR_BASE)
        except GitHubError as exc:
            # 404 — try to find the file elsewhere in the repo by basename
            if "404" in str(exc):
                basename = file_path.rsplit("/", 1)[-1]
                steps.append(f"⚠ {file_path} not found — searching repo for '{basename}'")
                matches = await self._github.find_files_by_name(
                    self._owner, self._repo, basename, ref=PR_BASE
                )
                if matches:
                    file_path = matches[0]
                    steps.append(f"✓ Found at {file_path} — retrying fetch")
                    logger.info("[FixGen] Resolved path via tree search: %s", file_path)
                    try:
                        content, file_sha = await self._github.get_file_contents(
                            self._owner, self._repo, file_path, ref=PR_BASE
                        )
                        steps.append(f"✓ Fetched {file_path} ({len(content)} chars)")
                        test_candidates, default_test_path = self._test_file_candidates(file_path)
                    except GitHubError as exc2:
                        steps.append(f"✗ Retry failed: {exc2}")
                        return _fail(f"Could not fetch {file_path}: {exc2}", branch=branch_name)
                else:
                    steps.append(f"✗ '{basename}' not found anywhere in repo")
                    return _fail(f"File '{basename}' not found in repo", branch=branch_name)
            else:
                steps.append(f"✗ get_file_contents failed: {exc}")
                logger.error("[FixGen] Failed to fetch file: %s", exc)
                return _fail(f"Could not fetch {file_path}: {exc}", branch=branch_name)

        # ── 2b. Fetch call chain — imports + callers ───────────────────
        call_chain = await self._fetch_call_chain(file_path, function_name, content)
        if call_chain:
            steps.append(f"✓ Call chain: {len(call_chain.splitlines())} lines of context fetched")
            logger.info("[FixGen] Call chain context fetched for %s", file_path)

        # ── 3. Generate fix via LLM ────────────────────────────────────
        try:
            old_function, new_function = await self._generate_fix(content, function_name, incident, file_path, call_chain)
        except Exception as exc:
            steps.append(f"✗ LLM fix generation failed: {exc}")
            logger.error("[FixGen] LLM error: %s", exc)
            return _fail(f"LLM error: {exc}", branch=branch_name)

        if not old_function:
            steps.append(f"✗ LLM could not locate {function_name} in the file")
            return _fail(f"{function_name} not found in {file_path}", branch=branch_name)

        steps.append(
            f"✓ Generated fix (old={len(old_function)} chars, new={len(new_function)} chars)"
        )
        logger.info("[FixGen] Generated fix")

        # ── 3b. Blast radius check ────────────────────────────────────
        files_to_touch = [file_path]
        additions = len(new_function.splitlines())
        deletions = len(old_function.splitlines())
        br_result = BlastRadiusGuard().check(files_to_touch, additions=additions, deletions=deletions)
        if not br_result.allowed:
            steps.append(f"✗ Blast radius violated: {br_result.reason}")
            logger.warning("[FixGen] Blast radius violation: %s", br_result.reason)
            return FixResult(
                issue_url=None, pr_url=None, pr_number=None, branch=branch_name,
                fix_description=f"BLAST_RADIUS_VIOLATION: {br_result.reason}",
                blast_radius_violation=True, blast_radius_violations=br_result.violations,
            ), steps
        steps.append(f"✓ Blast radius OK ({len(files_to_touch)} files, +{additions}/-{deletions} lines)")

        # ── 3c. Self-critique — verify fix addresses root cause ────────
        critique = await self._critique_fix(old_function, new_function, incident, file_path)
        steps.append(f"✓ Self-critique: {critique[:120]}")
        logger.info("[FixGen] Self-critique: %s", critique[:200])

        # ── 3c-retry. If critique says LIKELY WRONG, switch to alternate frame ──
        if "LIKELY WRONG" in critique.upper():
            frames = self._parse_stack_frames(incident)
            # Find a frame in a different file — the crash frame we may have skipped
            # or a deeper caller frame we haven't tried
            alt_frame = next(
                ((p, fn) for p, fn in frames if p != file_path), None
            )
            if alt_frame:
                alt_path, alt_fn = alt_frame
                steps.append(f"↻ Critique LIKELY WRONG — retrying with alternate frame: {alt_path}")
                logger.info("[FixGen] Critique rejected — switching to %s → %s", alt_path, alt_fn)
                try:
                    alt_content, _ = await self._github.get_file_contents(
                        self._owner, self._repo, alt_path, ref=PR_BASE
                    )
                    alt_call_chain = await self._fetch_call_chain(
                        alt_path, alt_fn or function_name, alt_content
                    )
                    alt_old, alt_new = await self._generate_fix(
                        alt_content, alt_fn or function_name, incident, alt_path, alt_call_chain,
                        test_failures=(
                            f"PREVIOUS FIX WAS REJECTED — critique said:\n{critique}\n\n"
                            f"Do NOT add null guards at the crash site. Fix the function that "
                            f"produces the undefined value."
                        ),
                    )
                    if alt_old:
                        file_path = alt_path
                        function_name = alt_fn or function_name
                        content = alt_content
                        call_chain = alt_call_chain
                        old_function = alt_old
                        new_function = alt_new
                        critique = await self._critique_fix(old_function, new_function, incident, file_path)
                        steps.append(f"✓ Alt-frame critique: {critique[:120]}")
                    else:
                        steps.append(f"⚠ Alt-frame fix generation failed — proceeding with original")
                except Exception as exc:
                    steps.append(f"⚠ Alt-frame retry failed: {exc} — proceeding with original")
            else:
                steps.append(f"⚠ Critique LIKELY WRONG but no alternate frame available — proceeding")

        # ── 3d. Sandbox validation with retry ─────────────────────────
        from app.services.sandbox import SandboxService
        _MAX_ATTEMPTS = 3
        _sandbox = SandboxService()
        _test_failures = ""
        new_content = ""

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            new_content = content.replace(old_function, new_function, 1)
            if new_content == content:
                new_content = content.replace(old_function.strip(), new_function.strip(), 1)

            sandbox_result = await _sandbox.run({file_path: new_content}, incident.id)
            _sess = session_logger.get(incident.id)
            if _sess:
                _sess.log_sandbox_attempt(attempt, sandbox_result.passed, sandbox_result.output or "")
            if sandbox_result.passed:
                steps.append(f"✓ Sandbox tests passed (attempt {attempt}/{_MAX_ATTEMPTS})")
                logger.info("[FixGen] Sandbox passed on attempt %d for %s", attempt, incident.id)
                break

            _test_failures = self._extract_test_failures(sandbox_result.output)
            reason = sandbox_result.error or "tests failed"
            steps.append(f"✗ Sandbox attempt {attempt}/{_MAX_ATTEMPTS} failed ({reason})\n{_test_failures}")
            logger.warning("[FixGen] Sandbox attempt %d failed for %s", attempt, incident.id)

            if attempt == _MAX_ATTEMPTS:
                return _fail(
                    f"Sandbox tests failed after {_MAX_ATTEMPTS} attempts: {reason}",
                    branch=branch_name,
                )

            steps.append(f"↻ Regenerating fix with test failure context (attempt {attempt + 1}/{_MAX_ATTEMPTS})")
            try:
                old_function, new_function = await self._generate_fix(
                    content, function_name, incident, file_path, call_chain,
                    test_failures=_test_failures,
                )
            except Exception as exc:
                steps.append(f"✗ LLM retry failed: {exc}")
                return _fail(f"LLM retry error: {exc}", branch=branch_name)

            if not old_function:
                return _fail(f"{function_name} not found on retry attempt {attempt + 1}", branch=branch_name)
            steps.append(f"✓ Generated retry fix (attempt {attempt + 1}/{_MAX_ATTEMPTS})")

        # ── 4. Create GitHub Issue ─────────────────────────────────────
        issue_url: str | None = None
        issue_number: int | None = None
        short_diagnosis = (incident.diagnosis or "")[:200]
        issue_body = (
            f"## Summary\n"
            f"- **Error:** `{event.error_type or event.title}`\n"
            f"- **Occurrences:** {incident.occurrences_24h} in last 24 hours\n"
            f"- **Root cause:** {incident.diagnosis}\n"
            f"- **Affected file:** `{file_path}` → `{function_name}`\n"
            f"- **Agent confidence:** {incident.confidence:.0%}\n"
            f"- **Detected:** {today}\n\n"
            f"## Fix approach\n{short_diagnosis}"
        )
        try:
            issue_number, issue_url = await self._github.create_issue(
                self._owner, self._repo,
                title=f"[{sev}] {event.title}",
                body=issue_body,
                labels=["bug", "ai-detected", sev.lower()],
            )
            steps.append(f"✓ Created Issue #{issue_number}: {issue_url}")
            logger.info("[FixGen] Created issue #%d", issue_number)
        except GitHubError as exc:
            steps.append(f"✗ create_issue failed (continuing without issue link): {exc}")
            logger.warning("[FixGen] Issue creation failed: %s", exc)

        # ── 5. Commit fix and open PR ──────────────────────────────────
        # new_content already computed in step 3d

        pr_body = (
            f"## Summary\n"
            f"- **Root cause:** {incident.diagnosis}\n"
            f"- **Fix:** Updated `{function_name}` in `{file_path}`\n"
            f"- **Self-critique:** {critique[:300]}\n\n"
            f"{f'Fixes #{issue_number}' if issue_number else ''}\n\n"
            f"**Incident ID:** {incident.id}  \n"
            f"**Agent confidence:** {incident.confidence:.0%}"
        )

        pr_number: int | None = None
        pr_url: str | None = None
        commit_sha: str | None = None

        try:
            base_sha = await self._github.get_branch_sha(self._owner, self._repo, PR_BASE)
            await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            steps.append(f"✓ Created branch {branch_name}")

            commit_sha = await self._github.update_file(
                self._owner, self._repo, file_path, new_content,
                f"fix: {function_name} in {file_path.split('/')[-1]}\n\n{f'Fixes #{issue_number}' if issue_number else ''}",
                branch_name, file_sha,
            )
            steps.append(f"✓ Committed fix (sha={commit_sha[:8]})")

            pr_number, pr_url = await self._github.create_pull_request(
                self._owner, self._repo,
                title=f"fix({file_path.split('/')[-1]}): {event.title}",
                body=pr_body,
                head=branch_name,
                base=PR_BASE,
                labels=["bug", "ai-generated-fix", "awaiting-review"],
            )
            steps.append(f"✓ Created PR #{pr_number}: {pr_url}")
            logger.info("[FixGen] PR #%d created: %s", pr_number, pr_url)

        except GitHubError as exc:
            steps.append(f"✗ GitHub error: {exc}")
            return _fail(str(exc), issue_url, branch_name)

        return FixResult(
            issue_url=issue_url,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch_name,
            fix_description=f"Fix applied to {function_name} in {file_path}",
            files_changed=[file_path],
            test_added=False,
            commit_sha=commit_sha,
            target_file=file_path,
            target_function=function_name,
        ), steps

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _parse_stack_frames(self, incident: IncidentState) -> list[tuple[str, str | None]]:
        """
        Extract ALL relevant stack frames from the error description/diagnosis.
        Returns list of (repo_relative_path, function_name) in stack order (crash site first).
        Skips internal Node frames and node_modules.
        """
        text = f"{incident.error_event.description or ''}\n{incident.diagnosis or ''}"

        frame_re = re.compile(
            r"at\s+"
            r"(?:([\w.<>$]+(?:\.[\w.<>$]+)*)\s+\()?"
            r"([^\s()]+\.(?:js|ts|jsx|tsx|py|rb|go))"
            r":\d+(?::\d+)?\)?",
            re.MULTILINE,
        )

        _SKIP = ("node:internal", "node_modules", "internal/process", "<anonymous>",
                 "node:events", "node:stream", "timers")
        _CONTAINER_PREFIXES = ("/app/", "/usr/src/app/", "/home/app/", "/srv/app/")

        frames: list[tuple[str, str | None]] = []
        seen: set[str] = set()

        for m in frame_re.finditer(text):
            fn_name = m.group(1)
            raw_path = m.group(2).strip()

            if any(s in raw_path for s in _SKIP):
                continue

            for prefix in _CONTAINER_PREFIXES:
                if raw_path.startswith(prefix):
                    raw_path = raw_path[len(prefix):]
                    break

            if raw_path.startswith("/"):
                continue

            # Deduplicate by path so we don't return the same file twice
            if raw_path not in seen:
                seen.add(raw_path)
                frames.append((raw_path, fn_name))

        return frames

    @staticmethod
    def _is_null_access_error(incident: IncidentState) -> bool:
        """Return True if the error is a null/undefined property access."""
        text = (
            f"{incident.error_event.error_type or ''} "
            f"{incident.error_event.description or ''}"
        ).lower()
        return any(p in text for p in [
            "cannot read properties of undefined",
            "cannot read properties of null",
            "cannot read property",
            "typeerror: undefined",
            "typeerror: null",
            "is not defined",
            " is undefined",
            " is null",
            "nullpointerexception",
            "attributeerror: 'nonetype'",
        ])

    async def _resolve_function_name(self, file_path: str, snippet: str, incident: IncidentState) -> str:
        """Given a confirmed file path and a matched code snippet, ask the LLM which function to fix."""
        prompt = (
            f"A production incident occurred in '{file_path}'.\n\n"
            f"Error type: {incident.error_event.error_type or 'unknown'}\n"
            f"Diagnosis: {incident.diagnosis or 'none'}\n"
            f"Matched code snippet from the file:\n{snippet}\n\n"
            f"What is the name of the function that should be fixed? "
            f"Return ONLY the function name, nothing else."
        )
        try:
            fn = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._with_harness("Return only the function name as a single word. No explanation."),
            )
            return fn.strip().split()[0]
        except Exception:
            return "the function handling this error"

    def _extract_keywords(self, incident: IncidentState) -> list[str]:
        """Extract searchable keyword terms from incident title, error type, and diagnosis."""
        raw = f"{incident.error_event.title} {incident.error_event.error_type or ''} {incident.diagnosis or ''}"
        tokens = re.split(r"[\s\-_./\\|:,()]+", raw)
        keywords: list[str] = []
        seen: set[str] = set()
        for t in tokens:
            # Split camelCase and PascalCase
            split1 = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
            # Split sequences of caps followed by a cap+lower: "TOKENEXPIREDERROR" → best-effort word boundaries
            split2 = re.sub(r"([A-Z]{2,})([A-Z][a-z])", r"\1 \2", split1)
            for p in split2.split():
                lp = p.lower()
                if len(lp) > 3 and lp not in seen:
                    seen.add(lp)
                    keywords.append(lp)
        return keywords[:10]

    async def _resolve_target(self, incident: IncidentState) -> tuple[str | None, str | None]:
        """
        Resolve the file and function to fix.

        Strategy (in order):
          0. Diagnosis result — DiagnosisAgent already identified the producer; use it
             for null/undefined errors where the crash site differs from root cause
          1. Stack trace parsing — exact path from log, no LLM needed
          2. Function name from error context + GitHub code search for the definition
        """
        # ── 0. Diagnosis-identified producer (null/undefined errors) ───
        # The diagnosis agent already traced the undefined value back to its producer.
        # Use that result directly instead of going to the crash frame from the stack trace.
        if (
            self._is_null_access_error(incident)
            and incident.diagnosis_affected_file
            and incident.diagnosis_affected_function
        ):
            diag_file = incident.diagnosis_affected_file.lstrip("/")
            diag_fn = incident.diagnosis_affected_function
            try:
                await self._github.get_file_contents(self._owner, self._repo, diag_file, ref=PR_BASE)
                logger.info("[FixGen] Using diagnosis target: %s → %s", diag_file, diag_fn)
                return diag_file, diag_fn
            except Exception:
                logger.info("[FixGen] Diagnosis target %s not fetchable — falling back to stack trace", diag_file)

        # ── 1. Stack trace (fastest, most accurate) ────────────────────
        frames = self._parse_stack_frames(incident)
        if frames:
            is_null = self._is_null_access_error(incident)
            # For null/undefined errors the crash frame is the symptom site.
            # Try caller frames first — that's where the null originates.
            # Fall back to the crash frame only if no caller frame exists in the repo.
            ordered = frames[1:] + frames[:1] if is_null and len(frames) > 1 else frames
            for raw_path, fn_name in ordered:
                try:
                    await self._github.get_file_contents(self._owner, self._repo, raw_path, ref=PR_BASE)
                    if is_null and raw_path == frames[0][0]:
                        logger.info("[FixGen] Null error — only crash frame found in repo: %s", raw_path)
                    else:
                        logger.info("[FixGen] Stack trace resolved: %s → %s", raw_path, fn_name)
                    return raw_path, fn_name or "the function handling this error"
                except Exception:
                    continue
            logger.info("[FixGen] No stack frame path found in repo — falling back")

        # ── 2. Function name from error context + code search ──────────
        # AWS SDK and similar library errors throw from inside the library, so the
        # application function name never appears in the stack trace. It does appear
        # in the error message ("NoSuchKey in moveAndRemoveFileFromS3") and in the
        # diagnosis. Extract it and search GitHub for the file that defines it.
        fn_name = self._extract_function_name_from_error(incident)
        if fn_name:
            logger.info("[FixGen] No stack trace — trying code search for function '%s'", fn_name)
            try:
                matches = await self._github.search_code(self._owner, self._repo, fn_name)
                _SKIP = ("node_modules", ".test.", ".spec.", "dist/", "build/", "vendor/", "min.js")
                candidates = [r["path"] for r in matches if not any(s in r["path"] for s in _SKIP)]
                for path in candidates[:5]:
                    try:
                        content, _ = await self._github.get_file_contents(
                            self._owner, self._repo, path, ref=PR_BASE
                        )
                        # Verify this file actually defines the function (not just calls it)
                        if self._extract_js_function(content, fn_name):
                            logger.info("[FixGen] Code search resolved: %s → %s", fn_name, path)
                            return path, fn_name
                    except Exception:
                        pass
            except Exception as exc:
                logger.debug("[FixGen] Code search for '%s' failed: %s", fn_name, exc)

        logger.info("[FixGen] Could not resolve target file/function for incident %s", incident.id)
        return None, None

    def _extract_function_name_from_error(self, incident: IncidentState) -> str | None:
        """
        Extract an application function name from the error title, description, or diagnosis.

        Handles patterns like:
          "NoSuchKey in moveAndRemoveFileFromS3"       → moveAndRemoveFileFromS3
          "moveAndRemoveFileFromS3 error NoSuchKey"    → moveAndRemoveFileFromS3
          diagnosis: "moveAndRemoveFileFromS3 function is attempting to..."
        """
        text = " ".join(filter(None, [
            incident.error_event.title or "",
            incident.error_event.description or "",
            incident.diagnosis or "",
        ]))
        patterns = [
            r'\bin\s+([a-z][a-zA-Z0-9]{4,})',           # "error in functionName"
            r'([a-z][a-zA-Z0-9]{4,})\s+(?:error|failed|threw|function\b)',  # "functionName error"
            r'\bat\s+([a-z][a-zA-Z0-9]{4,})\s*\(',      # "at functionName("
        ]
        seen: set[str] = set()
        _COMMON = {"error", "function", "failed", "undefined", "cannot", "object", "request"}
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                name = m.group(1)
                if name.lower() not in _COMMON and name not in seen and len(name) > 4:
                    seen.add(name)
                    return name
        return None

    def _test_file_candidates(self, file_path: str) -> tuple[list[str], str]:
        """Derive test file path candidates from the source file path."""
        parts = file_path.replace("\\", "/").split("/")
        filename = parts[-1]
        stem, ext = (filename.rsplit(".", 1) + ["js"])[:2]
        subdir = "/".join(parts[1:-1])
        base = f"{stem}.test.{ext}"
        candidates = [
            f"tests/{subdir}/{base}" if subdir else f"tests/{base}",
            f"__tests__/{subdir}/{base}" if subdir else f"__tests__/{base}",
            f"test/{subdir}/{base}" if subdir else f"test/{base}",
        ]
        return candidates, candidates[0]

    # ------------------------------------------------------------------
    # Test generation
    # ------------------------------------------------------------------

    async def _commit_test(
        self,
        branch_name: str,
        new_function: str,
        incident: IncidentState,
        test_candidates: list[str],
        default_test_path: str,
        steps: list[str],
    ) -> str | None:
        """Generate a test for the fixed function and commit it to branch_name.
        Returns the committed test file path, or None on failure."""
        try:
            test_code = await self._generate_test(new_function, incident)
        except Exception as exc:
            steps.append(f"⚠ Test generation (LLM) failed: {exc}")
            logger.warning("[FixGen] Test LLM error: %s", exc)
            return None

        test_path: str | None = None
        test_sha: str | None = None
        for candidate in test_candidates:
            try:
                _, existing_sha = await self._github.get_file_contents(
                    self._owner, self._repo, candidate, ref=branch_name
                )
                test_path = candidate
                test_sha = existing_sha
                steps.append(f"✓ Found existing test file {candidate}")
                break
            except GitHubError:
                pass

        if test_path is None:
            test_path = default_test_path
            steps.append(f"✓ Creating new test file {test_path}")

        try:
            test_commit_sha = await self._github.update_file(
                self._owner, self._repo, test_path, test_code,
                f"test: add tests for fix in {test_path.split('/')[-1]}",
                branch_name, test_sha,
            )
            steps.append(f"✓ Committed test file (sha={test_commit_sha[:8]})")
            logger.info("[FixGen] Committed test at %s", test_path)
            return test_path
        except GitHubError as exc:
            steps.append(f"⚠ Test commit failed (continuing): {exc}")
            logger.warning("[FixGen] Test commit error: %s", exc)
            return None

    async def _generate_test(self, new_function: str, incident: IncidentState) -> str:
        """Use a single LLM call to generate a test for the fixed function."""
        prompt = (
            f"Generate a minimal unit test for this fixed function.\n\n"
            f"FUNCTION:\n{new_function}\n\n"
            f"The fix addresses: {incident.diagnosis}\n\n"
            f"Requirements:\n"
            f"- Test that the fix works correctly for the error case\n"
            f"- Test that unrelated errors still propagate normally\n"
            f"- Use appropriate mocking for external dependencies\n"
            f"- Keep it minimal — only the essential test cases\n\n"
            f"Return ONLY the complete test file — no explanation, no markdown fences."
        )

        test_code = await self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=self._with_harness(
                "You are an engineer writing unit tests. "
                "Return ONLY the complete test file code, nothing else. "
                "No markdown, no backticks, no explanation."
            ),
        )

        test_code = re.sub(r"^```(?:javascript|js|python|py|ts)?\n?", "", test_code.strip())
        test_code = re.sub(r"\n?```$", "", test_code)
        return test_code.strip()

    # ------------------------------------------------------------------
    # Fix generation
    # ------------------------------------------------------------------

    def _extract_js_function(self, content: str, function_name: str) -> str:
        """
        Extract a complete function from source using brace counting.
        Handles async/regular functions and arrow functions assigned to const.
        Returns the exact text as it appears in the file, or "" if not found.
        """
        patterns = [
            rf'async\s+function\s+{re.escape(function_name)}\s*\(',
            rf'function\s+{re.escape(function_name)}\s*\(',
            rf'const\s+{re.escape(function_name)}\s*=\s*async\s*(?:function\s*)?\(',
            rf'const\s+{re.escape(function_name)}\s*=\s*function\s*\(',
        ]
        start_pos = -1
        for pattern in patterns:
            m = re.search(pattern, content)
            if m:
                start_pos = m.start()
                break

        if start_pos == -1:
            return ""

        brace_start = content.find("{", start_pos)
        if brace_start == -1:
            return ""

        depth = 0
        in_string = False
        string_char = ""
        escape_next = False

        for i in range(brace_start, len(content)):
            c = content[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if in_string:
                if c == string_char:
                    in_string = False
                continue
            if c in ('"', "'", "`"):
                in_string = True
                string_char = c
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return content[start_pos : i + 1]

        return ""

    async def _fetch_call_chain(self, file_path: str, function_name: str, content: str) -> str:
        """
        Fetch context from files that are imported by the target file and files that call
        the target function. Returns a formatted string to inject before the fix prompt.

        Two sources:
        1. Local imports parsed from the file — what the broken function depends on
        2. Callers found via GitHub code search — what passes data into the broken function
        """
        sections: list[str] = []

        # ── 1. Parse local imports from the file ──────────────────────
        import_paths = self._parse_local_imports(file_path, content)
        fetched_imports = 0
        for imp_path in import_paths[:4]:
            try:
                imp_content, _ = await self._github.get_file_contents(
                    self._owner, self._repo, imp_path, ref=PR_BASE
                )
                # Cap each imported file to 1200 chars — enough for config/setup context
                sections.append(f"--- IMPORT: {imp_path} ---\n{imp_content[:1200]}")
                fetched_imports += 1
                logger.debug("[FixGen] Call chain: fetched import %s", imp_path)
            except Exception:
                pass

        # ── 2. Find callers via code search ───────────────────────────
        try:
            caller_results = await self._github.search_code(
                self._owner, self._repo, function_name
            )
            _SKIP = (file_path, "node_modules", ".test.", ".spec.", "dist/", "build/")
            for result in caller_results[:3]:
                path = result.get("path", "")
                if any(s in path for s in _SKIP):
                    continue
                try:
                    caller_content, _ = await self._github.get_file_contents(
                        self._owner, self._repo, path, ref=PR_BASE
                    )
                    sections.append(f"--- CALLER: {path} ---\n{caller_content[:1200]}")
                    logger.debug("[FixGen] Call chain: fetched caller %s", path)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[FixGen] Call chain search failed: %s", exc)

        return "\n\n".join(sections)

    def _parse_local_imports(self, file_path: str, content: str) -> list[str]:
        """
        Parse local (relative) import paths from JS/TS/Python file content and resolve
        them to repo-relative paths.
        """
        file_dir = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        ext = "." + file_path.rsplit(".", 1)[-1] if "." in file_path else ".js"
        raw_paths: list[str] = []

        # JS/TS: import X from './y'  |  const X = require('./y')
        for m in re.finditer(r"""(?:import\s+.*?\s+from\s+|require\s*\(\s*)['"]([^'"]+)['"]""", content):
            raw_paths.append(m.group(1))

        # Python: from .module import X  |  from ../module import X
        for m in re.finditer(r"""from\s+['"]?(\.[^'";\s]+)['"]?\s+import""", content):
            raw_paths.append(m.group(1))

        resolved: list[str] = []
        for raw in raw_paths:
            if not raw.startswith("."):
                continue  # skip node_modules / stdlib
            # Resolve relative to file_dir
            parts = (file_dir + "/" + raw).split("/")
            norm: list[str] = []
            for p in parts:
                if p == "..":
                    if norm:
                        norm.pop()
                elif p and p != ".":
                    norm.append(p)
            resolved_path = "/".join(norm)
            # Add extension if missing
            if "." not in resolved_path.rsplit("/", 1)[-1]:
                resolved_path += ext
            resolved.append(resolved_path)

        return resolved

    async def _generate_fix(
        self, content: str, function_name: str, incident: IncidentState,
        file_path: str = "", call_chain: str = "", test_failures: str = ""
    ) -> tuple[str, str]:
        """
        Ask the LLM to locate the function in the file and return (old_text, fixed_text).

        Passes the full file content + RAG context so the LLM can reason about callers
        and dependencies, not just the crash site.
        Returns ("", "") if the function cannot be located.
        """
        human_notes_section = ""
        if incident.human_notes:
            human_notes_section = (
                f"\nHUMAN FEEDBACK (from previous fix attempt — you MUST follow this):\n"
                f"{incident.human_notes}\n"
            )

        test_failures_section = ""
        if test_failures:
            test_failures_section = (
                f"\nTEST FAILURES FROM PREVIOUS FIX ATTEMPT — your new fix must not break these tests:\n"
                f"{test_failures}\n"
                f"Study the failures above. Understand which invariant your previous fix violated "
                f"before writing the new version.\n"
            )

        rag_section = ""
        if self._rag is not None and file_path:
            try:
                if self._rag._collection.count() > 0:
                    query = f"{file_path} {function_name} {incident.diagnosis or ''}"
                    chunks = await self._rag.search(query, n_results=6)
                    chunks = [c for c in chunks if c.file_path != file_path][:4]
                    if chunks:
                        lines = ["\nRELATED CODEBASE CONTEXT (callers, dependencies, related modules):"]
                        for c in chunks:
                            lines.append(f"\n--- {c.file_path} (lines {c.start_line}–{c.end_line}) ---")
                            lines.append(c.content[:500])
                        rag_section = "\n".join(lines)
            except Exception as exc:
                logger.debug("[FixGen] RAG context skipped: %s", exc)

        call_chain_section = f"\nCALL CHAIN CONTEXT (files that import or call this function — read before writing the fix):\n{call_chain}\n" if call_chain else ""

        prompt = (
            f"You are fixing a production bug. Study the root cause carefully before writing any code.\n\n"
            f"ROOT CAUSE: {incident.diagnosis}\n"
            f"ERROR TYPE: {incident.error_event.error_type or 'unknown'}\n"
            f"ERROR DETAIL: {incident.error_event.description or ''}\n"
            f"{human_notes_section}"
            f"{test_failures_section}"
            f"{call_chain_section}"
            f"{rag_section}\n\n"
            f"FILE TO FIX ({file_path}):\n{content}\n\n"
            f"RULES — read these before writing the fix:\n"
            f"1. Fix the ROOT CAUSE, not the symptom. Ask yourself: 'Am I eliminating the\n"
            f"   reason this error occurs, or just catching/converting/hiding it?'\n"
            f"   - BAD: wrapping JSON.parse in try/catch, adding input sanitization after the fact,\n"
            f"     silencing exceptions, converting invalid values instead of rejecting them.\n"
            f"   - GOOD: fixing the upstream source (e.g. API call config, schema validation,\n"
            f"     correct algorithm, proper error propagation).\n"
            f"2. NULL / UNDEFINED GUARD RULE — this is the most common symptom-fix mistake:\n"
            f"   If the error is 'Cannot read properties of undefined/null' or a NullPointerException\n"
            f"   at line N, DO NOT add a null check, optional chaining (?.), nullish coalescing (??),\n"
            f"   or try/catch at line N. That only hides the problem.\n"
            f"   Instead: find the function that PRODUCES the undefined/null value and fix it to\n"
            f"   always return the expected structure. If it is an external API/LLM call, add\n"
            f"   response validation or a safe default in the function that makes the call.\n"
            f"3. If the related context above shows the real fix belongs in a different layer\n"
            f"   (e.g. the API call should use structured output, or validation belongs at ingestion),\n"
            f"   fix it at that layer within the target function — do not patch the crash site.\n"
            f"4. The fix must handle ALL invalid inputs, not just the specific value that triggered this error.\n"
            f"5. Do not add logging, comments, or unrelated cleanup.\n\n"
            f"Find the function '{function_name}' (or the closest handler for this error) and fix it.\n"
            f"Return your answer using EXACTLY these delimiters — do NOT use JSON or markdown:\n\n"
            f"<OLD>\n"
            f"exact verbatim text of the function as it appears in the file\n"
            f"</OLD>\n"
            f"<NEW>\n"
            f"complete fixed version\n"
            f"</NEW>\n\n"
            f"The text inside <OLD> must be copy-pasted exactly — no changes to whitespace or quotes."
        )

        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._with_harness("You are a senior software engineer who always fixes root causes, never symptoms. Use only <OLD> and <NEW> delimiters as instructed."),
            )
            old_match = re.search(r"<OLD>\s*(.*?)\s*</OLD>", response, re.DOTALL)
            new_match = re.search(r"<NEW>\s*(.*?)\s*</NEW>", response, re.DOTALL)
            old_function = old_match.group(1) if old_match else ""
            new_function = new_match.group(1) if new_match else ""
        except Exception as exc:
            logger.error("[FixGen] LLM fix generation failed: %s", exc)
            return "", ""

        if not old_function:
            logger.error(
                "[FixGen] LLM returned empty 'old' function for %s — raw response (first 500 chars): %s",
                function_name, response[:500] if "response" in dir() else "no response",
            )
            return "", ""

        # Verify old_function actually appears in the file before returning
        if old_function not in content and old_function.strip() not in content:
            logger.error("[FixGen] LLM-returned 'old' text not found verbatim in file — likely hallucinated")
            return "", ""

        logger.info("[FixGen] Generated fix (old=%d chars, new=%d chars)", len(old_function), len(new_function))
        return old_function, new_function.strip()

    def _extract_test_failures(self, output: str) -> str:
        """Extract the meaningful lines from jest test output for LLM context."""
        lines = output.splitlines()
        keep = []
        for line in lines:
            s = line.strip()
            if any(s.startswith(k) for k in (
                "FAIL ", "● ", "expect(", "Expected", "Received", "Error:", "TypeError",
                "Test Suites:", "Tests:",
            )):
                keep.append(line)
        # Fall back to tail if we extracted too little
        result = keep if len(keep) >= 5 else lines[-60:]
        return "\n".join(result[:80])

    async def _critique_fix(
        self, old_code: str, new_code: str, incident: IncidentState, file_path: str = ""
    ) -> str:
        """
        Self-critique pass (Haiku) with RAG context. Asks: does the fix address the
        root cause or just suppress the symptom? Returns a short plain-text assessment.
        """
        rag_section = ""
        if self._rag is not None and file_path:
            try:
                if self._rag._collection.count() > 0:
                    # Search for callers and related code — crucial for catching symptom fixes
                    query = f"{file_path} {incident.diagnosis or ''}"
                    chunks = await self._rag.search(query, n_results=5)
                    chunks = [c for c in chunks if c.file_path != file_path][:4]
                    if chunks:
                        lines = ["\nRELATED CODEBASE CONTEXT (callers, dependencies):"]
                        for c in chunks:
                            lines.append(f"\n--- {c.file_path} (lines {c.start_line}–{c.end_line}) ---")
                            lines.append(c.content[:400])
                        rag_section = "\n".join(lines)
            except Exception as exc:
                logger.debug("[FixGen] Critique RAG skipped: %s", exc)

        prompt = (
            f"A production fix was generated. Assess whether it correctly addresses the root cause.\n\n"
            f"ROOT CAUSE: {incident.diagnosis}\n"
            f"ERROR: {incident.error_event.description or incident.error_event.title}\n\n"
            f"OLD CODE:\n{old_code[:800]}\n\n"
            f"NEW CODE:\n{new_code[:800]}\n"
            f"{rag_section}\n\n"
            f"SYMPTOM-FIX CHECKLIST — flag immediately if the new code:\n"
            f"- Adds a null check, optional chaining (?.), nullish coalescing (??), or try/catch\n"
            f"  at or near the crash line without fixing the function that produces the null value\n"
            f"- Adds `if (x && x.y)` or `x?.y ?? default` where `x` comes from an external call\n"
            f"  (LLM, API, DB) — the fix belongs in the function that makes that call\n"
            f"- Converts an invalid value instead of preventing it from being invalid in the first place\n\n"
            f"Answer in 2-3 sentences:\n"
            f"1. Does the fix address the root cause, or does it just suppress/convert the error?\n"
            f"   If related context above shows the real fix should be upstream (e.g. API call config,\n"
            f"   input validation at source), flag it as a symptom fix.\n"
            f"2. What edge cases or risks does the fix introduce?\n"
            f"3. Verdict: LOOKS CORRECT / NEEDS REVIEW / LIKELY WRONG"
        )
        try:
            return await self._llm_haiku.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._with_harness("You are a skeptical senior engineer reviewing an AI-generated fix. Be concise and critical."),
            )
        except Exception as exc:
            logger.warning("[FixGen] Critique failed: %s", exc)
            return "Critique unavailable"
