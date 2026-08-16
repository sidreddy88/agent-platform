import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import websocket
from app.api.routes import agents as agents_routes
from app.api.routes import approvals, dashboard, health, incidents, logs, webhooks
from app.api.routes import circuit_breaker as circuit_breaker_routes
from app.api.routes import debug as debug_routes
from app.api.routes import drift as drift_routes
from app.api.routes import evals as evals_routes
from app.api.routes import events as events_routes
from app.api.routes import failures as failures_routes
from app.api.routes import injection as injection_routes
from app.api.routes import metrics as metrics_routes
from app.api.routes import monitors as monitors_routes
from app.api.routes import orchestrator as orchestrator_routes

# from app.api.routes import performance as performance_routes  # disabled — Atlas-backed
from app.api.routes import sessions as sessions_routes
from app.api.websocket_dashboard import router as ws_dashboard_router
from app.services.database import init_db
from app.services.detection import detection_service
from app.services.drift_detector import drift_detector
from app.services.model_config import validate_models_live
from app.services.orchestrator import orchestrator
from app.services.threshold_monitor import threshold_monitor

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Agent Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# In dev, vite proxies `/api/*` to the backend and rewrites away the `/api`
# prefix. In production, the same compiled frontend bundle calls
# `/api/dashboard`, `/api/agents/...`, etc. — but FastAPI registers those
# routes without a prefix. Strip `/api` here so the production bundle
# works without changing the frontend code.
@app.middleware("http")
async def _strip_api_prefix(request, call_next):
    path = request.scope["path"]
    if path.startswith("/api/") or path == "/api":
        new_path = path[4:] or "/"
        request.scope["path"] = new_path
        if "raw_path" in request.scope:
            request.scope["raw_path"] = new_path.encode()
    return await call_next(request)

# Routes
app.include_router(health.router)
app.include_router(websocket.router)
app.include_router(ws_dashboard_router)
app.include_router(approvals.router)
app.include_router(dashboard.router)
app.include_router(incidents.router)
app.include_router(events_routes.router)
app.include_router(webhooks.router)
app.include_router(logs.router)
app.include_router(orchestrator_routes.router)
app.include_router(metrics_routes.router)
app.include_router(circuit_breaker_routes.router)
app.include_router(injection_routes.router)
app.include_router(drift_routes.router)
app.include_router(evals_routes.router)
app.include_router(monitors_routes.router)
app.include_router(agents_routes.router)
app.include_router(debug_routes.router)
app.include_router(sessions_routes.router)
app.include_router(failures_routes.router)
# app.include_router(performance_routes.router)  # disabled — Atlas-backed


# Frontend dashboard — bundled into the Docker image at frontend/dist by the
# multi-stage build. Mounted LAST so all API routers above take precedence.
# Local dev still uses `npm run dev` (vite on :4000 proxying to :8000) and
# this directory is absent — the mount is a no-op when missing.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(_FRONTEND_DIST), html=True),
        name="dashboard",
    )
else:
    logging.getLogger(__name__).info(
        "[Startup] %s not found — frontend bundle not served (running in dev mode?)",
        _FRONTEND_DIST,
    )


async def _backfill_rag_index() -> None:
    """Index all existing incidents that have a diagnosis + pr_url but were never indexed."""
    try:
        from app.services.incident_store import incident_store
        from app.services.rag import RAGService
        rag = RAGService()
        terminal = {"resolved", "noise", "duplicate", "rejected"}
        for incident in incident_store.list_all():
            if incident.diagnosis and incident.pr_url and incident.status.value not in terminal:
                await rag.index_incident(incident)
    except Exception as exc:
        logging.getLogger(__name__).debug("[Startup] RAG backfill skipped: %s", exc)


async def _agent_status_broadcaster() -> None:
    """
    Broadcast agent status to all WS clients.
    Polls at 0.5s while the pipeline is active, 3s when idle.
    """
    from app.api.websocket_dashboard import broadcast
    from app.services.agent_tracker import agent_tracker
    while True:
        try:
            snapshot = agent_tracker.snapshot()
            active = bool(snapshot["active_runs"]) or bool(snapshot["pipeline_activity"])
            await broadcast({"type": "agent_status", "agents": snapshot})
            await asyncio.sleep(0.5 if active else 3.0)
        except Exception:
            await asyncio.sleep(3.0)


@app.on_event("startup")
async def _startup():
    init_db()
    asyncio.create_task(detection_service.run_forever())
    asyncio.create_task(threshold_monitor.run_forever())
    asyncio.create_task(orchestrator.run_forever())
    asyncio.create_task(drift_detector.run_forever())
    asyncio.create_task(_agent_status_broadcaster())
    asyncio.create_task(_backfill_rag_index())
    asyncio.create_task(validate_models_live())


@app.on_event("shutdown")
async def _shutdown():
    detection_service.stop()
    threshold_monitor.stop()
    orchestrator.stop()
    drift_detector.stop()
