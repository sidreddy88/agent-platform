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

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from app.agents.base import BaseAgent
from app.core.config import settings
from app.models.events import IncidentState
from app.services.blast_radius import BlastRadiusGuard
from app.services.github import GitHubError, GitHubService
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


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
        self._github = github or GitHubService()
        self._owner, self._repo = settings.fix_target_repo.split("/", 1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fix(self, incident: IncidentState) -> FixResult:
        """Generate a fix and open a GitHub PR."""
        result, _ = await self.fix_with_steps(incident)
        return result

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
        test_candidates, default_test_path = self._test_file_candidates(file_path)

        # ── 2. Resolve default branch + fetch the file ─────────────────
        try:
            default_branch = await self._github.get_default_branch(self._owner, self._repo)
            steps.append(f"✓ Default branch: {default_branch}")
            logger.info("[FixGen] Default branch: %s", default_branch)
        except GitHubError as exc:
            default_branch = "main"
            steps.append(f"⚠ Could not detect default branch ({exc}) — assuming '{default_branch}'")

        try:
            content, file_sha = await self._github.get_file_contents(
                self._owner, self._repo, file_path, ref=default_branch
            )
            steps.append(f"✓ Fetched {file_path} (sha={file_sha[:8]}, {len(content)} chars)")
            logger.info("[FixGen] Fetched %s (%d chars)", file_path, len(content))
        except GitHubError as exc:
            # 404 — try to find the file elsewhere in the repo by basename
            if "404" in str(exc):
                basename = file_path.rsplit("/", 1)[-1]
                steps.append(f"⚠ {file_path} not found — searching repo for '{basename}'")
                matches = await self._github.find_files_by_name(
                    self._owner, self._repo, basename, ref=default_branch
                )
                if matches:
                    file_path = matches[0]
                    steps.append(f"✓ Found at {file_path} — retrying fetch")
                    logger.info("[FixGen] Resolved path via tree search: %s", file_path)
                    try:
                        content, file_sha = await self._github.get_file_contents(
                            self._owner, self._repo, file_path, ref=default_branch
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

        # ── 3. Generate fix via LLM ────────────────────────────────────
        try:
            old_function, new_function = await self._generate_fix(content, function_name, incident)
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
        files_to_touch = [file_path, default_test_path]
        additions = len(new_function.splitlines())
        deletions = len(old_function.splitlines())
        br_result = BlastRadiusGuard().check(files_to_touch, additions=additions, deletions=deletions)
        if not br_result.allowed:
            steps.append(f"✗ Blast radius violated: {br_result.reason}")
            logger.warning("[FixGen] Blast radius violation: %s", br_result.reason)
            return FixResult(
                issue_url=None,
                pr_url=None,
                pr_number=None,
                branch=branch_name,
                fix_description=f"BLAST_RADIUS_VIOLATION: {br_result.reason}",
                blast_radius_violation=True,
                blast_radius_violations=br_result.violations,
            ), steps
        steps.append(f"✓ Blast radius OK ({len(files_to_touch)} files, +{additions}/-{deletions} lines)")

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

        # ── 5. Apply fix, commit on branch, open PR ────────────────────
        if old_function in content:
            new_content = content.replace(old_function, new_function, 1)
        elif old_function.strip() in content:
            new_content = content.replace(old_function.strip(), new_function.strip(), 1)
        else:
            steps.append("✗ old_function not found verbatim in file — cannot apply patch")
            logger.error("[FixGen] old_function not found in content")
            return _fail("old_function not found in file — LLM may have altered it", issue_url, branch_name)

        pr_body = (
            f"## Summary\n"
            f"- **Root cause:** {incident.diagnosis}\n"
            f"- **Fix:** Updated `{function_name}` in `{file_path}`\n\n"
            f"{f'Fixes #{issue_number}' if issue_number else ''}\n\n"
            f"**Incident ID:** {incident.id}  \n"
            f"**Agent confidence:** {incident.confidence:.0%}"
        )

        try:
            base_sha = await self._github.get_branch_sha(
                self._owner, self._repo, default_branch
            )
            await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            steps.append(f"✓ Created branch {branch_name} from {default_branch}")

            issue_ref = f"Fixes #{issue_number}" if issue_number else ""
            commit_sha = await self._github.update_file(
                self._owner, self._repo, file_path, new_content,
                f"fix: {function_name} in {file_path.split('/')[-1]}\n\n{issue_ref}",
                branch_name, file_sha,
            )
            steps.append(f"✓ Committed fix (sha={commit_sha[:8]})")
            logger.info("[FixGen] Committed fix on branch %s", branch_name)

            # ── 5b. Add test file ──────────────────────────────────────
            test_added = await self._commit_test(
                branch_name, new_function, incident, test_candidates, default_test_path, steps
            )

            pr_number, pr_url = await self._github.create_pull_request(
                self._owner, self._repo,
                title=f"fix({file_path.split('/')[-1]}): {event.title}",
                body=pr_body,
                head=branch_name,
                base=default_branch,
                labels=["bug", "ai-generated-fix", "awaiting-review"],
            )
            steps.append(f"✓ Created PR #{pr_number}: {pr_url}")
            logger.info("[FixGen] Created PR #%d: %s", pr_number, pr_url)

        except GitHubError as exc:
            steps.append(f"✗ GitHub error during PR creation: {exc}")
            logger.error("[FixGen] GitHub error: %s", exc)
            return _fail(str(exc), issue_url, branch_name)

        return FixResult(
            issue_url=issue_url,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch_name,
            fix_description=f"Fix applied to {function_name} in {file_path}",
            files_changed=[file_path, default_test_path] if test_added else [file_path],
            test_added=test_added,
            commit_sha=commit_sha,
        ), steps

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _parse_stack_trace(self, incident: IncidentState) -> tuple[str | None, str | None]:
        """
        Extract the first relevant file path and function name from a stack trace
        in the error description or diagnosis.

        Handles Node.js format:
          at functionName (/app/routes/helper/file.js:42:5)
          at /app/routes/helper/file.js:42:5

        Returns (repo_relative_path, function_name_or_None).
        """
        text = f"{incident.error_event.description or ''}\n{incident.diagnosis or ''}"

        # Match Node.js stack frames
        frame_re = re.compile(
            r"at\s+"
            r"(?:([\w.<>$]+(?:\.[\w.<>$]+)*)\s+\()?"   # optional: functionName (
            r"([^\s()]+\.(?:js|ts|jsx|tsx|py|rb|go))"   # file path with extension
            r":\d+(?::\d+)?\)?",                         # :line or :line:col
            re.MULTILINE,
        )

        _SKIP = ("node:internal", "node_modules", "internal/process", "<anonymous>",
                 "node:events", "node:stream", "timers")
        _CONTAINER_PREFIXES = ("/app/", "/usr/src/app/", "/home/app/", "/srv/app/")

        for m in frame_re.finditer(text):
            fn_name = m.group(1)
            raw_path = m.group(2).strip()

            if any(s in raw_path for s in _SKIP):
                continue

            # Strip Docker/container absolute prefix to get repo-relative path
            for prefix in _CONTAINER_PREFIXES:
                if raw_path.startswith(prefix):
                    raw_path = raw_path[len(prefix):]
                    break

            # Reject remaining absolute paths — they can't be in the repo
            if raw_path.startswith("/"):
                continue

            logger.info("[FixGen] Stack trace → file=%s  fn=%s", raw_path, fn_name)
            return raw_path, fn_name

        return None, None

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
                system="Return only the function name as a single word. No explanation.",
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
          1. Stack trace parsing — exact path from log, no LLM needed
          2. GitHub Code Search — search file contents for the error type string
          3. Keyword search + full server file list → LLM picks from real candidates
        """
        event = incident.error_event

        default_branch = "main"
        try:
            default_branch = await self._github.get_default_branch(self._owner, self._repo)
        except Exception:
            pass

        # ── 1. Stack trace (fastest, most accurate) ────────────────────
        st_path, st_fn = self._parse_stack_trace(incident)
        if st_path:
            # Validate the path actually exists in the repo
            try:
                await self._github.get_file_contents(self._owner, self._repo, st_path, ref=default_branch)
                logger.info("[FixGen] Stack trace resolved: %s → %s", st_path, st_fn)
                # If no function name from stack trace, still pass it to let LLM figure out fn
                return st_path, st_fn or "the function handling this error"
            except Exception:
                logger.info("[FixGen] Stack trace path '%s' not found in repo — falling back to search", st_path)

        # ── 2. GitHub Code Search — search file contents for the error type ──
        error_query = (incident.error_event.error_type or "").replace("_", " ")
        if error_query:
            code_results = await self._github.search_code(self._owner, self._repo, error_query)
            if code_results:
                top = code_results[0]
                logger.info("[FixGen] Code search resolved: %s (fragment: %s…)", top["path"], top["fragment"][:60])
                # Ask LLM for function name only — file path is confirmed real
                fn = await self._resolve_function_name(top["path"], top["fragment"], incident)
                return top["path"], fn

        # ── 3. Keyword search + full server file list ──────────────────
        keywords = self._extract_keywords(incident)
        logger.info("[FixGen] Searching repo with keywords: %s", keywords)

        _SKIP = ("node_modules", "package-lock", ".min.", "dist/", "build/", ".test.", ".spec.",
                 ".ttf", ".woff", ".png", ".jpg", ".svg", ".ico", "README", ".npmrc", ".env")
        _SOURCE_EXTS = (".js", ".ts", ".jsx", ".tsx", ".py", ".rb", ".go", ".java", ".cs")
        _CLIENT_DIRS = ("client/", "frontend/", "public/", "static/", "assets/")

        def _is_source(path: str) -> bool:
            return (
                not any(x in path for x in _SKIP)
                and any(path.endswith(e) for e in _SOURCE_EXTS)
            )

        def _is_server(path: str) -> bool:
            return _is_source(path) and not any(path.startswith(d) for d in _CLIENT_DIRS)

        # Always start with the full list of server-side source files
        all_files = await self._github.search_files_by_keyword(
            self._owner, self._repo, "", ref=default_branch
        )
        all_server = [f for f in all_files if _is_server(f)]

        # Promote keyword matches to the top so the LLM sees the most relevant files first
        keyword_matches: list[str] = []
        for kw in keywords:
            for f in all_server:
                if kw in f.lower() and f not in keyword_matches:
                    keyword_matches.append(f)

        # keyword matches first, then remaining server files
        candidates = keyword_matches + [f for f in all_server if f not in keyword_matches]
        candidates = candidates[:50]

        logger.info("[FixGen] %d server source file(s) for LLM (%d keyword matches): %s",
                    len(candidates), len(keyword_matches), keyword_matches[:5] or candidates[:5])

        candidate_section = ""
        if candidates:
            listed = "\n".join(f"  {p}" for p in candidates)
            candidate_section = (
                f"\nAll server-side source files in the repo (files listed first are keyword matches "
                f"— you MUST choose file_path from this list):\n{listed}"
            )

        prompt = (
            f"A production incident needs a code fix. Based on the details below, "
            f"identify the exact file path and function name that needs to be changed.\n\n"
            f"Title: {event.title}\n"
            f"Error type: {event.error_type or 'unknown'}\n"
            f"Description: {event.description or 'none'}\n"
            f"Root cause: {incident.diagnosis or 'none'}\n"
            f"{candidate_section}\n"
            f'Return ONLY a JSON object: {{"file_path": "...", "function_name": "..."}}\n'
            f"No explanation, no markdown."
        )
        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a software engineer. Return only valid JSON with file_path and function_name.",
            )
            response = re.sub(r"^```(?:json)?\n?", "", response.strip())
            response = re.sub(r"\n?```$", "", response)
            data = json.loads(response.strip())
            file_path = data["file_path"]
            function_name = data["function_name"]

            # Reject hallucinated paths — LLM must pick from the real candidate list
            if candidates and file_path not in candidates:
                logger.warning(
                    "[FixGen] LLM returned '%s' which is not in the candidate list — rejecting",
                    file_path,
                )
                # Fall back to the top keyword match if available, otherwise fail cleanly
                if keyword_matches:
                    logger.info("[FixGen] Falling back to top keyword match: %s", keyword_matches[0])
                    return keyword_matches[0], function_name
                return None, None

            return file_path, function_name
        except Exception as exc:
            logger.error("[FixGen] Failed to resolve target file/function: %s", exc)
            return None, None

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
    ) -> bool:
        """Generate a test for the fixed function and commit it to branch_name."""
        try:
            test_code = await self._generate_test(new_function, incident)
        except Exception as exc:
            steps.append(f"⚠ Test generation (LLM) failed: {exc}")
            logger.warning("[FixGen] Test LLM error: %s", exc)
            return False

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
            return True
        except GitHubError as exc:
            steps.append(f"⚠ Test commit failed (continuing): {exc}")
            logger.warning("[FixGen] Test commit error: %s", exc)
            return False

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
            system=(
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

    async def _generate_fix(self, content: str, function_name: str, incident: IncidentState) -> tuple[str, str]:
        """
        Ask the LLM to locate the function in the file and return (old_text, fixed_text).

        Passes the full file content so the LLM can handle any function syntax —
        arrow functions, object methods, class methods, exports, etc.
        Returns ("", "") if the function cannot be located.
        """
        prompt = (
            f"You are fixing a production bug. The file below contains the function "
            f"'{function_name}' (or the closest handler for this error).\n\n"
            f"DIAGNOSIS: {incident.diagnosis}\n"
            f"ERROR TYPE: {incident.error_event.error_type or 'unknown'}\n\n"
            f"FILE CONTENT:\n{content}\n\n"
            f"Instructions:\n"
            f"1. Find the function or handler responsible for this error (named '{function_name}' or similar).\n"
            f"2. Apply the minimal change needed to fix the root cause.\n"
            f"3. Return a JSON object with exactly two keys:\n"
            f'   - "old": the exact verbatim text of the function as it appears in the file\n'
            f'   - "new": the complete fixed version\n'
            f"The 'old' value must be copy-pasted exactly from the file — no changes to whitespace or quotes.\n"
            f"Return ONLY the JSON object. No explanation, no markdown fences."
        )

        try:
            response = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a software engineer. Return only valid JSON with 'old' and 'new' keys.",
            )
            response = re.sub(r"^```(?:json)?\n?", "", response.strip())
            response = re.sub(r"\n?```$", "", response)
            data = json.loads(response.strip())
            old_function = data.get("old", "")
            new_function = data.get("new", "")
        except Exception as exc:
            logger.error("[FixGen] LLM fix generation failed: %s", exc)
            return "", ""

        if not old_function:
            logger.error("[FixGen] LLM returned empty 'old' function for %s", function_name)
            return "", ""

        # Verify old_function actually appears in the file before returning
        if old_function not in content and old_function.strip() not in content:
            logger.error("[FixGen] LLM-returned 'old' text not found verbatim in file — likely hallucinated")
            return "", ""

        logger.info("[FixGen] Generated fix (old=%d chars, new=%d chars)", len(old_function), len(new_function))
        return old_function, new_function.strip()
