"""--diagnoses-from accepts a run directory or a saved .jsonl(.gz) file."""
import gzip
import json

from scripts.eval_swebench_fix import OUT, _diagnoses_path, _read_diagnoses


def test_run_name_resolves_under_runs_fix():
    assert _diagnoses_path("all500-run1") == OUT / "all500-run1"


def test_reads_gz_file_and_run_dir(tmp_path):
    line = json.dumps({"instance_id": "a__b-1", "diagnosis": {"full": {"affected_file": "x.py"}}}) + "\n"
    gz = tmp_path / "d.jsonl.gz"
    with gzip.open(gz, "wt") as f:
        f.write(line)
    assert _diagnoses_path(str(gz)) == gz
    assert _read_diagnoses(gz) == line
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.jsonl").write_text(line)
    assert _read_diagnoses(run) == line


def test_shipped_run1_diagnoses_cover_all_500():
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_run1_diagnoses.jsonl.gz"
    rows = [json.loads(x) for x in _read_diagnoses(src).splitlines() if x]
    assert len(rows) == 500 and all(r["diagnosis"]["full"] for r in rows)
