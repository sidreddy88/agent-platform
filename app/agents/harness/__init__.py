"""
Agent harnesses as directories of editable files.

A harness is everything around the frozen model that shapes how an agent
works: its prompt text, tool descriptions and a few numeric settings. Keeping
that surface in files (not Python string literals) makes it explicit and
bounded. The harness optimizer edits files in a copy of this directory,
diffs stay readable, and an agent can be pointed at a candidate harness
without any code change:

    load_harness("diagnosis")                         # app/agents/harness/diagnosis/
    load_harness("diagnosis", "/tmp/candidate-3")     # an explicit directory
    HARNESS_DIR_DIAGNOSIS=/tmp/candidate-3 ...        # same, via the environment

Layout of a harness directory:
    settings.json            numeric/string knobs (keys starting with "_" are docs)
    tool_descriptions.json   {tool_name: description shown to the model}
    *.prompt                 prompt templates, rendered with str.format(**values);
                             literal braces are written {{ }}. Not .md: the
                             Docker build's .dockerignore drops every *.md, so
                             the image would ship without its prompts.

Code decides *what* to fill in and *when* each template is used; the files
decide the wording. The optimizer's edit surface is exactly these files.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Harness:
    name: str
    directory: Path
    settings: dict
    tool_descriptions: dict[str, str]
    templates: dict[str, str]

    def render(self, template: str, **values) -> str:
        try:
            text = self.templates[template]
        except KeyError:
            raise KeyError(f"harness {self.name!r} ({self.directory}) has no template "
                           f"{template!r}.prompt") from None
        return text.format(**values)

    def tool_description(self, tool: str) -> str:
        try:
            return self.tool_descriptions[tool]
        except KeyError:
            raise KeyError(f"harness {self.name!r} ({self.directory}) has no description "
                           f"for tool {tool!r}") from None

    def setting(self, key: str):
        try:
            return self.settings[key]
        except KeyError:
            raise KeyError(f"harness {self.name!r} ({self.directory}) has no setting "
                           f"{key!r}") from None


def load_harness(name: str, directory: str | Path | None = None) -> Harness:
    """Load a harness by name. Resolution order: `directory` if given, then the
    HARNESS_DIR_<NAME> environment variable, then the built-in default."""
    root = Path(directory or os.environ.get(f"HARNESS_DIR_{name.upper()}")
                or DEFAULT_ROOT / name)
    if not root.is_dir():
        raise FileNotFoundError(f"harness {name!r}: directory {root} does not exist")
    settings = {k: v for k, v in json.loads((root / "settings.json").read_text()).items()
                if not k.startswith("_")}
    tools = json.loads((root / "tool_descriptions.json").read_text())
    templates = {p.stem: p.read_text() for p in sorted(root.glob("*.prompt"))}
    return Harness(name=name, directory=root, settings=settings,
                   tool_descriptions=tools, templates=templates)
