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
from app.services.ipi_guard import scan_for_injection, wrap_untrusted
from app.services.llm import HAIKU_MODEL, LLMService
from app.services.repo import LocalRepoService
from app.services.session_logger import session_logger

logger = logging.getLogger(__name__)

# Module-level call graph singleton — loaded from Postgres on first import.
# Agents query this directly via the find_callers tool; no disk I/O at query time.
try:
    from app.services.code_graph.graph import CodeGraph
    _code_graph = CodeGraph.load_from_store()
except Exception as _cg_err:
    from app.services.code_graph.graph import CodeGraph
    _code_graph = CodeGraph()
    logger.warning("[FixGen] Call graph unavailable: %s", _cg_err)

PR_BASE = "staging"  # all fix PRs target this branch; fix branches are created from its tip

# Upper bound on how many additional files _resolve_secondary_targets() will commit
# fixes to in one PR. Bounds cost/blast-radius when a diagnosis's blast_radius list
# is large — this codebase's brand fan-out tops out around 8 sibling files today, so
# 10 gives headroom without being unbounded.
_MAX_SECONDARY_FIXES = 10


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


def _prune_tool_results(
    messages: list[dict],
    keep_last: int = 2,
    threshold: int = 500,
) -> tuple[list[dict], int]:
    """Truncate large tool results that are no longer recent.

    Keeps the last `keep_last` tool messages intact so the model retains
    recent context. Earlier tool results longer than `threshold` chars are
    replaced with a short stub — the model can re-call read_file if it
    needs the content again.

    Returns (pruned_messages, chars_removed).
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    prune_set = set(tool_indices[:-keep_last]) if len(tool_indices) > keep_last else set()

    pruned_messages: list[dict] = []
    chars_removed = 0
    for i, msg in enumerate(messages):
        if i in prune_set:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > threshold:
                chars_removed += len(content) - threshold
                msg = {**msg, "content": f"[pruned — {len(content)} chars — re-call read_file if needed]"}
        pruned_messages.append(msg)
    return pruned_messages, chars_removed


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
    # Self-assessed verdict from the fix agent. Mirrors the diagnosis
    # confidence/escalate pattern so a shaky fix opens a human approval gate
    # instead of a PR. confidence < 0.70 OR escalate=True triggers the
    # human-in-the-loop escalation path in IncidentLoop.
    confidence: float | None = None       # 0.0–1.0, None if the model didn't emit one
    escalate: bool = False
    escalate_reason: str | None = None
    blast_radius_addressed: bool | None = None


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
        self._local_repo = LocalRepoService(self._owner, self._repo)
        self._rag = None
        try:
            from app.services.rag import RAGService
            self._rag = RAGService()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Local repo helpers
    # ------------------------------------------------------------------

    async def _ensure_local_repo(self) -> None:
        try:
            await self._local_repo.ensure_fresh()
            logger.info("[FixGen] Local repo ready (%d files)", len(self._local_repo.list_files()))
        except Exception as exc:
            logger.warning("[FixGen] Local repo unavailable — falling back to GitHub API for reads: %s", exc)

    async def _read_file(self, path: str, ref: str = PR_BASE) -> tuple[str, str]:
        """Return (content, sha). Content from local clone; SHA from API (for writes).

        SHA is fetched right before it's needed, minimising the stale-SHA window.
        """
        if self._local_repo.ready and self._local_repo.file_exists(path):
            try:
                content = self._local_repo.read_file(path)
                _, sha = await self._github.get_file_contents(self._owner, self._repo, path, ref=ref)
                return content, sha
            except Exception:
                pass
        # Fallback: full API fetch
        return await self._github.get_file_contents(self._owner, self._repo, path, ref=ref)

    async def _path_exists_in_repo(self, path: str) -> bool:
        """Check file existence — local clone first, GitHub API as fallback."""
        if self._local_repo.ready:
            return self._local_repo.file_exists(path)
        try:
            await self._github.get_file_contents(self._owner, self._repo, path, ref=PR_BASE)
            return True
        except Exception:
            return False

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

        await self._ensure_local_repo()

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

        # ── 2. Fetch the file ─────────────────────────────────────────
        # If the review said "fix is correct, extend it", human_notes carries
        # a BASE BRANCH hint pointing to the PR branch with the fix already applied.
        # Use that branch so the LLM builds on the fixed code, not the pre-fix staging.
        fetch_ref = PR_BASE
        base_branch_hint = re.search(
            r"\[BASE BRANCH FOR THIS FIX:\s*([^\]]+)\]",
            incident.human_notes or "",
        )
        if base_branch_hint:
            fetch_ref = base_branch_hint.group(1).strip()
            steps.append(f"✓ Review intent=EXTEND — fetching from PR branch {fetch_ref}")
            logger.info("[FixGen] EXTEND mode: fetching from PR branch %s", fetch_ref)

        try:
            content, file_sha = await self._read_file(file_path, ref=fetch_ref)
            steps.append(f"✓ Fetched {file_path} from {fetch_ref} (sha={file_sha[:8]}, {len(content)} chars)")
            logger.info("[FixGen] Fetched %s (%d chars) from %s", file_path, len(content), fetch_ref)
        except (GitHubError, Exception) as exc:
            # 404 — try to find the file elsewhere in the repo by basename
            if "404" in str(exc):
                basename = file_path.rsplit("/", 1)[-1]
                steps.append(f"⚠ {file_path} not found — searching repo for '{basename}'")
                # Use local clone listing if available, else fall back to GitHub tree search
                if self._local_repo.ready:
                    all_paths = self._local_repo.list_files()
                    matches = [p for p in all_paths if p.rsplit("/", 1)[-1] == basename]
                else:
                    matches = await self._github.find_files_by_name(
                        self._owner, self._repo, basename, ref=PR_BASE
                    )
                if matches:
                    file_path = matches[0]
                    steps.append(f"✓ Found at {file_path} — retrying fetch")
                    logger.info("[FixGen] Resolved path via tree search: %s", file_path)
                    try:
                        content, file_sha = await self._read_file(file_path, ref=PR_BASE)
                        steps.append(f"✓ Fetched {file_path} ({len(content)} chars)")
                        test_candidates, default_test_path = self._test_file_candidates(file_path)
                    except Exception as exc2:
                        steps.append(f"✗ Retry failed: {exc2}")
                        return _fail(f"Could not fetch {file_path}: {exc2}", branch=branch_name)
                else:
                    steps.append(f"✗ '{basename}' not found anywhere in repo")
                    return _fail(f"File '{basename}' not found in repo", branch=branch_name)
            else:
                steps.append(f"✗ get_file_contents failed: {exc}")
                logger.error("[FixGen] Failed to fetch file: %s", exc)
                return _fail(f"Could not fetch {file_path}: {exc}", branch=branch_name)

        # ── 2b. Fetch context — imports (Tier 3), callers + tests + types (Tier 2) ──
        context_bundle = await self._fetch_call_chain(file_path, function_name, content)
        n_items = sum(len(v) for v in context_bundle.values())
        if n_items:
            steps.append(
                f"✓ Context fetched: {len(context_bundle['callers'])} caller(s), "
                f"{len(context_bundle['tests'])} test(s), "
                f"{len(context_bundle['imports'])} import(s)"
            )
            logger.info("[FixGen] Context bundle for %s: %d items", file_path, n_items)

        # ── 2c. For null/undefined errors, trace back to the source function ──
        # The current file may be the consumer of the undefined value, not the producer.
        # Find the function that RETURNS the undefined object and fix it instead.
        if self._is_null_access_error(incident):
            src = await self._trace_undefined_source(content, file_path, incident)
            if src:
                src_path, src_fn, src_content = src
                steps.append(f"✓ Source trace: undefined value produced by '{src_fn}' in {src_path} — retargeting")
                logger.info("[FixGen] Retargeting fix to source: %s → %s", src_path, src_fn)
                # Promote the original consumer file into Tier 2 callers — it's the
                # function that uses the (now-broken) return value.
                consumer_label = f"{file_path} :: {function_name} (uses return value of {src_fn})"
                context_bundle["callers"].append((consumer_label, content[:2000]))
                file_path = src_path
                function_name = src_fn
                content = src_content

        # ── 3. Generate fix via LLM ────────────────────────────────────
        try:
            old_function, new_function, patches, fix_verdict = await self._generate_fix(
                content, function_name, incident, file_path, context_bundle,
            )
        except Exception as exc:
            steps.append(f"✗ LLM fix generation failed: {exc}")
            logger.error("[FixGen] LLM error: %s", exc)
            return _fail(f"LLM error: {exc}", branch=branch_name)

        if not old_function and not patches:
            steps.append(f"✗ LLM could not locate {function_name} in the file")
            return _fail(f"{function_name} not found in {file_path}", branch=branch_name)

        steps.append(
            f"✓ Generated fix (old={len(old_function)} chars, new={len(new_function)} chars"
            + (f", +{len(patches)} patch_line edits" if patches else "") + ")"
        )
        logger.info("[FixGen] Generated fix")

        # ── 3b. Blast radius check ────────────────────────────────────
        files_to_touch = [file_path]
        additions = len(new_function.splitlines()) + sum(len(ns.splitlines()) for _, ns in patches)
        deletions = len(old_function.splitlines()) + sum(len(os_.splitlines()) for os_, _ in patches)
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
        critique_old, critique_new = self._critique_content(old_function, new_function, patches)
        critique = await self._critique_fix(critique_old, critique_new, incident, file_path)
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
                    alt_content, _ = await self._read_file(alt_path, ref=PR_BASE)
                    alt_context_bundle = await self._fetch_call_chain(
                        alt_path, alt_fn or function_name, alt_content
                    )
                    alt_old, alt_new, alt_patches, alt_verdict = await self._generate_fix(
                        alt_content, alt_fn or function_name, incident, alt_path, alt_context_bundle,
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
                        context_bundle = alt_context_bundle
                        old_function = alt_old
                        new_function = alt_new
                        patches = alt_patches
                        fix_verdict = alt_verdict
                        critique_old, critique_new = self._critique_content(old_function, new_function, patches)
                        critique = await self._critique_fix(critique_old, critique_new, incident, file_path)
                        steps.append(f"✓ Alt-frame critique: {critique[:120]}")
                    else:
                        steps.append("⚠ Alt-frame fix generation failed — proceeding with original")
                except Exception as exc:
                    steps.append(f"⚠ Alt-frame retry failed: {exc} — proceeding with original")
            else:
                steps.append("⚠ Critique LIKELY WRONG but no alternate frame available — proceeding")

        # ── 3d. Sandbox validation with retry ─────────────────────────
        from app.services.sandbox import SandboxService
        _MAX_ATTEMPTS = 3
        _sandbox = SandboxService()
        _test_failures = ""
        new_content = ""

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            new_content = self._apply_all_edits(content, old_function, new_function, patches)

            sandbox_result = await _sandbox.run({file_path: new_content}, incident.id)
            _sess = session_logger.get(incident.id)
            if _sess:
                _sess.log_sandbox_attempt(
                    attempt, sandbox_result.passed, sandbox_result.output or "",
                    fix_content=new_function,
                )
            if sandbox_result.passed:
                steps.append(f"✓ Sandbox tests passed (attempt {attempt}/{_MAX_ATTEMPTS})")
                logger.info("[FixGen] Sandbox passed on attempt %d for %s", attempt, incident.id)
                break

            _test_failures = self._extract_test_failures(sandbox_result.output)
            reason = sandbox_result.error or "tests failed"
            steps.append(f"✗ Sandbox attempt {attempt}/{_MAX_ATTEMPTS} failed ({reason})\n{_test_failures}")
            logger.warning("[FixGen] Sandbox attempt %d failed for %s", attempt, incident.id)

            # Sandbox unavailable (npm/Docker not present) — skip rather than retry
            if sandbox_result.error and "not available" in sandbox_result.error:
                steps.append("⚠ Sandbox unavailable in this environment — skipping validation")
                logger.warning("[FixGen] Sandbox unavailable for %s — proceeding without validation", incident.id)
                break

            if attempt == _MAX_ATTEMPTS:
                return _fail(
                    f"Sandbox tests failed after {_MAX_ATTEMPTS} attempts: {reason}",
                    branch=branch_name,
                )

            steps.append(f"↻ Regenerating fix with test failure context (attempt {attempt + 1}/{_MAX_ATTEMPTS})")
            try:
                old_function, new_function, patches, retry_verdict = await self._generate_fix(
                    content, function_name, incident, file_path, context_bundle,
                    test_failures=_test_failures,
                )
                if retry_verdict is not None:
                    fix_verdict = retry_verdict
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

            # ── 5b. Secondary file fixes (same issue, other files) ────────
            # One dedicated _generate_fix() pass per additional file the diagnosis
            # named — see _resolve_secondary_targets(). Each pass is single-file
            # scoped exactly like the primary one; best-effort per file so one
            # failure doesn't block the others or the primary fix already committed.
            secondary_targets = self._resolve_secondary_targets(incident, file_path)
            secondary_files_changed: list[str] = []
            for secondary_path, secondary_fn, secondary_snippet in secondary_targets:
                try:
                    sec_content, sec_sha = await self._read_file(secondary_path, ref=PR_BASE)
                    sec_fn = secondary_fn or "(module-level)"
                    # Ground this in the ACTUAL primary diff + diagnosis snippet, not
                    # free-text prose alone. Real incident (AllInterviews PR #2552):
                    # 2 of 5 files got the correct fix applied to the existing
                    # handler; the other 3 -- given only a prose description of the
                    # bug with no concrete code to search for -- fabricated an
                    # entirely new, never-called route ("/by-preview-code") instead
                    # of finding and editing the real vulnerable handler, because
                    # nothing forced them to locate real matching code first. The
                    # exact before/after text from the primary fix is the strongest
                    # anchor available: search for that same pattern here.
                    if old_function:
                        primary_change = f"BEFORE:\n{old_function[:1500]}\n\nAFTER:\n{new_function[:1500]}\n\n"
                    elif patches:
                        primary_change = "\n\n".join(
                            f"BEFORE:\n{p_old[:600]}\n\nAFTER:\n{p_new[:600]}" for p_old, p_new in patches[:3]
                        ) + "\n\n"
                    else:
                        primary_change = ""
                    anchor = (
                        f"The identical bug was found and fixed in {file_path}. "
                        f"Here is the EXACT change that was applied there:\n\n{primary_change}"
                    )
                    if secondary_snippet:
                        anchor += (
                            f"The diagnosis found this specific code in THIS file "
                            f"({secondary_path}) with the same bug:\n{secondary_snippet[:800]}\n\n"
                        )
                    anchor += (
                        "Find the matching existing code in THIS file — same route path, "
                        "same field name, same query/find call — and apply the equivalent "
                        "fix to it. Do NOT write a new function, route, or endpoint that "
                        "doesn't already exist here. If you cannot locate closely matching "
                        "existing code in this file after searching, make NO edit at all "
                        "and call end_turn — leaving this file unchanged is correct when "
                        "the pattern genuinely isn't here; inventing new code is not."
                    )
                    sec_old, sec_new, sec_patches, _ = await self._generate_fix(
                        sec_content, sec_fn, incident, secondary_path,
                        test_failures=anchor,
                    )
                    if sec_old or sec_patches:
                        sec_new_content = self._apply_all_edits(sec_content, sec_old, sec_new, sec_patches)
                        await self._github.update_file(
                            self._owner, self._repo, secondary_path, sec_new_content,
                            f"fix: same issue in {secondary_path.split('/')[-1]}",
                            branch_name, sec_sha,
                        )
                        steps.append(f"✓ Committed secondary fix to {secondary_path}")
                        secondary_files_changed.append(secondary_path)
                    else:
                        steps.append(f"⚠ Secondary fix skipped — LLM produced no edits for {secondary_path}")
                except Exception as exc:
                    steps.append(f"⚠ Secondary fix failed ({secondary_path}): {exc} — proceeding without it")
                    logger.warning("[FixGen] Secondary fix error for %s: %s", secondary_path, exc)

            if secondary_targets:
                fixed = "\n".join(f"- `{p}`" for p in secondary_files_changed) or "(none)"
                skipped = self._skipped_secondary_files(secondary_targets, secondary_files_changed)
                skipped_note = (
                    "\n\nNot fixed automatically (review manually): "
                    + ", ".join(f"`{p}`" for p in skipped)
                    if skipped else ""
                )
                pr_body += (
                    f"\n\n## Same bug, other files\n"
                    f"Diagnosis found {len(secondary_targets)} additional affected file(s). "
                    f"Fixed automatically in this PR:\n{fixed}{skipped_note}"
                )

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

        # Persist the fix-agent's self-assessed verdict. None defaults to
        # confidence=0.75 (above the 0.70 gate) so the existing approval
        # flow continues unchanged when the LLM doesn't emit a verdict.
        fv = fix_verdict or {}
        verdict_confidence: float | None
        try:
            verdict_confidence = float(fv["confidence"]) if "confidence" in fv else None
        except (TypeError, ValueError):
            verdict_confidence = None
        verdict_escalate = bool(fv.get("escalate", False))
        verdict_reason = fv.get("escalate_reason") if isinstance(fv.get("escalate_reason"), str) else None
        verdict_blast_addressed = (
            bool(fv["blast_radius_addressed"]) if "blast_radius_addressed" in fv else None
        )

        return FixResult(
            issue_url=issue_url,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch_name,
            fix_description=f"Fix applied to {function_name} in {file_path}"
            + (f" (+{len(secondary_files_changed)} sibling file(s))" if secondary_files_changed else ""),
            files_changed=[file_path, *secondary_files_changed],
            test_added=False,
            commit_sha=commit_sha,
            target_file=file_path,
            target_function=function_name,
            confidence=verdict_confidence,
            escalate=verdict_escalate,
            escalate_reason=verdict_reason,
            blast_radius_addressed=verdict_blast_addressed,
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
          0. Diagnosis result — DiagnosisAgent already named the file + function; use it
             for all error types when both fields are populated
          1. Stack trace parsing — exact path from log, no LLM needed
          2. Code search using diagnosis function name (most reliable) or regex extraction
        """
        # ── 0. Diagnosis-identified target (all error types) ──────────
        # The DiagnosisAgent has already reasoned about root cause and named the file
        # and function. Prefer files where the function is defined; fall back to files
        # where it is called (the call site may itself be the bug).
        if incident.diagnosis_affected_file:
            diag_file = incident.diagnosis_affected_file.lstrip("/")
            diag_fn = incident.diagnosis_affected_function  # may be None for anonymous handlers
            try:
                content, _ = await self._read_file(diag_file, ref=PR_BASE)
                if diag_fn is None:
                    # Anonymous handler — file is the only target; use it directly.
                    logger.info("[FixGen] Using diagnosis target (anonymous handler): %s", diag_file)
                    return diag_file, "the function handling this error"
                if self._extract_js_function(content, diag_fn):
                    logger.info("[FixGen] Using diagnosis target: %s → %s", diag_file, diag_fn)
                    return diag_file, diag_fn
                # Function not defined here but may be called here — still a valid fix site.
                if diag_fn in content:
                    logger.info(
                        "[FixGen] Diagnosis target %s calls but doesn't define %s — using call site",
                        diag_file, diag_fn,
                    )
                    return diag_file, diag_fn
                logger.info(
                    "[FixGen] Diagnosis target %s has no reference to %s — falling back to stack trace",
                    diag_file, diag_fn,
                )
            except Exception:
                logger.info("[FixGen] Diagnosis target %s not fetchable — falling back to stack trace", diag_file)

        # ── 1. Stack trace (fastest, most accurate) ────────────────────
        frames = self._parse_stack_frames(incident)
        if frames:
            is_null = self._is_null_access_error(incident)
            # For null/undefined errors the crash frame is the symptom site.
            # Try caller frames first — that's where the null originates.
            ordered = frames[1:] + frames[:1] if is_null and len(frames) > 1 else frames
            for raw_path, fn_name in ordered:
                exists = (
                    self._local_repo.ready and self._local_repo.file_exists(raw_path)
                ) or await self._path_exists_in_repo(raw_path)
                if exists:
                    if is_null and raw_path == frames[0][0]:
                        logger.info("[FixGen] Null error — only crash frame found in repo: %s", raw_path)
                    else:
                        logger.info("[FixGen] Stack trace resolved: %s → %s", raw_path, fn_name)
                    return raw_path, fn_name or "the function handling this error"
            logger.info("[FixGen] No stack frame path found in repo — falling back")

        # ── 1b. File paths mentioned in diagnosis prose ───────────────────
        # When affected_file was nulled by grounding but the diagnosis text
        # still contains the correct path, extract and verify it here.
        if incident.diagnosis:
            import re as _re
            prose_paths = _re.findall(r'\b([\w/-]+\.(?:js|ts|jsx|tsx))\b', incident.diagnosis)
            for prose_path in dict.fromkeys(prose_paths):  # dedupe, preserve order
                exists = (
                    self._local_repo.ready and self._local_repo.file_exists(prose_path)
                ) or await self._path_exists_in_repo(prose_path)
                if exists:
                    logger.info("[FixGen] Diagnosis prose resolved file: %s", prose_path)
                    return prose_path, incident.diagnosis_affected_function or "the function handling this error"

        # ── 2. Code search using diagnosis function name or regex extraction ──
        # Prefer diagnosis_affected_function over regex — it's already been reasoned
        # about by the DiagnosisAgent and is far less likely to be a service/class name.
        fn_name = incident.diagnosis_affected_function or self._extract_function_name_from_error(incident)
        if fn_name:
            logger.info("[FixGen] No stack trace — trying code search for function '%s'", fn_name)
            try:
                matches = await self._github.search_code(self._owner, self._repo, fn_name)
                _SKIP = ("node_modules", ".test.", ".spec.", "dist/", "build/", "vendor/", "min.js")
                candidates = [r["path"] for r in matches if not any(s in r["path"] for s in _SKIP)]
                # First pass: prefer files that define the function
                first_reference: tuple[str, str] | None = None
                for path in candidates[:5]:
                    try:
                        content, _ = await self._read_file(path, ref=PR_BASE)
                        if self._extract_js_function(content, fn_name):
                            logger.info("[FixGen] Code search resolved (definition): %s → %s", fn_name, path)
                            return path, fn_name
                        if first_reference is None and fn_name in content:
                            first_reference = (path, fn_name)
                    except Exception:
                        pass
                # Second pass: accept a call-site reference if no definition found
                if first_reference:
                    logger.info(
                        "[FixGen] Code search resolved (reference): %s → %s", fn_name, first_reference[0]
                    )
                    return first_reference
            except Exception as exc:
                logger.debug("[FixGen] Code search for '%s' failed: %s", fn_name, exc)

        # ── 3. Keyword search from error message ──────────────────────────
        # Extracts distinctive model/collection/resource names from the error text
        # and searches for those when function-name search turns up nothing.
        # Example: "Error inserting into AppMasterReferrals" → search "AppMasterReferrals"
        for keyword in self._extract_error_keywords(incident):
            logger.info("[FixGen] Trying keyword search: '%s'", keyword)
            try:
                matches = await self._github.search_code(self._owner, self._repo, keyword)
                _SKIP = ("node_modules", ".test.", ".spec.", "dist/", "build/", "vendor/", "min.js")
                candidates = [r["path"] for r in matches if not any(s in r["path"] for s in _SKIP)]
                if candidates:
                    logger.info("[FixGen] Keyword search resolved: '%s' → %s", keyword, candidates[0])
                    return candidates[0], incident.diagnosis_affected_function or keyword
            except Exception as exc:
                logger.debug("[FixGen] Keyword search for '%s' failed: %s", keyword, exc)

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

    def _extract_error_keywords(self, incident: IncidentState) -> list[str]:
        """
        Extract distinctive model/collection/resource identifiers from the error message
        to use as fallback code-search terms when function-name search finds nothing.

        Examples:
          "Error inserting into AppMasterReferrals"  → ["AppMasterReferrals"]
          "S3NoSuchKey bucket my-bucket key foo/bar" → ["my-bucket"]
          "Failed to update UserProfile document"    → ["UserProfile"]
        """
        text = " ".join(filter(None, [
            incident.error_event.description or "",
            incident.error_event.title or "",
        ]))
        keywords: list[str] = []
        seen: set[str] = set()
        _NOISE = {
            "error", "failed", "cannot", "undefined", "null", "object",
            "function", "collection", "document", "database", "index",
            "mongobulkwriteerror", "bulkwriteerror", "duplicate",
        }

        # Pattern 1 — "Error [verb] into/from/for ModelName"
        for m in re.finditer(
            r'(?:inserting|updating|deleting|fetching|reading|writing)\s+(?:into|from|for|to)?\s*([A-Z][a-zA-Z0-9]{3,})',
            text, re.IGNORECASE,
        ):
            kw = m.group(1)
            if kw.lower() not in _NOISE and kw not in seen:
                seen.add(kw)
                keywords.append(kw)

        # Pattern 2 — standalone PascalCase identifiers (model/class names)
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z0-9]+)+)\b', text):
            kw = m.group(1)
            if kw.lower() not in _NOISE and kw not in seen and len(kw) > 5:
                seen.add(kw)
                keywords.append(kw)

        # Pattern 3 — MongoDB collection name from "collection: db.collectionName"
        for m in re.finditer(r'collection:\s*\w+\.(\w+)', text, re.IGNORECASE):
            kw = m.group(1)
            if kw.lower() not in _NOISE and kw not in seen:
                seen.add(kw)
                keywords.append(kw)

        return keywords[:3]  # at most 3 searches

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

    async def _trace_undefined_source(
        self, content: str, file_path: str, incident: IncidentState
    ) -> tuple[str, str, str] | None:
        """
        For null/undefined access errors, find the function that PRODUCES the
        undefined value and return (source_file_path, source_function_name, source_content).

        Strategy:
        1. Ask an LLM to read the file content and identify which function call
           returns the object whose property is undefined.
        2. Search the codebase for the definition of that function.
        3. Return its file + content so the fix targets the producer, not the consumer.
        """
        if not self._is_null_access_error(incident):
            return None

        # Ask the LLM: given this file and this error, what function produces the undefined?
        try:
            probe = await self._llm.complete(
                messages=[{"role": "user", "content": (
                    f"This file ({file_path}) has a production error:\n"
                    f"{incident.error_event.description or incident.error_event.title}\n\n"
                    f"FILE CONTENT (first 3000 chars):\n{content[:3000]}\n\n"
                    f"Identify the function call in this file that returns an object "
                    f"whose property is undefined at the crash line. "
                    f"Return ONLY the function name (e.g. 'classifyFields'). "
                    f"If you cannot identify it, return 'UNKNOWN'."
                )}],
                system=self._with_harness("Return only the function name. Single word or camelCase identifier. No punctuation."),
                model="claude-haiku-4-5-20251001",
            )
            source_fn = probe.strip().split()[0].strip(".,;:()")
        except Exception as exc:
            logger.debug("[FixGen] Source trace probe failed: %s", exc)
            return None

        if not source_fn or source_fn.upper() == "UNKNOWN" or len(source_fn) < 3:
            return None

        logger.info("[FixGen] Source trace: undefined value produced by '%s'", source_fn)

        # Search the repo for the definition of that function
        try:
            matches = await self._github.search_code(self._owner, self._repo, source_fn)
            _SKIP = ("node_modules", ".test.", ".spec.", "dist/", "build/", "vendor/", "min.js")
            candidates = [r["path"] for r in matches
                          if not any(s in r["path"] for s in _SKIP) and r["path"] != file_path]
            for path in candidates[:5]:
                try:
                    src_content, _ = await self._read_file(path, ref=PR_BASE)
                    # Only use this file if it actually DEFINES the function (not just calls it)
                    if self._extract_js_function(src_content, source_fn):
                        logger.info("[FixGen] Source trace resolved: %s → %s", source_fn, path)
                        return path, source_fn, src_content
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[FixGen] Source trace search failed: %s", exc)

        return None

    async def _fetch_call_chain(
        self,
        file_path: str,
        function_name: str,
        content: str,
    ) -> dict[str, list[tuple[str, str]]]:
        """Fetch related-file context for tiered prompt assembly.

        Returns a dict with three keys, each a list of (path, content) pairs:
          - "callers": files that reference the target function (Tier 2)
          - "tests":   existing test files for the target file (Tier 2)
          - "imports": local imports from the target file (Tier 3)

        TS/TSX files also pick up type-definition matches via a separate
        search for `interface <fn>` / `type <fn>`.
        """
        callers: list[tuple[str, str]] = []
        tests: list[tuple[str, str]] = []
        imports: list[tuple[str, str]] = []

        # ── Imports (Tier 3) ──────────────────────────────────────────
        for imp_path in self._parse_local_imports(file_path, content)[:4]:
            try:
                imp_content, _ = await self._read_file(imp_path, ref=PR_BASE)
                imports.append((imp_path, imp_content[:1200]))
                logger.debug("[FixGen] Call chain: fetched import %s", imp_path)
            except Exception:
                pass

        # ── Callers (Tier 2) ──────────────────────────────────────────
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
                    caller_content, _ = await self._read_file(path, ref=PR_BASE)
                    callers.append((path, caller_content[:1200]))
                    logger.debug("[FixGen] Call chain: fetched caller %s", path)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[FixGen] Call chain search failed: %s", exc)

        # ── Tests (Tier 2) ────────────────────────────────────────────
        # Existing tests are constraints — the fix must not break them.
        test_candidates, _ = self._test_file_candidates(file_path)
        for test_path in test_candidates[:3]:
            try:
                test_content, _ = await self._read_file(test_path, ref=PR_BASE)
                tests.append((test_path, test_content[:1500]))
                logger.debug("[FixGen] Call chain: fetched test %s", test_path)
                break  # one matching test file is plenty
            except Exception:
                continue

        # ── Type definitions for TS / TSX (Tier 2) ─────────────────────
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        if ext in ("ts", "tsx"):
            for query in (f"interface {function_name}", f"type {function_name}"):
                try:
                    type_results = await self._github.search_code(
                        self._owner, self._repo, query
                    )
                    for result in type_results[:2]:
                        path = result.get("path", "")
                        if not path or path == file_path or "node_modules" in path:
                            continue
                        if any(p == path for p, _ in callers):
                            continue
                        fragment = (result.get("fragment") or "")[:600]
                        if fragment:
                            callers.append((f"{path} (type)", fragment))
                except Exception:
                    pass

        return {"callers": callers, "tests": tests, "imports": imports}

    def _format_tier_block(
        self,
        label: str,
        items: list[tuple[str, str]],
        per_item_cap: int = 1200,
    ) -> str:
        """Render a list of (path, content) pairs as a single labelled block."""
        if not items:
            return ""
        lines: list[str] = []
        for path, body in items:
            lines.append(f"--- {label}: {path} ---\n{(body or '')[:per_item_cap]}")
        return "\n\n".join(lines)

    def _format_blast_radius(self, blast_radius: list[dict]) -> str:
        """Render diagnosis_blast_radius into a Tier 2 block."""
        if not blast_radius:
            return ""
        lines: list[str] = []
        for entry in blast_radius:
            file = entry.get("file", "")
            fn = entry.get("function", "") or "(top-level)"
            snippet = (entry.get("snippet", "") or "").strip()
            if not file:
                continue
            head = f"--- CALLER (from diagnosis): {file} :: {fn} ---"
            lines.append(f"{head}\n{snippet}" if snippet else head)
        return "\n\n".join(lines)

    def _resolve_secondary_targets(
        self, incident: IncidentState, primary_file: str
    ) -> list[tuple[str, str | None, str | None]]:
        """Every OTHER file the diagnosis says needs the identical fix.

        Historically only `diagnosis_additional_fix_file` (a single field) drove the
        one dedicated secondary-file pass in fix_with_steps(). That undercounts badly
        now that DiagnosisAgent's search-driven grounding (see targets/allinterviews/
        AGENTS.md) routinely finds every sibling in a copy-pasted-per-brand bug — a
        real incident found 7 additional files, but the pipeline only ever committed
        a fix to 1 of them. Three sources feed this now, all deduped by path and
        capped so a runaway diagnosis can't fan out into an unbounded number of
        commits:
          - diagnosis_blast_radius — structured caller list (populated for functions
            with real callers; usually empty for top-level route handlers).
          - diagnosis_additional_fix_targets — structured multi-file list, purpose-
            built for the copy-pasted-per-brand case (see DiagnosisAgent's
            additional_fix_targets/_enforce_grounding): a real incident correctly
            identified 3 vulnerable sibling files in one diagnosis, but
            diagnosis_additional_fix_file could only ever carry one of them — this
            field exists so ALL of them can actually get fixed in the same PR.
          - diagnosis_additional_fix_file — legacy single-file field, kept for the
            narrower "one other entry point" case.

        Returns (file, function, snippet) — the snippet (when the diagnosis captured
        one) is what actually lets a secondary pass locate real code instead of
        guessing; see the comment where this is consumed in fix_with_steps().
        """
        seen: dict[str, tuple[str | None, str | None]] = {}
        for entry in incident.diagnosis_blast_radius or []:
            file = entry.get("file")
            if not file or file == primary_file or file in seen:
                continue
            fn = entry.get("function") or None
            snippet = entry.get("snippet") or None
            seen[file] = (fn, snippet)
        for entry in incident.diagnosis_additional_fix_targets or []:
            file = entry.get("file")
            if not file or file == primary_file or file in seen:
                continue
            fn = entry.get("function") or None
            snippet = entry.get("snippet") or None
            seen[file] = (fn, snippet)
        secondary_path = incident.diagnosis_additional_fix_file
        if secondary_path and secondary_path != primary_file and secondary_path not in seen:
            seen[secondary_path] = (
                incident.diagnosis_additional_fix_function,
                incident.diagnosis_additional_fix_snippet,
            )
        return [(f, fn, snip) for f, (fn, snip) in list(seen.items())[:_MAX_SECONDARY_FIXES]]

    @staticmethod
    def _skipped_secondary_files(
        secondary_targets: list[tuple[str, str | None, str | None]],
        secondary_files_changed: list[str],
    ) -> list[str]:
        """Which secondary targets did NOT end up with a committed fix.

        Pulled out of the inline PR-body-building code specifically so it's
        unit-testable on its own: a real production bug (FixGenerationAgent
        raising "too many values to unpack (expected 2)" on every multi-file
        incident) was exactly this logic silently left unpacking 2-tuples after
        _resolve_secondary_targets() was extended to return 3-tuples (file,
        function, snippet) in an earlier change. An inline list comprehension
        buried in fix_with_steps() has no way to be covered by a targeted test;
        this does.
        """
        return [f for f, _fn, _snip in secondary_targets if f not in secondary_files_changed]

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

    # ------------------------------------------------------------------
    # Agentic fix tools
    # ------------------------------------------------------------------

    _FIX_TOOLS = [
        {
            "name": "read_file",
            "description": (
                "Read any file from the repository. Use this to understand imports, "
                "callers, type definitions, or any related code before writing the fix."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file path"}
                },
                "required": ["path"],
            },
        },
        {
            "name": "search_code",
            "description": "Search for a symbol or string across the repository. Returns matching file paths.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol name or code string to search for"}
                },
                "required": ["query"],
            },
        },
        {
            "name": "find_callers",
            "description": (
                "Query the call graph to find every function that calls the target function. "
                "Use this BEFORE generating a fix to understand the blast radius — if callers exist, "
                "include updates to them in the same patch when needed. "
                "Returns file paths, caller function names, and line numbers. "
                "Returns an empty list if the function has no known callers or is not in the index."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "function_name": {
                        "type": "string",
                        "description": "Simple function name to look up (e.g. 'classifyFields', not 'obj.classifyFields')",
                    }
                },
                "required": ["function_name"],
            },
        },
        {
            "name": "apply_edit",
            "description": (
                "Replace the primary target function with the fixed version. "
                "If the old function text was pre-extracted, only supply new_text. "
                "If not pre-extracted, supply both old_text (verbatim from file) and new_text. "
                "Call this ONCE for the main function fix."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "new_text": {
                        "type": "string",
                        "description": "Complete fixed version of the target function",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Verbatim text to replace (required when function was not pre-extracted)",
                    },
                },
                "required": ["new_text"],
            },
        },
        {
            "name": "patch_line",
            "description": (
                "Make a targeted replacement anywhere in the file — for issues OUTSIDE the primary function: "
                "wrong model IDs, missing error handling, stale comments, adjacent bugs, etc.\n"
                "Rules for old_snippet:\n"
                "- Copy EXACT verbatim text from the file (1–5 lines). Short and unique.\n"
                "- Do NOT use this to replace the primary function — use apply_edit for that.\n"
                "You can call this multiple times for different issues in the file."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "old_snippet": {
                        "type": "string",
                        "description": "Exact verbatim text to replace (1–5 lines, copied from the file)",
                    },
                    "new_snippet": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                },
                "required": ["old_snippet", "new_snippet"],
            },
        },
        {
            "name": "final_verdict",
            "description": (
                "OPTIONAL last call — emit your self-assessed verdict on the fix you just made. "
                "Call this AFTER apply_edit + any patch_line calls, RIGHT BEFORE end_turn. "
                "If you skip it, the runtime defaults to confidence=0.75, escalate=false. "
                "Use confidence < 0.70 OR escalate=true when you're unsure: a shaky fix opens "
                "a human approval gate instead of a PR. Be honest — escalation is correct, "
                "guessing is not."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "confidence": {
                        "type": "number",
                        "description": "0.0 – 1.0. < 0.70 routes to human approval.",
                    },
                    "escalate": {
                        "type": "boolean",
                        "description": "True if a human should review before the PR opens.",
                    },
                    "escalate_reason": {
                        "type": "string",
                        "description": (
                            "Required if escalate=true. Examples: "
                            "'didn't read all Tier 2 callers', "
                            "'unsure about contract change impact', "
                            "'fix may not handle all input shapes'."
                        ),
                    },
                    "blast_radius_addressed": {
                        "type": "boolean",
                        "description": "True if every Tier 2 caller still works with this fix.",
                    },
                },
                "required": ["confidence"],
            },
        },
    ]

    def _apply_all_edits(
        self, content: str, old_function: str, new_function: str, patches: list[tuple[str, str]]
    ) -> str:
        """Apply the primary function replacement then all patch_line edits sequentially."""
        new_content = content.replace(old_function, new_function, 1)
        if new_content == content:
            new_content = content.replace(old_function.strip(), new_function.strip(), 1)
        for old_snip, new_snip in patches:
            if old_snip in new_content:
                new_content = new_content.replace(old_snip, new_snip, 1)
            else:
                logger.warning("[FixGen] patch_line: snippet not found verbatim — skipping")
        return new_content

    async def _generate_fix(
        self, content: str, function_name: str, incident: IncidentState,
        file_path: str = "",
        context_bundle: dict[str, list[tuple[str, str]]] | None = None,
        test_failures: str = "",
    ) -> tuple[str, str, list[tuple[str, str]], dict | None]:
        """
        Agentic fix generation using Claude tool_use — mirrors how Claude Code works.

        The LLM receives the target file and tools to read any other files it needs.
        It calls apply_edit for the primary function fix, then patch_line for adjacent
        issues (wrong model IDs, missing error handling, stale values, etc.). Optionally
        the LLM emits a `final_verdict` tool call with self-assessed confidence/escalate.
        Returns (old_function, new_function, patches, verdict) — verdict is None when the
        LLM didn't emit one. On failure: ("", "", [], verdict_or_None).

        Context is structured into three labelled tiers:
          - Tier 1: the file containing the broken function (most important).
          - Tier 2: callers / tests / type defs the fix MUST NOT BREAK
                    (constraints, not just information).
          - Tier 3: imports + harness docs (background).
        Diagnosis-supplied blast_radius takes priority over caller search
        results when populated.
        """
        bundle = context_bundle or {"callers": [], "tests": [], "imports": []}

        human_notes_section = (
            f"\nHUMAN FEEDBACK (from previous attempt — MUST follow):\n{incident.human_notes}\n"
            if incident.human_notes else ""
        )
        test_failures_section = (
            f"\nTEST FAILURES from previous attempt — new fix must not break these:\n{test_failures}\n"
            if test_failures else ""
        )

        # Contract-change banner: surface at the top so it can't be missed.
        contract_change = (incident.diagnosis_contract_change or "none").lower()
        contract_warning = ""
        if contract_change != "none":
            detail = incident.diagnosis_contract_change_detail or ""
            contract_warning = (
                f"\n⚠ CONTRACT CHANGE: this fix changes the function's "
                f"{contract_change.replace('_', ' ')}"
                + (f" — {detail}" if detail else "")
                + ". Every Tier 2 caller must be updated.\n"
            )

        # Tier 2: prefer diagnosis blast_radius (curated), fall back to callers
        # found via code search. Tests + type defs are always added.
        tier2_blast = self._format_blast_radius(incident.diagnosis_blast_radius or [])
        tier2_callers_fallback = self._format_tier_block("CALLER", bundle.get("callers", []))
        tier2_callers = tier2_blast or tier2_callers_fallback
        tier2_tests = self._format_tier_block("TEST", bundle.get("tests", []), per_item_cap=1500)
        tier2_blocks = "\n\n".join(b for b in (tier2_callers, tier2_tests) if b)

        tier2_section = (
            "\n## TIER 2 — Callers, tests, and type contracts your fix MUST NOT BREAK\n"
            f"{tier2_blocks}\n" if tier2_blocks else ""
        )

        # Tier 3: imports — context for understanding only, not constraints.
        tier3_blocks = self._format_tier_block("IMPORT", bundle.get("imports", []))
        tier3_section = (
            "\n## TIER 3 — Background context (read for understanding, not as a constraint)\n"
            f"{tier3_blocks}\n" if tier3_blocks else ""
        )
        additional_fix_section = ""
        if incident.diagnosis_additional_fix:
            secondary_loc = ""
            if incident.diagnosis_additional_fix_function and incident.diagnosis_additional_fix_file:
                secondary_loc = f" ({incident.diagnosis_additional_fix_function} in {incident.diagnosis_additional_fix_file})"
            elif incident.diagnosis_additional_fix_function:
                secondary_loc = f" ({incident.diagnosis_additional_fix_function})"
            # This section used to invite the model to `read_file` the secondary
            # location "for context" — but apply_edit/patch_line can only ever touch
            # `file_path` (the file already fetched into this call). When a diagnosis
            # names more affected files than the single diagnosis_additional_fix_file
            # this pipeline can act on (a live incident found 7 — see PR referencing
            # this comment), the model would dutifully read_file every one of them
            # looking for a way to fix them, burn the entire iteration budget on
            # unreachable files, and never call patch_line/apply_edit on the one file
            # it actually can fix. diagnosis_additional_fix_file (if set) already gets
            # its own dedicated _generate_fix() pass after this one returns (see the
            # "Secondary file fix" step in run()) — so this pass must not attempt it.
            additional_fix_section = (
                f"\nDIAGNOSIS NOTE (background only — do not act on this in this call): "
                f"{incident.diagnosis_additional_fix}\n"
                f"Your ONLY target in this call is {function_name} in {file_path}. "
                + (
                    f"The single additional location{secondary_loc} is fixed automatically "
                    f"in a separate pass right after this one — do not read or touch it here.\n"
                    if incident.diagnosis_additional_fix_file else
                    "Any other files named above are NOT fixed automatically and are out of "
                    "scope for this call.\n"
                )
                + f"Do NOT call read_file on any file other than {file_path} because of this "
                f"note — stay on target and call patch_line/apply_edit for it now.\n"
            )

        # Pre-extract the target function so the LLM never needs to reproduce old text.
        # If extraction succeeds, apply_edit only needs new_text — old_text comes from here.
        _is_sentinel = function_name in (
            "(module-level)", "the function handling this error", "", None,
        )
        extracted_old = None if _is_sentinel else self._extract_js_function(content, function_name)
        # Fall through to module-level path if extraction failed — the file may be a
        # top-to-bottom script with no enclosing function (e.g. a Fargate task entry point).
        _is_module_level = _is_sentinel or (extracted_old is None)
        if extracted_old:
            function_ref = (
                f"\nCURRENT FUNCTION (do NOT reproduce this in apply_edit — it is already known):\n"
                f"<OLD_REFERENCE>\n{extracted_old}\n</OLD_REFERENCE>\n\n"
                f"Call apply_edit(new_text=<your fixed version>) when ready.\n"
            )
        elif _is_module_level:
            function_ref = (
                f"\nACTION REQUIRED — module-level fix (no enclosing function):\n"
                f"The file content is shown above. Do NOT call read_file — the code is already here.\n"
                f"Call patch_line NOW with:\n"
                f"  old_snippet = the exact line(s) from the file that need changing (copy verbatim)\n"
                f"  new_snippet = the replacement (for an insertion, prepend the new line before the existing one)\n"
                f"Example for adding a line before a call:\n"
                f"  patch_line(old_snippet='mongoose.connect(...)', new_snippet='mongoose.set(...);\\nmongoose.connect(...)')\n"
                f"Do not call any other tool first. patch_line immediately.\n"
            )
        else:
            function_ref = (
                f"\nThe function '{function_name}' could not be pre-extracted (may be a class method, "
                f"arrow function, or React component method).\n"
                f"Read the file above, find the specific code that needs changing, then:\n"
                f"  • For a small targeted change (wrapping a call, changing a value): "
                f"use patch_line(old_snippet=<verbatim 1-5 lines from file>, new_snippet=<replacement>).\n"
                f"  • For replacing a whole function: "
                f"use apply_edit(old_text=<verbatim function from file>, new_text=<fixed version>).\n"
                f"Do NOT guess at text — copy it exactly from the file content shown above.\n"
            )

        _target_label = (
            f"TARGET: module-level code in {file_path} (no enclosing function — use patch_line)"
            if _is_module_level else
            f"TARGET FUNCTION: {function_name} in {file_path}"
        )
        initial_prompt = (
            f"Fix this production bug.\n\n"
            f"ERROR TYPE: {incident.error_event.error_type or 'unknown'}\n"
            f"ERROR: {incident.error_event.description or incident.error_event.title}\n"
            f"ROOT CAUSE: {incident.diagnosis}\n"
            f"{_target_label}\n"
            f"{contract_warning}{human_notes_section}{test_failures_section}{additional_fix_section}"
            f"\n## TIER 1 — Code you are changing (most important)\n"
            f"FILE: {file_path}\n{content}\n"
            f"{function_ref}"
            f"{tier2_section}"
            f"{tier3_section}"
            f"ROOT CAUSE RULES (violation = wrong fix):\n"
            f"1. Fix the cause, not the symptom. No null guards / optional chaining / try-catch at crash sites.\n"
            f"2. For 'Cannot read properties of undefined/null': fix the function that RETURNS the undefined value — "
            f"every return path must include the complete structure callers depend on.\n"
            f"3. The fix must handle ALL invalid inputs, not just the one that triggered this error.\n"
            f"4. No unrelated cleanup, logging, or comments.\n"
            f"5. Never invent a new function, route, or endpoint that doesn't already exist as your "
            f"'fix' for an existing bug. Find and edit the REAL vulnerable code. If you cannot locate "
            f"it in this file after actually searching, make no edit and call end_turn — that is "
            f"correct; fabricating unrelated new code is not.\n"
            f"6. When validating that input matches an expected narrow shape (e.g. a numeric ID), reason "
            f"about what the ACTUAL valid shape looks like — don't reach for a loose type-coercion check "
            f"out of habit. `isNaN(Number(x))` looks like numeric validation but isn't strict: "
            f"Number('1e5') is 100000 (scientific notation), Number('0x1A') is 26 (hex), Number('  5  ') "
            f"trims whitespace, Number('') is 0 — all pass as 'valid numbers' even though none of them are "
            f"the plain digit string the field actually expects. If the value should be a plain positive "
            f"integer, validate that exact shape (e.g. a digits-only regex, or Number.isInteger() plus a "
            f"range check — whichever fits the actual domain) instead of trusting Number()'s permissive "
            f"coercion to reject what it won't.\n\n"
            f"IF THE TARGET FUNCTION IS NOT IN THIS FILE:\n"
            f"The function '{function_name}' may be defined in a different file than the one shown above "
            f"(e.g. it may be a backend service called by this frontend component, or a helper in a "
            f"sibling directory). If you cannot find it here:\n"
            f"1. Use search_code to search for '{function_name}' — this will find where it is actually defined.\n"
            f"2. Use read_file on the correct file.\n"
            f"3. Apply your fix there using patch_line or apply_edit.\n"
            f"Do not spin in place — if the function is not here, find where it is.\n\n"
            f"MULTI-EDIT WORKFLOW:\n"
            f"1. Call apply_edit or patch_line ONCE for the primary function fix.\n"
            f"2. After the fix, review the file as a senior engineer doing code review.\n"
            f"   Fix every issue you would flag — not just the primary bug.\n"
            f"3. Call patch_line for each additional issue found (can call multiple times).\n"
            f"4. Only call end_turn when ALL issues in the file are addressed.\n"
        )

        system = self._with_harness(
            "You are a senior software engineer fixing production bugs. "
            "Read the code carefully, explore related files as needed, then call apply_edit "
            "with the minimum precise change. After apply_edit, review the file as you would "
            "in a code review — apply the same quality bar you'd hold a junior engineer to. "
            "Call patch_line for every issue you'd flag. Always fix root causes — never symptoms. "
            "Call end_turn only when the file would pass your review."
        )

        messages: list[dict] = [{"role": "user", "content": initial_prompt}]
        edit_result: dict | None = None
        patch_calls: list[dict] = []
        verdict: dict | None = None
        _SKIP = ("node_modules", "dist/", "build/", ".min.js")
        _total_pruned_chars = 0
        import json as _json

        # 14 -> 20: modest headroom now that the prompt above no longer invites
        # exploring files this loop has no ability to edit (see additional_fix_section
        # comment). Legitimate single-file work — read tests/callers, patch, then scan
        # for adjacent issues — can still reasonably need more than 14 turns.
        for iteration in range(20):
            if iteration > 0 and iteration % 3 == 0:
                messages, pruned = _prune_tool_results(messages)
                _total_pruned_chars += pruned
                if pruned:
                    logger.debug("[FixGen] State pruning at iteration %d: removed %d chars", iteration, pruned)
            try:
                text, tool_calls, stop_reason = await self._llm.complete_with_tools(
                    messages, self._FIX_TOOLS, system=system
                )
            except Exception as exc:
                logger.error("[FixGen] Agentic LLM call failed (iteration %d): %s", iteration, exc)
                return "", "", [], None

            if stop_reason == "max_tokens":
                logger.warning(
                    "[FixGen] Agentic: LLM hit max_tokens at iteration %d — tool inputs may be truncated; discarding",
                    iteration,
                )
                # Truncated tool calls produce incomplete new_text — don't accept them.
                # Ask the model to produce a shorter replacement on the next iteration.
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response was cut off because it exceeded the output limit. "
                        "Write a shorter, more focused fix. Use apply_edit with only the minimum "
                        "lines that need to change — do not reproduce unchanged surrounding code."
                    ),
                })
                continue

            if stop_reason == "end_turn" or not tool_calls:
                if not edit_result:
                    logger.warning("[FixGen] Agentic: LLM stopped without apply_edit (iteration %d)", iteration)
                break

            messages.append({
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": _json.dumps(tc["input"])},
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                name = tc["name"]
                if name == "apply_edit":
                    if edit_result is None:
                        edit_result = tc["input"]
                    result = (
                        "✓ Primary function fix recorded. "
                        "Now scan the ENTIRE file for adjacent issues — wrong model IDs, "
                        "missing error handling, stale hardcoded values — and call patch_line "
                        "for each one found. Call end_turn when done."
                    )
                elif name == "patch_line":
                    patch_calls.append(tc)
                    snip = tc["input"].get("old_snippet", "")[:60].replace("\n", "↵")
                    result = f"✓ patch_line recorded ({snip}). Continue scanning for more issues or call end_turn."
                elif name == "read_file":
                    path = tc["input"].get("path", "")
                    try:
                        file_content, _ = await self._read_file(path, ref=PR_BASE)
                        scan_for_injection(file_content, source=f"github-file:{path}")
                        result = wrap_untrusted(file_content, source=f"github-file:{path}")
                    except Exception as exc:
                        result = f"Error reading {path}: {exc}"
                    logger.debug("[FixGen] Agentic read_file: %s", path)
                elif name == "search_code":
                    query = tc["input"].get("query", "")
                    try:
                        matches = await self._github.search_code(self._owner, self._repo, query)
                        paths = [r["path"] for r in matches if not any(s in r["path"] for s in _SKIP)][:10]
                        result = "\n".join(paths) if paths else "No results"
                    except Exception as exc:
                        result = f"Search failed: {exc}"
                    logger.debug("[FixGen] Agentic search_code: %s", query)
                elif name == "find_callers":
                    fn_name = tc["input"].get("function_name", "")
                    try:
                        callers = _code_graph.find_callers(fn_name)
                        if not callers:
                            result = f"No callers found for '{fn_name}' in the call graph index."
                        else:
                            lines = [f"Callers of `{fn_name}` ({len(callers)} found):"]
                            for c in callers[:10]:
                                lines.append(f"  {c.file_path} → {c.function_name}() at line {c.line}")
                            if len(callers) > 10:
                                lines.append(f"  ... and {len(callers) - 10} more")
                            result = "\n".join(lines)
                    except Exception as exc:
                        result = f"Call graph lookup failed: {exc}"
                    logger.debug("[FixGen] find_callers: %s → %d results", fn_name, len(callers) if "callers" in dir() else 0)
                elif name == "final_verdict":
                    verdict = tc["input"]
                    logger.info(
                        "[FixGen] Agentic verdict: confidence=%s escalate=%s reason=%s",
                        verdict.get("confidence"),
                        verdict.get("escalate"),
                        verdict.get("escalate_reason"),
                    )
                    if not edit_result and not patch_calls:
                        result = (
                            "ERROR: You called final_verdict before making any edits. "
                            "You MUST call patch_line or apply_edit FIRST to actually change the code. "
                            "Call patch_line now with the exact lines to change, then call final_verdict."
                        )
                    else:
                        result = "✓ verdict recorded — call end_turn now."
                else:
                    result = f"Unknown tool: {name}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        if _total_pruned_chars:
            logger.info("[FixGen] State pruning total: %d chars removed across %d iterations", _total_pruned_chars, iteration + 1)

        if not edit_result:
            # Module-level fix: LLM used patch_line only (no enclosing function to replace).
            # If patch_calls has entries, return them as a patches-only result.
            patches_only = [
                (tc["input"]["old_snippet"], tc["input"]["new_snippet"])
                for tc in patch_calls
                if tc["input"].get("old_snippet") and tc["input"].get("new_snippet")
            ]
            if patches_only:
                logger.info(
                    "[FixGen] Agentic fix: module-level patch_line only (%d patches) in %s",
                    len(patches_only), file_path,
                )
                return "", "", patches_only, verdict
            logger.error("[FixGen] Agentic fix: no apply_edit call after %d iterations", iteration + 1)
            return "", "", [], verdict

        new_text = edit_result.get("new_text", "")
        if not new_text:
            logger.error("[FixGen] Agentic apply_edit: empty new_text")
            return "", "", [], verdict

        # Prefer pre-extracted old_text; fall back to LLM-supplied old_text
        if extracted_old:
            old_text = extracted_old
        else:
            llm_old = edit_result.get("old_text", "").strip()
            if not llm_old:
                logger.error("[FixGen] Agentic apply_edit: no old_text for %s (not pre-extracted and not provided)", function_name)
                return "", "", [], verdict
            old_text = llm_old
            logger.info("[FixGen] Agentic apply_edit: using LLM-supplied old_text for %s", function_name)

        if old_text not in content and old_text.strip() not in content:
            logger.error("[FixGen] Agentic apply_edit: old_text not found in %s", file_path)
            return "", "", [], verdict

        patches = [
            (tc["input"]["old_snippet"], tc["input"]["new_snippet"])
            for tc in patch_calls
            if tc["input"].get("old_snippet") and tc["input"].get("new_snippet")
        ]
        logger.info(
            "[FixGen] Agentic fix: %d chars → %d chars in %s, patch_line calls=%d",
            len(old_text), len(new_text), file_path, len(patches),
        )
        return old_text, new_text.strip(), patches, verdict

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

    @staticmethod
    def _critique_content(old_function: str, new_function: str, patches: list[tuple[str, str]]) -> tuple[str, str]:
        """Return (old_code, new_code) text to hand to _critique_fix.

        A fix can be represented two ways: a full old_function/new_function
        replacement, or a list of (old_snippet, new_snippet) line-level
        patches (patches path) -- and it's valid for a fix to use ONLY the
        patches path, leaving old_function/new_function as empty strings
        (see the `if not old_function and not patches` guard above). Confirmed
        in production: _critique_fix was called with those empty strings
        whenever a fix used patches, so the reviewer had nothing to look at
        ("I don't see the OLD CODE or NEW CODE sections populated") and that
        confused response leaked verbatim into a real GitHub PR body. Render
        the patches into the same shape when the whole-function fields are
        empty, so the critique always has real content to review.
        """
        if old_function or new_function or not patches:
            return old_function, new_function
        return (
            "\n\n".join(old for old, _new in patches),
            "\n\n".join(new for _old, new in patches),
        )

    async def _critique_fix(
        self, old_code: str, new_code: str, incident: IncidentState, file_path: str = ""
    ) -> str:
        """
        Self-critique pass (Haiku) — four explicit checks instead of one.

        The original critique focused on symptom-vs-root-cause. That signal is
        retained (load-bearing — it catches null-guard regressions). On top of
        it we ask:
          1. Does the fix break any Tier 2 caller (from diagnosis blast_radius)?
          2. Did you handle the edge cases mentioned in the diagnosis?
          3. Is there a simpler fix that achieves the same result?
          4. Does any other file need updating that you didn't touch?

        Returns a short plain-text assessment ending in a verdict line:
        `LOOKS CORRECT` / `NEEDS REVIEW` / `LIKELY WRONG`. The agentic retry
        loop reads `LIKELY WRONG` to fire an alternate-frame retry.
        """
        rag_section = ""
        if self._rag is not None and file_path:
            try:
                if self._rag._collection.count() > 0:
                    # Search for callers and related code — crucial for catching symptom fixes
                    query = f"{file_path} {incident.diagnosis or ''}"
                    chunks = await self._rag.hybrid_search(query, n_results=5, min_score=0.45)
                    chunks = [c for c in chunks if c.file_path != file_path][:4]
                    if chunks:
                        lines = ["\nRELATED CODEBASE CONTEXT (callers, dependencies):"]
                        for c in chunks:
                            lines.append(f"\n--- {c.file_path} (lines {c.start_line}–{c.end_line}) ---")
                            lines.append(c.content[:400])
                        rag_section = "\n".join(lines)
            except Exception as exc:
                logger.debug("[FixGen] Critique RAG skipped: %s", exc)

        # Surface diagnosis blast_radius callers as Tier 2 constraints in the
        # critique prompt so check #1 can actually be evaluated.
        blast_section = ""
        if incident.diagnosis_blast_radius:
            blast_section = "\nTIER 2 CALLERS (must not break):\n" + self._format_blast_radius(
                incident.diagnosis_blast_radius
            )

        contract_section = ""
        cc = (incident.diagnosis_contract_change or "none").lower()
        if cc != "none":
            detail = incident.diagnosis_contract_change_detail or ""
            contract_section = (
                f"\nCONTRACT CHANGE: this fix changes the function's "
                f"{cc.replace('_', ' ')}"
                + (f" — {detail}" if detail else "")
                + ". Every Tier 2 caller above must already work with the new contract or also be updated."
            )

        prompt = (
            f"A production fix was generated. Critique it as a skeptical senior reviewer.\n\n"
            f"FILE: {file_path or '(not specified)'}\n"
            f"ROOT CAUSE (diagnosis's own description — may be imprecise; the code below is "
            f"ground truth if the two ever disagree): {incident.diagnosis}\n"
            f"ERROR: {incident.error_event.description or incident.error_event.title}\n\n"
            f"OLD CODE:\n{old_code[:800]}\n\n"
            f"NEW CODE:\n{new_code[:800]}\n"
            f"{blast_section}{contract_section}{rag_section}\n\n"
            f"SYMPTOM-FIX CHECKLIST — flag immediately if the new code:\n"
            f"- Adds a null check, optional chaining (?.), nullish coalescing (??), or try/catch\n"
            f"  at or near the crash line without fixing the function that produces the null value\n"
            f"- Adds `if (x && x.y)` or `x?.y ?? default` where `x` comes from an external call\n"
            f"  (LLM, API, DB) — the fix belongs in the function that makes that call\n"
            f"- Converts an invalid value instead of preventing it from being invalid in the first place\n"
            f"- Fixes only the happy-path return of a producer function but leaves error/early-exit\n"
            f"  return paths still missing the expected field — all return paths must be complete\n\n"
            f"FOUR EXPLICIT CHECKS — answer each in one sentence:\n"
            f"1. Does the fix BREAK any Tier 2 caller listed above? "
            f"(Walk the callers; check each still works with the new function shape.)\n"
            f"2. Did the fix handle every edge case implied by the root cause / diagnosis?\n"
            f"3. Is there a SIMPLER fix that achieves the same result?\n"
            f"4. Does any other file need updating that this fix didn't touch? "
            f"(Especially callers if the contract changed.)\n\n"
            f"Then answer:\n"
            f"5. Does the fix address the root cause, or does it just suppress/convert the error? "
            f"Apply the symptom-fix checklist above.\n\n"
            f"Do NOT fail the fix solely because a route path, endpoint name, or other string in "
            f"ROOT CAUSE's wording doesn't literally appear in the OLD/NEW CODE shown above — the "
            f"code is ground truth, ROOT CAUSE is a paraphrase that can be imprecise even when the "
            f"code change itself is correct. Judge the CODE change against the ERROR, not against "
            f"ROOT CAUSE's exact wording.\n\n"
            f"FINAL LINE — must be exactly one of: LOOKS CORRECT / NEEDS REVIEW / LIKELY WRONG"
        )
        try:
            result = await self._llm_haiku.complete(
                messages=[{"role": "user", "content": prompt}],
                system=self._with_harness("You are a skeptical senior engineer reviewing an AI-generated fix. Be concise and critical."),
            )
        except Exception as exc:
            logger.warning("[FixGen] Critique failed: %s", exc)
            return "Critique unavailable"

        # A response with none of the three required verdict words means
        # something went wrong upstream (e.g. the reviewer had nothing real
        # to look at, as above) rather than a genuine assessment. Without
        # this check the raw confused response — once, literally "I cannot
        # review a fix I cannot see... Please provide: 1. OLD CODE..." —
        # just flows through unchanged into steps, the retry-verdict check,
        # and a real GitHub PR body.
        if not any(v in result.upper() for v in ("LOOKS CORRECT", "NEEDS REVIEW", "LIKELY WRONG")):
            logger.warning("[FixGen] Critique had no valid verdict — treating as failed: %s", result[:200])
            return "Critique inconclusive — reviewer did not return a valid verdict. Manual review recommended."

        return result
