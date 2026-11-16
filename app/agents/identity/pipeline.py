"""Pipeline ops worker: keeps deal stages and activities current; external email needs approval.

Run (with worker-gate's scripts/run_all.sh running, and the worker deployed for the tenant):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.pipeline --tenant acme --obo <owner> "<request>"
"""
from app.agents.identity.base import IdentityWorker, main


class PipelineWorker(IdentityWorker):
    """Pipeline ops worker: keeps deal stages and activities current; external email needs approval."""
    role = "pipeline"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._required_tool_names_before_answer = {"get_deal"}


if __name__ == "__main__":
    main(PipelineWorker)
