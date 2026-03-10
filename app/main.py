from fastapi import FastAPI
from app.api.routes import health
from app.api import websocket

app = FastAPI(title="Agent Platform")

app.include_router(health.router)
app.include_router(websocket.router)
