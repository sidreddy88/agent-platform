"""Access worker: handles requests to give people access to apps and groups.

Run (with worker-gate's scripts/run_all.sh running):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.access --tenant acme --obo alice "Give bob access to github"
"""
from app.agents.identity.base import IdentityWorker, main


class AccessWorker(IdentityWorker):
    """Grants app and group access within policy."""
    role = "access"


if __name__ == "__main__":
    main(AccessWorker)
