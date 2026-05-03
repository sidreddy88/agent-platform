"""
ReAct agent loop (Reason + Act).

Each iteration:
  1. Send conversation to Claude with tool descriptions in system prompt
  2. Parse response for Thought / Action / Action Input  →  execute tool
                         OR Answer                       →  return to caller
  3. Append observation and loop (max MAX_ITERATIONS)
"""

import json
import logging
import re
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Set this before awaiting any agent to link its runs to an incident in the tracker.
# incident_loop.py sets it once per _process() task; all awaited agents inherit it.
incident_id_ctx: ContextVar[str | None] = ContextVar("incident_id", default=None)

from app.services.checkpoint import context_checkpointer  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from app.services.preferences import build_preferences_prompt  # noqa: E402
from app.services.tracing import TracingContext, trace_agent, trace_tool_call  # noqa: E402

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 10

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ToolFn = Callable[..., Coroutine[Any, Any, str]]  # async (str | dict) → str


@dataclass
class Step:
    """One iteration of the ReAct loop — kept for debugging / logging."""
    iteration: int
    thought: str = ""
    action: str = ""
    action_input: str = ""
    observation: str = ""
    answer: str = ""        # non-empty only on the final step


@dataclass
class AgentResult:
    answer: str
    steps: list[Step]
    iterations: int


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

REACT_SYSTEM = """\
You are a reasoning agent. To answer the user's question you may use tools.

TOOLS AVAILABLE
{tool_descriptions}

RESPONSE FORMAT — you MUST follow this exactly every turn:

Thought: <your reasoning about what to do next>
Action: <tool name, must be one of [{tool_names}]>
Action Input: <a single JSON object of arguments for the tool>

After you receive an Observation from the tool, continue reasoning.
When you have enough information to answer the user, output:

Thought: <final reasoning>
Answer: <your final answer to the user>

Rules:
- Never call a tool that is not listed above.
- Action Input must be valid JSON.
- Do not output anything after Answer.
"""


def _build_system_prompt(tools: dict[str, tuple[ToolFn, str]]) -> str:
    """Render the system prompt with the registered tools and user preferences."""
    preferences = build_preferences_prompt()

    if not tools:
        return f"{preferences}\n\nYou are a helpful assistant."

    descriptions = "\n".join(
        f"- {name}: {description}" for name, (_, description) in tools.items()
    )
    names = ", ".join(tools.keys())
    react_prompt = REACT_SYSTEM.format(tool_descriptions=descriptions, tool_names=names)
    return f"{preferences}\n\n{react_prompt}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_THOUGHT_RE = re.compile(r"Thought:\s*(.+?)(?=\nAction:|\nAnswer:|$)", re.DOTALL)
_ACTION_RE = re.compile(r"Action:\s*(\w+)")
_ANSWER_RE = re.compile(r"Answer:\s*(.+)", re.DOTALL)


def _extract_json_block(text: str) -> str:
    """
    Extract the first complete JSON object from text using brace-counting.

    The old regex approach (`{.*?}`) fails whenever the JSON value contains
    nested braces — e.g. JavaScript function bodies in old_function/new_function.
    This parser tracks string quoting and escape sequences so that `}` inside a
    quoted string value is never mistaken for the closing brace of the object.
    """
    start = text.find("{")
    if start == -1:
        return "{}"
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return "{}"


