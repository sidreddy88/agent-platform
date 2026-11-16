"""Invoice processing worker: matches invoices to purchase orders; payment needs approval.

Run (with worker-gate's scripts/run_all.sh running, and the worker deployed for the tenant):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.invoices --tenant acme --obo <owner> "<request>"
"""
from app.agents.identity.base import IdentityWorker, main


class InvoicesWorker(IdentityWorker):
    """Invoice processing worker: matches invoices to purchase orders; payment needs approval."""
    role = "invoices"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._required_tool_names_before_answer = {"get_invoice", "get_purchase_order"}


if __name__ == "__main__":
    main(InvoicesWorker)
