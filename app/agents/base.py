"""
ReAct agent loop (Reason + Act).

Each iteration:
  1. Send conversation to Claude with tool descriptions in system prompt
  2. Parse response for Thought / Action / Action Input  →  execute tool
                         OR Answer                       →  return to caller
  3. Append observation and loop (max MAX_ITERATIONS)
"""

import json
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from app.services.llm import LLMService
from app.services.preferences import build_preferences_prompt
from app.services.tracing import TracingContext, trace_agent, trace_tool_call

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
_INPUT_RE = re.compile(r"Action Input:\s*(\{.*?\})", re.DOTALL)
_ANSWER_RE = re.compile(r"Answer:\s*(.+)", re.DOTALL)


def _parse(text: str) -> dict[str, str]:
    thought = (_THOUGHT_RE.search(text) or type("", (), {"group": lambda s, n: ""})()).group(1).strip()
    answer_match = _ANSWER_RE.search(text)
    if answer_match:
        return {"thought": thought, "answer": answer_match.group(1).strip()}

    action_match = _ACTION_RE.search(text)
    input_match = _INPUT_RE.search(text)
    return {
        "thought": thought,
        "action": action_match.group(1).strip() if action_match else "",
        "action_input": input_match.group(1).strip() if input_match else "{}",
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

    def __init__(self, llm: LLMService | None = None) -> None:
        self._llm = llm or LLMService()
        # name → (async callable, description shown to Claude)
        self._tools: dict[str, tuple[ToolFn, str]] = {}
        # set by @trace_agent at runtime — no-op sentinel until then
        self._tracing_ctx: TracingContext = TracingContext(trace=None, enabled=False)

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
        system = _build_system_prompt(self._tools)
        messages: list[dict] = [{"role": "user", "content": user_input}]
        steps: list[Step] = []

        for i in range(1, MAX_ITERATIONS + 1):
            step = Step(iteration=i)
            raw = await self._llm.complete(messages=messages, system=system, tracing_ctx=self._tracing_ctx)

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
            return await trace_tool_call(self._tracing_ctx, name, parsed_input, coro)
        except Exception as e:
            return f"Error running tool '{name}': {e}"
