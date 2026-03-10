from collections.abc import AsyncIterator

import anthropic

from app.core.config import settings

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4096


class LLMService:
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def complete(
        self,
        messages: list[dict],
        system: str | None = None,
    ) -> str:
        """Single blocking call — returns full response text. Use for agent loops."""
        kwargs: dict = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        response = await self._client.messages.create(**kwargs)
        return response.content[0].text

    async def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text chunks from Claude. Yields one string per token."""
        kwargs: dict = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
