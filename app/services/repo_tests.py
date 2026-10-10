"""
Run a repository's existing tests around a fix (the fix harness's test mode).

Production's fix agent validates every fix in a sandbox and regenerates it from
the test failures (fix_generation.py, step 3d). Offline evals stop before that
step (patch_only), so SWE-bench runs measured a configuration production never
runs. Test mode closes the gap for evals: in the case's own SWE-bench image
(app/services/test_sandbox.py) it

  1. picks existing test files likely to exercise the change (score_test_file:
     the module's name, its area and the changed function in the test's path,
     and mentions of the changed function or class),
  2. runs them on the unchanged repo (the baseline: some tests fail already),
  3. runs them again with the fix and reports tests that passed before and
     don't now. Those are the fix's regressions.

Only tests already in the repository are used. SWE-bench's grading tests are
added by the harness at grading time and are not in the image.

Test commands follow SWE-bench's per-repo specs (swebench.harness.constants:
Django's runtests.py, sympy's bin/test, pytest elsewhere); result parsing is a
reduced version of swebench.harness.log_parsers.
"""
from __future__ import annotations

import math
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.services.test_sandbox import TestSandbox, trim_output

PASSING = {"PASSED", "XFAIL"}

_DJANGO_CMD = "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
_SYMPY_CMD = "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose"
_PYTEST_CMD = "pytest -rA --tb=short -p no:cacheprovider"


def framework_for(repo: str) -> str:
    """'django', 'sympy' or 'pytest' for an owner/name repo."""
    name = repo.lower()
    if name == "django/django":
        return "django"
    if name == "sympy/sympy":
        return "sympy"
    return "pytest"


def _is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix != ".py" or p.name in ("conftest.py", "__init__.py"):
        return False
    return (p.name.startswith("test_") or p.name == "tests.py"
            or any(part in ("tests", "test", "testing") for part in p.parts[:-1]))


def module_stem(path: str) -> str:
    """Name a test file for this module would use: query.py -> query,
    _logistic.py -> logistic, k_means_.py -> k_means, pkg/__init__.py -> pkg."""
    p = PurePosixPath(path)
    stem = p.parent.name if p.stem == "__init__" else p.stem
    return stem.strip("_")


def search_symbol(function_name: str | None) -> str | None:
    """The identifier to look for in test files: the method or function name, or
    the class for dunder methods. None if nothing specific enough is left (also
    for prose such as "the function handling this error")."""
    if not function_name:
        return None
    parts = [x for x in re.split(r"[.:]", function_name.strip()) if x]
    for name in reversed(parts):
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            return None
        if not (name.startswith("__") and name.endswith("__")) and len(name) >= 4:
            return name
    return None


def _distance(a: str, b: str) -> int:
    pa, pb = PurePosixPath(a).parent.parts, PurePosixPath(b).parent.parts
    common = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        common += 1
    return len(pa) + len(pb) - 2 * common


_TEST_TREES = ("tests", "test", "testing")
_GENERIC = {"test", "tests", "testing", "src", "lib", "init", "core", "base", "util", "utils", "main",
            "get", "set", "sql", "the", "handling", "this", "error", "function", "common", "misc"}


def _key(word: str) -> str:
    """Loose spelling key: models/model, transforms/transformations, ufunc/ufuncs."""
    w = word.lower()
    if len(w) > 4 and w.endswith("s"):
        w = w[:-1]
    return w[:7]


def _tokens(parts) -> set[str]:
    out = set()
    for part in parts:
        for t in re.split(r"[_\W]+", str(part)):
            if len(t) >= 3 and t.lower() not in _GENERIC:
                out.add(_key(t))
    return out


def _nearby(changed_path: str, test_path: str) -> bool:
    """A test file in a repo-level test tree (tests/, testing/), or in the same
    subpackage as the change (sharing its first two directories). Keeps a
    generic name like core.py from matching test_core.py across a whole repo."""
    tp = PurePosixPath(test_path).parts
    if tp and tp[0] in _TEST_TREES:
        return True
    cp = PurePosixPath(changed_path).parent.parts
    common = 0
    for x, y in zip(cp, tp[:-1]):
        if x != y:
            break
        common += 1
    return common >= min(2, len(cp))


def _area(changed_path: str) -> list[str]:
    """Directories below the top-level package: django/db/models/fields -> db, models, fields."""
    parts = [p for p in PurePosixPath(changed_path).parent.parts if p not in ("src", "lib")]
    return parts[1:]


