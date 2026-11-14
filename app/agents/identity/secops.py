"""Security Ops worker: investigates sign-in alerts, records findings, suspends compromised users (with approval).

Run (with worker-gate's scripts/run_all.sh running):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.secops --tenant acme --obo it-lead "Alert: odd sign-ins for dave"
"""
from app.agents.identity.base import IdentityWorker, main


class SecOpsWorker(IdentityWorker):
    """Investigates sign-in alerts and contains compromised accounts."""
    role = "secops"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        # a finding must rest on the real sign-in log, not on the alert text
        self._required_tool_names_before_answer = {"get_signin_log"}


if __name__ == "__main__":
    main(SecOpsWorker)
