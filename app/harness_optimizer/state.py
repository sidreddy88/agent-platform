"""
Durable run state for the harness optimizer.

An optimizer run lasts hours and spends real money: every evaluation of a
candidate is ~20 DiagnosisAgent replays. A crash, a cancelled job or a laptop
going to sleep must never lose paid-for work or repeat it. So:

- Everything the loop knows lives in run_dir/state.json, rewritten
  atomically (write a temp file, fsync, rename) after every phase change.
- Every evaluation result is written to run_dir/evals/<key>.json the moment
  it finishes, keyed by (harness content hash, cases, trials), and a resume
  reads it back instead of re-running it.
- Candidate harness directories are materialised under run_dir/candidates/
  and never mutated after evaluation, so a resumed run evaluates exactly the
  bytes it was going to.

Layout:
    run_dir/
      state.json          RunState, the checkpoint
      history.jsonl       one line per judged candidate (history.py): the memory
      evals/<key>.json    cached EvalResults
      candidates/rN/      candidate harness directories
      incumbent/          the current best harness
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Where a round is in its life. A resume restarts the current phase from its
# beginning; every phase is idempotent given the cached evals and files.
PHASES = ("propose", "screen", "evaluate", "decide", "done")


@dataclass
class RunState:
    run_id: str
    config: dict
    round: int = 0                      # 0 = evaluate the starting harness
    phase: str = "evaluate"
    S_star: float | None = None         # best evolve-set score accepted so far
    delta: float | None = None          # noise band; None until calibrated
    incumbent_eval: str | None = None   # evals/<key> of the incumbent
    candidate: dict | None = None       # the in-flight candidate of this round
    accepted: list[str] = field(default_factory=list)   # candidate ids, in order
    stalled_rounds: int = 0             # consecutive rounds with no acceptance
    spent_usd: float = 0.0              # measured spend so far (cost meter), survives resume
    stop_reason: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class RunDir:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    @property
    def incumbent_dir(self) -> Path:
        return self.root / "incumbent"

    def candidate_dir(self, round_no: int) -> Path:
        return self.root / "candidates" / f"r{round_no}"

    def exists(self) -> bool:
        return self.state_path.exists()

    def save(self, state: RunState) -> None:
        _atomic_write(self.state_path, json.dumps(state.to_json(), indent=1, sort_keys=True))

    def load(self) -> RunState:
        return RunState(**json.loads(self.state_path.read_text()))

    def eval_path(self, key: str) -> Path:
        return self.root / "evals" / f"{key}.json"

    def save_eval(self, key: str, data: dict) -> None:
        _atomic_write(self.eval_path(key), json.dumps(data, indent=1, sort_keys=True))

    def load_eval(self, key: str) -> dict | None:
        p = self.eval_path(key)
        return json.loads(p.read_text()) if p.exists() else None