def score_test_file(changed_path: str, function_name: str | None, test_path: str, hits: int) -> float:
    """How likely a test file exercises a change. Signals, all general:
    the module's own test file (test_query.py), the module name in the test's
    name or folder (intermediate_rotation_transforms -> test_intermediate_
    transformations; autodoc/__init__.py -> test_ext_autodoc_configs.py), the
    change's area in the test's path (db/models/fields -> tests/model_fields/;
    impute/ -> test_impute.py), the function name in the test's path, and how
    often the test file mentions the changed function or class."""
    t = PurePosixPath(test_path)
    stem = module_stem(changed_path)
    t_dirs = _tokens(p for p in t.parent.parts if p not in _TEST_TREES)
    t_name = _tokens([t.stem])
    where = t_dirs | t_name
    score = 0.0
    if t.stem in (f"test_{stem}", f"tests_{stem}", f"test{stem}"):
        score += 4
    score += 2 * len(_tokens([stem]) & where)
    score += len(_tokens(_area(changed_path)) & where)
    fn = search_symbol(function_name)
    if fn:
        score += len(_tokens([fn]) & where)
    if hits > 0:
        score += min(3.0, math.log2(1 + hits))
    return score


def select_test_files(changed_path: str, test_files: list[str], symbol_hits: dict[str, int],
                      max_files: int, function_name: str | None = None) -> list[str]:
    """The highest-scoring test files for a change (score_test_file). Files in a
    repo-wide test tree need more evidence (score 2) than files in the change's
    own subpackage (score 1). If none qualify, a package-wide test file
    (requests/models.py -> test_requests.py)."""
    scored = []
    for f in test_files:
        if not _is_test_file(f) or not _nearby(changed_path, f):
            continue
        sc = score_test_file(changed_path, function_name, f, symbol_hits.get(f, 0))
        need = 2.0 if PurePosixPath(f).parts[0] in _TEST_TREES else 1.0
        if sc >= need:
            scored.append((-sc, _distance(changed_path, f), f))
    out = [f for _, _, f in sorted(scored)]
    if not out:
        pkg = [part.strip("_") for part in PurePosixPath(changed_path).parts[:-1] if part != "src"][:1]
        out = sorted((f for f in test_files if pkg and PurePosixPath(f).stem == f"test_{pkg[0]}"),
                     key=lambda f: (_distance(changed_path, f), f))
    return out[:max(0, max_files)]


def django_label(path: str) -> str | None:
    """tests/queries/test_query.py -> queries.test_query (runtests.py labels)."""
    p = PurePosixPath(path)
    if len(p.parts) < 2 or p.parts[0] != "tests" or p.suffix != ".py":
        return None
    return ".".join(p.with_suffix("").parts[1:])


def test_command(framework: str, files: list[str]) -> str | None:
    if framework == "django":
        labels = [lbl for lbl in (django_label(f) for f in files) if lbl]
        return f"{_DJANGO_CMD} {' '.join(map(shlex.quote, labels))}" if labels else None
    if not files:
        return None
    base = _SYMPY_CMD if framework == "sympy" else _PYTEST_CMD
    return f"{base} {' '.join(map(shlex.quote, files))}"


_PYTEST_STATUS = ("PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS")
_DJANGO_NAME = re.compile(r"^\w+ \([\w.]+\)$")
_DJANGO_STATUS = {"ok": "PASSED", "OK": "PASSED", "FAIL": "FAILED", "ERROR": "ERROR",
                  "expected failure": "XFAIL", "unexpected success": "FAILED"}
_SYMPY_FILE = re.compile(r"^(\S+\.py)\[\d+\]")
_SYMPY_STATUS = {"ok": "PASSED", "F": "FAILED", "E": "ERROR", "f": "XFAIL", "X": "XPASS",
                 "s": "SKIPPED", "w": "SKIPPED"}


