"""
Run the official SWE-bench harness (swebench 4.0.3) on an Apple-silicon Mac
with the published x86_64 images.

On arm64 the harness picks arm64 images, which aren't published, so it builds
every environment locally: slow, and it saturates the machine. Reporting
x86_64 makes it pull swebench/sweb.eval.x86_64.* (with --namespace swebench),
which Docker runs under Rosetta. Every argument is passed straight through:

    ~/.venvs/swebench/bin/python scripts/swebench_eval_x86.py \\
        --dataset_name princeton-nlp/SWE-bench_Verified --predictions_path preds.jsonl \\
        --run_id fix-pilot --namespace swebench --max_workers 2
"""
import platform
import runpy

platform.machine = lambda: "x86_64"
runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
