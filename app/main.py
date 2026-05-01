import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import websocket
from app.api.routes import agents as agents_routes
from app.api.routes import approvals, dashboard, health, incidents, logs, webhooks
from app.api.routes import circuit_breaker as circuit_breaker_routes
from app.api.routes import debug as debug_routes
from app.api.routes import drift as drift_routes
from app.api.routes import evals as evals_routes
from app.api.routes import events as events_routes
from app.api.routes import injection as injection_routes
from app.api.routes import metrics as metrics_routes
from app.api.routes import monitors as monitors_routes
from app.api.routes import orchestrator as orchestrator_routes
from app.api.routes import sessions as sessions_routes
from app.api.websocket_dashboard import router as ws_dashboard_router
from app.services.database import init_db
from app.services.drift_detector import drift_detector
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
    # Detection runs on-demand via POST /incidents/scan, not on a background loop
    # asyncio.create_task(detection_service.run_forever())
    asyncio.create_task(threshold_monitor.run_forever())
    asyncio.create_task(orchestrator.run_forever())
    asyncio.create_task(drift_detector.run_forever())
    asyncio.create_task(_agent_status_broadcaster())


@app.on_event("shutdown")
async def _shutdown():
    # detection_service.stop()
    threshold_monitor.stop()
    orchestrator.stop()
    drift_detector.stop()