def _parse(text: str) -> dict[str, str]:
    thought = (_THOUGHT_RE.search(text) or type("", (), {"group": lambda s, n: ""})()).group(1).strip()
    answer_match = _ANSWER_RE.search(text)
    if answer_match:
        return {"thought": thought, "answer": answer_match.group(1).strip()}

    action_match = _ACTION_RE.search(text)

    # Find Action Input and extract JSON with brace-counting (not regex)
    action_input = "{}"
    ai_marker = "Action Input:"
    ai_pos = text.find(ai_marker)
    if ai_pos != -1:
        action_input = _extract_json_block(text[ai_pos + len(ai_marker):].lstrip())

    return {
        "thought": thought,
        "action": action_match.group(1).strip() if action_match else "",
        "action_input": action_input,
    }


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent:
    """
    Minimal ReAct agent.

    Usage:
        agent = BaseAgent()
        agent.register_tool("search", search_fn, "Search the web for a query")
        result = await agent.run("What is the capital of France?")
        print(result.answer)
        for step in result.steps:
            print(step)
    """

    def __init__(self, llm: LLMService | None = None, gateway: Any = None) -> None:
        self._llm = llm or LLMService()
        self._gateway = gateway
        # name → (async callable, description shown to Claude)
        self._tools: dict[str, tuple[ToolFn, str]] = {}
        # set by @trace_agent at runtime — no-op sentinel until then
        self._tracing_ctx: TracingContext = TracingContext(trace=None, enabled=False)
        # Harness docs (AGENTS.md + CONSTRAINTS.md) injected into every LLM call.
        self._harness_docs: str = self._load_harness_docs()

    @staticmethod
    def _load_harness_docs() -> str:
        """Load AGENTS.md and CONSTRAINTS.md from the configured harness path."""
        try:
            from app.core.config import settings
            raw_path = settings.harness_docs_path
            if not raw_path:
                return ""
            p = Path(raw_path)
            if not p.is_absolute():
                # Resolve relative to project root (this file is app/agents/base.py)
                p = Path(__file__).parent.parent.parent / raw_path
            docs: list[str] = []
            for filename in ("AGENTS.md", "CONSTRAINTS.md"):
                fp = p / filename
                if fp.exists():
                    docs.append(f"=== {filename} ===\n{fp.read_text()}")
            return "\n\n".join(docs)
        except Exception:
            return ""

    def _with_harness(self, system: str) -> str:
        """Prepend harness docs to a system prompt so every LLM call sees them."""
        docs = getattr(self, "_harness_docs", "")
        if not docs:
            return system
        return f"{docs}\n\n---\n\n{system}"

    async def _call_llm(
        self,
        messages: list[dict],
        task_type: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call the LLM via the gateway (if configured) and return LLMResponse.
        Falls back to LLMService when no gateway is injected."""
        if self._gateway is not None:
            return await self._gateway.complete(messages, task_type or "unknown", **kwargs)

        from app.services.llm_gateway import LLMResponse
        text = await self._llm.complete(messages, **kwargs)
        return LLMResponse(
            content=text,
            input_tokens=self._llm.last_input_tokens,
            output_tokens=self._llm.last_output_tokens,
            provider="anthropic",
            model=getattr(self._llm, "_model", "unknown"),
            cost_usd=0.0,
        )

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def register_tool(self, name: str, fn: ToolFn, description: str) -> None:
        """Register a tool the agent can call.

        Args:
            name:        Exact name Claude must use in Action: lines.
            fn:          Async function. Receives one argument: the parsed
                         Action Input dict (or raw string if JSON parse fails).
            description: One-line description shown to Claude in the system prompt.
        """
        self._tools[name] = (fn, description)

    def tool(self, name: str, description: str) -> Callable[[ToolFn], ToolFn]:
        """Decorator shorthand for register_tool."""
        def decorator(fn: ToolFn) -> ToolFn:
            self.register_tool(name, fn, description)
            return fn
        return decorator

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------

    @trace_agent
    async def run(self, user_input: str) -> AgentResult:
        """Run the ReAct loop and return the final answer + all steps."""
        from app.services.agent_tracker import agent_tracker
        _run_id = agent_tracker.start(type(self).__name__, incident_id=incident_id_ctx.get())
        self._current_run_id = _run_id
        _failed = False

        try:
            system = self._with_harness(_build_system_prompt(self._tools))
            messages: list[dict] = [{"role": "user", "content": user_input}]
            steps: list[Step] = []
            _total_input_tokens = 0
            _total_output_tokens = 0

            for i in range(1, MAX_ITERATIONS + 1):
                # Compress conversation history if the previous call's token count
                # reached 70% of the context window (checked before every call
                # except the very first — no usage data available yet on i==1).
                if i > 1 and context_checkpointer.needs_checkpoint(self._llm.last_input_tokens):
                    agent_name = type(self).__name__
                    logger.warning(
                        "[%s] Context checkpoint at iteration %d — %d input tokens (limit %d). Compressing.",
                        agent_name, i, self._llm.last_input_tokens, context_checkpointer._limit,
                    )
                    messages = await context_checkpointer.compress(messages, steps)

                step = Step(iteration=i)
                raw = await self._llm.complete(messages=messages, system=system, tracing_ctx=self._tracing_ctx)
                _total_input_tokens += self._llm.last_input_tokens
                _total_output_tokens += self._llm.last_output_tokens

                # Append assistant turn so Claude sees its own prior reasoning
                messages.append({"role": "assistant", "content": raw})

                parsed = _parse(raw)
                step.thought = parsed.get("thought", "")

                # ── ANSWER → done ──────────────────────────────────────────
                if "answer" in parsed:
                    step.answer = parsed["answer"]
                    steps.append(step)
                    return AgentResult(
                        answer=step.answer,
                        steps=steps,
                        iterations=i,
                    )

                # ── ACTION → execute tool ───────────────────────────────────
                step.action = parsed.get("action", "")
                step.action_input = parsed.get("action_input", "{}")

                observation = await self._execute_tool(step.action, step.action_input)
                step.observation = observation
                steps.append(step)

                # Feed observation back as a user turn
                messages.append({"role": "user", "content": f"Observation: {observation}"})

            # Exceeded max iterations
            return AgentResult(
                answer="I was unable to find an answer within the allowed number of steps.",
                steps=steps,
                iterations=MAX_ITERATIONS,
            )

        except Exception as exc:
            _failed = True
            agent_tracker.fail(_run_id, str(exc))
            raise

        finally:
            if not _failed:
                agent_tracker.complete(_run_id, _total_input_tokens, _total_output_tokens, getattr(self._llm, "_model", "unknown"))

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, raw_input: str) -> str:
        if name not in self._tools:
            return f"Error: unknown tool '{name}'. Available: {list(self._tools)}"

        fn, _ = self._tools[name]

        try:
            parsed_input = json.loads(raw_input)
        except json.JSONDecodeError:
            parsed_input = raw_input  # pass raw string if JSON is malformed

        try:
            if isinstance(parsed_input, dict):
                coro = fn(**parsed_input)
            else:
                coro = fn(parsed_input)
            result = await trace_tool_call(self._tracing_ctx, name, parsed_input, coro)
            from app.services.agent_tracker import agent_tracker
            if hasattr(self, "_current_run_id"):
                agent_tracker.increment_tool_call(self._current_run_id)
            return result
        except Exception as e:
            return f"Error running tool '{name}': {e}"
