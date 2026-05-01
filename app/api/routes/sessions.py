from fastapi import APIRouter

from app.services.session_logger import session_logger

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("/recent")
async def get_recent_sessions(n: int = 20) -> list[dict]:
    """Return the n most recent completed agent session records."""
    return session_logger.recent(n)
