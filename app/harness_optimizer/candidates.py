"""
Candidate harnesses: copies of a harness directory with one edit applied.

The edit surface is bounded to the files that already exist in the harness
(app/agents/harness/diagnosis/): an edit may rewrite them, never add or
delete files. Before anything is spent evaluating a candidate, it must pass
structural validation, so a candidate that can't even load (bad JSON, a
template placeholder the code never fills, a removed tool description, a
turn budget of 500) is rejected for free. RRSI does the same with its
"deterministic compile/constructor/smoke checks" after the critic.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import string
from pathlib import Path

IGNORED = {"README.md"}         # documentation, never loaded, never edited

# Allowed ranges for numeric settings. Keeps an edit from "saving cost" by
# starving the agent (max_iterations=1) or blowing the budget (a 1M-char read).
SETTING_BOUNDS = {
    "max_iterations": (5, 25),
    "file_read_char_limit": (2000, 60000),
    "grep_max_matches": (10, 200),
}


class InvalidCandidate(ValueError):
    pass


def harness_files(directory: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(Path(directory).iterdir())
            if p.is_file() and p.name not in IGNORED}


def content_hash(directory: Path) -> str:
    h = hashlib.sha256()
    for name, text in harness_files(directory).items():
        h.update(name.encode() + b"\0" + text.encode() + b"\0")
    return h.hexdigest()[:16]


def _placeholders(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def apply_edit(base: Path, dest: Path, files: dict[str, str]) -> None:
    """dest = copy of base, with `files` (name -> full new content) overwritten."""
    base_files = harness_files(base)
    unknown = sorted(set(files) - set(base_files))
    if unknown:
        raise InvalidCandidate(f"edit touches files outside the harness surface: {unknown}")
    if not any(files[n] != base_files[n] for n in files):
        raise InvalidCandidate("edit changes nothing")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base, dest)
    for name, text in files.items():
        (dest / name).write_text(text)


def validate(base: Path, candidate: Path) -> None:
    """Raise InvalidCandidate unless `candidate` is loadable and in bounds."""
    b, c = harness_files(base), harness_files(candidate)
    if set(b) != set(c):
        raise InvalidCandidate(f"file set changed: {sorted(set(b) ^ set(c))}")
    try:
        b_set, c_set = json.loads(b["settings.json"]), json.loads(c["settings.json"])
        b_tools, c_tools = json.loads(b["tool_descriptions.json"]), json.loads(c["tool_descriptions.json"])
    except json.JSONDecodeError as exc:
        raise InvalidCandidate(f"invalid JSON: {exc}") from exc
    if set(c_set) != set(b_set):
        raise InvalidCandidate(f"settings keys changed: {sorted(set(b_set) ^ set(c_set))}")
    for key, value in c_set.items():
        if key.startswith("_"):
            continue
        if type(value) is not type(b_set[key]):
            raise InvalidCandidate(f"setting {key} changed type")
        lo_hi = SETTING_BOUNDS.get(key)
        if lo_hi and not lo_hi[0] <= value <= lo_hi[1]:
            raise InvalidCandidate(f"setting {key}={value} outside {lo_hi}")
    if set(c_tools) != set(b_tools):
        raise InvalidCandidate(f"tool set changed: {sorted(set(b_tools) ^ set(c_tools))}")
    if any(not str(v).strip() for v in c_tools.values()):
        raise InvalidCandidate("a tool description is empty")
    for name in b:
        if not name.endswith(".prompt"):
            continue
        try:
            extra = _placeholders(c[name]) - _placeholders(b[name])
        except ValueError as exc:       # unbalanced braces
            raise InvalidCandidate(f"{name}: malformed template: {exc}") from exc
        if extra:
            raise InvalidCandidate(f"{name}: uses placeholders the code never fills: {sorted(extra)}")


def diff(base: Path, candidate: Path) -> str:
    b, c = harness_files(base), harness_files(candidate)
    out = []
    for name in sorted(b):
        if b[name] != c.get(name):
            out.extend(difflib.unified_diff(b[name].splitlines(keepends=True),
                                            c.get(name, "").splitlines(keepends=True),
                                            f"a/{name}", f"b/{name}"))
    return "".join(out)


def changed_files(base: Path, candidate: Path) -> list[str]:
    b, c = harness_files(base), harness_files(candidate)
    return sorted(n for n in b if b[n] != c.get(n))
