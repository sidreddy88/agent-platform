import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import health, approvals
from app.api.routes import dashboard, incidents, webhooks
from app.api import websocket
from app.api.websocket_dashboard import router as ws_dashboard_router
from app.services.detection import detection_service

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
app.include_router(webhooks.router)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(detection_service.run_forever())


@app.on_event("shutdown")
async def _shutdown():
    detection_service.stop()
