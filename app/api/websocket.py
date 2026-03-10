import json

import anthropic
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.llm import LLMService

router = APIRouter()
llm = LLMService()


@router.websocket("/ws/chat")
async def chat(websocket: WebSocket) -> None:
    await websocket.accept()
    history: list[dict] = []

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
                user_text = data["message"]
            except (json.JSONDecodeError, KeyError):
                await websocket.send_text(
                    json.dumps({"type": "error", "content": "Invalid payload. Expected {\"message\": \"...\"}"})
                )
                continue

            history.append({"role": "user", "content": user_text})

            full_reply = []
            try:
                async for chunk in llm.stream_chat(messages=history):
                    full_reply.append(chunk)
                    await websocket.send_text(json.dumps({"type": "chunk", "content": chunk}))
            except anthropic.APIStatusError as e:
                history.pop()
                await websocket.send_text(
                    json.dumps({"type": "error", "content": f"Claude API error {e.status_code}: {e.message}"})
                )
                continue
            except anthropic.APIConnectionError:
                history.pop()
                await websocket.send_text(
                    json.dumps({"type": "error", "content": "Could not reach Claude API. Check your connection."})
                )
                continue

            assistant_message = "".join(full_reply)
            history.append({"role": "assistant", "content": assistant_message})

            await websocket.send_text(json.dumps({"type": "done", "content": assistant_message}))

    except WebSocketDisconnect:
        pass
