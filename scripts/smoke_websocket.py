"""
Quick smoke test for the /ws/chat WebSocket endpoint. Requires the server
running locally (npm run dev) — not a pytest test, run manually.
Run: python scripts/smoke_websocket.py
"""
import asyncio
import json

import websockets


async def test():
    async with websockets.connect("ws://localhost:8000/ws/chat") as ws:
        await ws.send(json.dumps({"message": "Hello, who are you?"}))
        while True:
            response = await ws.recv()
            data = json.loads(response)
            if data.get("type") == "chunk":
                print(data["content"], end="", flush=True)
            elif data.get("type") == "done":
                print("\n--- Done ---")
                break

asyncio.run(test())