def parse_results(log: str, framework: str) -> dict[str, str]:
    """Test id -> PASSED / FAILED / ERROR / SKIPPED / XFAIL / XPASS."""
    out: dict[str, str] = {}
    if framework == "django":
        prev = ""
        for raw in log.splitlines():
            line = raw.strip()
            if " ... " in line:
                name, _, status = line.rpartition(" ... ")
                name = name if _DJANGO_NAME.match(name) else (prev if _DJANGO_NAME.match(prev) else name)
                status = status.strip()
                if status.startswith("skipped"):
                    out[name] = "SKIPPED"
                elif status in _DJANGO_STATUS:
                    out[name] = _DJANGO_STATUS[status]
            m = re.match(r"^(ERROR|FAIL): (\w+ \([\w.]+\))", line)
            if m:                       # failures summary, also import errors of a label
                out.setdefault(m.group(2), "ERROR" if m.group(1) == "ERROR" else "FAILED")
            prev = line
        return out
    if framework == "sympy":
        current = ""
        for raw in log.splitlines():
            line = raw.strip()
            m = _SYMPY_FILE.match(line)
            if m:
                current = m.group(1)
                continue
            parts = line.split()
            if len(parts) == 2 and parts[0].startswith("test_") and parts[1] in _SYMPY_STATUS:
                out[f"{current}::{parts[0]}"] = _SYMPY_STATUS[parts[1]]
        return out
    for raw in log.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
        head, _, rest = line.partition(" ")
        if head in _PYTEST_STATUS and rest:
            out[rest.split(" - ")[0].strip()] = head
    return out


def newly_failing(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Tests that passed before the fix and don't after it (missing counts: an
    import error stops a whole file from running)."""
    return sorted(t for t, s in before.items() if s in PASSING and after.get(t) not in PASSING)


def failure_excerpt(log: str, head: int = 1500, tail: int = 4500) -> str:
    """The run's output without the passing-test lines, trimmed to start and end."""
    keep = [ln for ln in log.splitlines()
            if not (ln.startswith("PASSED ") or ln.rstrip().endswith(" ... ok") or ln.rstrip().endswith(" ok"))]
    return trim_output("\n".join(keep), head=head, tail=tail)


@dataclass
class TestRun:
    command: str
    files: list[str]
    exit_code: int
    timed_out: bool
    seconds: float
    results: dict[str, str] = field(default_factory=dict)
    excerpt: str = ""
    infra_error: str | None = None

    @property
    def passing(self) -> int:
        return sum(1 for s in self.results.values() if s in PASSING)

    def summary(self, label: str, broken: list[str] | None = None) -> dict:
        counts: dict[str, int] = {}
        for s in self.results.values():
            counts[s] = counts.get(s, 0) + 1
        out = {"run": label, "files": self.files, "exit": self.exit_code, "timed_out": self.timed_out,
               "seconds": round(self.seconds, 1), "counts": counts, "infra_error": self.infra_error}
        if broken is not None:
            out["newly_failing"] = broken[:50]
        return out


async def find_test_files(sandbox: TestSandbox, changed_path: str, function_name: str | None,
                          max_files: int) -> list[str]:
    """Pick test files in the sandbox's repo for a change to `changed_path`."""
    code, listing = await sandbox.run("git ls-files -- '*.py'", timeout=60, trim=False)
    if code != 0:
        return []
    test_files = [f for f in listing.splitlines() if _is_test_file(f.strip())]
    hits: dict[str, int] = {}
    symbol = search_symbol(function_name)
    if symbol and test_files:
        # Grep every tracked .py file inside the sandbox and keep the test files
        # here: passing the file list as an argument can exceed Modal's 64 KB
        # exec-argument cap (Django has ~1,800 test files).
        cmd = (f"git ls-files -z -- '*.py' | xargs -0 grep -c -w -- {shlex.quote(symbol)} "
               f"2>/dev/null | grep -v ':0$' || true")
        _, out = await sandbox.run(cmd, timeout=120, trim=False)
        wanted = set(test_files)
        for line in out.splitlines():
            path, _, n = line.rpartition(":")
            if path in wanted and n.isdigit():
                hits[path] = int(n)
    return select_test_files(changed_path, test_files, hits, max_files, function_name)


async def run_tests(sandbox: TestSandbox, framework: str, files: list[str], timeout: int) -> TestRun:
    command = test_command(framework, files) or ""
    if not command:
        return TestRun(command="", files=files, exit_code=-1, timed_out=False, seconds=0.0,
                       infra_error="no runnable test command for these files")
    t0 = time.monotonic()
    code, log = await sandbox.run(command, timeout=timeout, trim=False)
    run = TestRun(command=command, files=files, exit_code=code, timed_out=code == 124,
                  seconds=time.monotonic() - t0, results=parse_results(log, framework),
                  excerpt=failure_excerpt(log))
    if code == -1 and log.startswith("[sandbox error"):
        run.infra_error = log[:300]
    return run
