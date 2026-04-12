"""
Context-window checkpointing for the BaseAgent ReAct loop.

When the cumulative input-token count for a running agent loop crosses
CHECKPOINT_AT (70% of the model context window), the conversation history
is compressed: a cheap Haiku call summarises every completed step into a
single paragraph, and the messages list is replaced with:

  [original user message]
  [assistant checkpoint summary]
  [most recent observation]

This keeps the next LLM call well under the context limit while preserving
all key findings discovered so far.

Usage (wired into BaseAgent automatically):
    from app.services.checkpoint import context_checkpointer

    if context_checkpointer.needs_checkpoint(llm.last_input_tokens):
        messages = await context_checkpointer.compress(messages, steps)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.base import Step

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT_WINDOW: int = 200_000   # tokens — Claude Sonnet 4 / Haiku 4 shared limit
CHECKPOINT_AT: float = 0.70     # trigger at 70% → 140 000 input tokens


# ---------------------------------------------------------------------------
# ContextCheckpointer
# ---------------------------------------------------------------------------

class ContextCheckpointer:
    """
    Monitors per-iteration input-token counts and compresses the messages
    list when the running context approaches the model's limit.
    """

    def __init__(
        self,
        context_window: int = CONTEXT_WINDOW,
        threshold: float = CHECKPOINT_AT,
    ) -> None:
        self._limit = int(context_window * threshold)
        self.checkpoints_taken: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def needs_checkpoint(self, input_tokens: int) -> bool:
        """Return True when the last prompt consumed >= threshold tokens."""
        if not isinstance(input_tokens, (int, float)):
            return False
        return input_tokens >= self._limit

    async def compress(
        self,
        messages: list[dict],
        steps: list[Step],
    ) -> list[dict]:
        """
        Compress *messages* by summarising completed *steps* via a Haiku call.

        Always returns a 3-message alternating list:
          user  → original user request
          asst  → [CHECKPOINT] summary paragraph
          user  → most recent observation  (same as messages[-1])

        If summarisation fails, falls back to concatenating raw observations.
        """
        if len(messages) < 2:
            return messages  # nothing to compress

        try:
            summary = await self._summarise(steps)
        except Exception as exc:
            logger.warning("[Checkpoint] _summarise raised unexpectedly (%s) — using fallback", exc)
            summary = self._fallback_summary(steps)
        self.checkpoints_taken += 1

        original_user = messages[0]        # {"role": "user", "content": original_input}
        last_message   = messages[-1]      # most recent observation (always a user turn)

        compressed: list[dict] = [
            original_user,
            {
                "role": "assistant",
                "content": (
                    f"[CONTEXT CHECKPOINT — {len(steps)} prior step(s) compressed]\n"
                    f"{summary}"
                ),
            },
        ]
        # Append the latest observation only when it differs from the original request
        if last_message is not original_user:
            compressed.append(last_message)

        logger.info(
            "[Checkpoint] Compressed %d messages → %d  (steps=%d, checkpoint #%d)",
            len(messages), len(compressed), len(steps), self.checkpoints_taken,
        )
        return compressed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _summarise(self, steps: list[Step]) -> str:
        """Call Haiku to produce a concise summary of completed steps."""
        from app.services.llm import HAIKU_MODEL, LLMService

        steps_text = self._format_steps(steps)
        if not steps_text:
            return "(no steps to summarise)"

        prompt = (
            "You are a context compressor for an AI agent loop. "
            "Summarise the following completed reasoning steps into one concise paragraph. "
            "Preserve every key fact, tool result, or finding discovered. "
            "Do NOT include meta-commentary — only the essential information.\n\n"
            f"{steps_text}"
        )
        haiku = LLMService(model=HAIKU_MODEL)
        try:
            return await haiku.complete(messages=[{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.warning("[Checkpoint] Haiku summarisation failed (%s) — using fallback", exc)
            return self._fallback_summary(steps)

    @staticmethod
    def _format_steps(steps: list[Step]) -> str:
        lines: list[str] = []
        for s in steps:
            lines.append(f"Iteration {s.iteration}:")
            if s.thought:
                lines.append(f"  Thought: {s.thought[:300]}")
            if s.action:
                lines.append(f"  Action:  {s.action}({s.action_input[:200]})")
            if s.observation:
                lines.append(f"  Result:  {s.observation[:400]}")
        return "\n".join(lines)

    @staticmethod
    def _fallback_summary(steps: list[Step]) -> str:
        observations = [
            f"Step {s.iteration} ({s.action}): {s.observation[:300]}"
            for s in steps
            if s.observation
        ]
        return "Key findings: " + " | ".join(observations) if observations else "(no observations)"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

context_checkpointer = ContextCheckpointer()
