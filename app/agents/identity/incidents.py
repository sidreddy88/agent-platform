"""Incident response worker: triages incidents; service restarts need a human's approval.

Run (with worker-gate's scripts/run_all.sh running, and the worker deployed for the tenant):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.incidents --tenant acme --obo <owner> "<request>"
"""
from app.agents.identity.base import IdentityWorker, main


class IncidentsWorker(IdentityWorker):
    """Incident response worker: triages incidents; service restarts need a human's approval."""
    role = "incidents"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._required_tool_names_before_answer = {"get_service_status"}


if __name__ == "__main__":
    main(IncidentsWorker)
