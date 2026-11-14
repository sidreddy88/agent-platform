"""Onboarding worker: sets up new hires (account, groups, standard apps).

Run (with worker-gate's scripts/run_all.sh running):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \\
  python -m app.agents.identity.onboarding --tenant acme --obo alice "New hire: Gina Grant (gina), finance"
"""
from app.agents.identity.base import IdentityWorker, main


class OnboardingWorker(IdentityWorker):
    """Creates accounts for new hires with their team's groups and apps."""
    role = "onboarding"


if __name__ == "__main__":
    main(OnboardingWorker)
