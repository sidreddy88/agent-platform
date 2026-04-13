"""
Monitor generation API.

GET  /monitors/generated   list recently generated monitor configs (newest first)
GET  /monitors/coverage    aggregate coverage metrics across all runs
POST /monitors/generate    manually trigger monitor generation for a specific PR
"""
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException

from app.services.monitor_store import monitor_store

router = APIRouter(prefix="/monitors", tags=["monitors"])


@router.get("/generated")
async def get_generated() -> List[Dict[str, Any]]:
    """List recently generated monitor configs, newest first (up to 100)."""
    return monitor_store.get_all()


@router.get("/coverage")
async def get_coverage() -> Dict[str, Any]:
    """Aggregate coverage metrics across all monitor generation runs."""
    return monitor_store.coverage_metrics()


@router.post("/generate")
async def generate_monitors(
    owner: str = Body(..., embed=True),
    repo: str = Body(..., embed=True),
    pr_number: int = Body(..., embed=True),
) -> Dict[str, Any]:
    """
    Manually trigger monitor generation for a specific merged PR.

    Runs synchronously — waits for the agent to complete and returns the result.
    Requires GITHUB_TOKEN and ANTHROPIC_API_KEY to be set.
    """
    try:
        from app.agents.monitor_generation import MonitorGenerationAgent
        agent = MonitorGenerationAgent()
        result = await agent.generate_monitors(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
        )
    except ValueError as exc:
        # Raised when GITHUB_TOKEN or ANTHROPIC_API_KEY is not configured
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    full_repo = f"{owner}/{repo}"
    monitor_store.save(repo=full_repo, pr_number=pr_number, result=result)

    return {
        "pr_number": result.pr_number,
        "repo": result.repo,
        "files_analyzed": result.files_analyzed,
        "monitors_created": result.monitors_created,
        "coverage_ratio": result.coverage_ratio,
        "dry_run": result.dry_run,
        "monitors": [
            {
                "monitor_type": m.monitor_type,
                "file": m.file,
                "name": m.name,
                "config": m.config,
                "created": m.created,
            }
            for m in result.monitors
        ],
    }
