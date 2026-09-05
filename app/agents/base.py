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
    action_match = _ACTION_RE.search(text)

    # A single response can contain both markers -- either the model "ran
    # ahead" and pre-wrote an answer in the same turn as a real action, or
    # (confirmed in production) fabricated a fake Action/Observation pair
    # ahead of its answer to look like it had done verification it never
    # actually did. Treating "Answer:" as authoritative whenever it appears
    # ANYWHERE, unconditionally, let that fabrication bypass tool-call
    # enforcement entirely for any agent that hasn't opted into
    # _min_tool_calls_before_answer (BaseAgent.run() has its own defense
    # for agents that DO opt in, but every other agent had no protection at
    # all against this exact pattern). Whichever marker appears FIRST is
    # what the model actually intended this turn: an Action appearing
    # before Answer means the trailing answer text is premature and gets
    # discarded in favor of actually running the tool and letting the model
    # answer for real on a later turn, once it has a genuine observation.
    if answer_match and (action_match is None or answer_match.start() < action_match.start()):
        return {"thought": thought, "answer": answer_match.group(1).strip()}

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
        # Opt-in floor: subclasses whose answers get acted on (e.g. fed to
        # FixGenerationAgent) can require at least N tool calls before a
        # final answer is accepted, rather than trusting whatever the LLM
        # produces on its very first response. 0 (default) preserves the
        # original behavior for every existing agent. See DiagnosisAgent
        # for why this exists — a real production diagnosis answered in a
        # single LLM call with zero tool calls and fabricated its evidence.
        self._min_tool_calls_before_answer: int = 0
        # Opt-in: require at least one call to a tool NAMED in this set before
        # an answer is accepted, on top of (not instead of) the count-based
        # floor above. Real production bug the count-based floor alone can't
        # catch: a diagnosis called two log-checking tools (both returned no
        # data), satisfying "at least 1 real tool call," then answered with a
        # fabricated affected_file/root_cause_snippet -- having never called
        # get_file_contents/search_codebase/grep_codebase, i.e. never actually
        # read any real code at all. A count floor can't distinguish "verified
        # something real" from "called any tool, even one that found nothing
        # relevant" -- this can. Empty set (default) preserves existing
        # behavior for every agent that doesn't opt in.
        self._required_tool_names_before_answer: set[str] = set()
        # Opt-in: a single tool that MUST have been called before an answer
        # is accepted -- distinct from the "any one of these" semantics above.
        # Real production bug found via a SWE-bench eval: a diagnosis called
        # get_file_contents/search_codebase/grep_codebase (satisfying the set
        # above), reasoned correctly, and named the actual right file -- then
        # wrote it all as free-text prose in its Answer instead of calling
        # submit_diagnosis, mimicking an "accepted" response ("Status:
        # Diagnosis accepted...") without ever invoking the tool that finalizes
        # one. The set-based check above can't catch this: "called some
        # code-reading tool" and "called the one finalizing tool" are
        # independent facts, not alternatives to each other. None (default)
        # preserves existing behavior for every agent that doesn't opt in.
        # Used for the rejection message text -- see _must_call_check below
        # for what actually gates.
        self._must_call_before_answer: str | None = None
        # Second real bug found one layer deeper, same session: gating on
        # "was this tool NAME ever in _tools_called" is satisfied by a
        # REJECTED call too -- a diagnosis called submit_diagnosis once, got
        # rejected, made two more grep_codebase calls, then wrote another
        # free-text "the diagnosis has been accepted" answer, which sailed
        # through because "submit_diagnosis" was already in _tools_called
        # from the earlier rejected attempt. "Called" and "succeeded" are
        # different facts; the tool-name check can only ever observe the
        # former. When set, this predicate is checked INSTEAD of tool-name
        # membership -- DiagnosisAgent sets it to check
        # self._diagnosis_submitted is not None, which is only ever set by a
        # submission that actually passed grounding. None (default) falls
        # back to the tool-name check above, preserving existing behavior.
        self._must_call_check: Callable[[], bool] | None = None

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

    def _with_harness(self, system: str) -> str | list:
        """Prepend harness docs to a system prompt so every LLM call sees them.

        When harness docs are present, returns a content-block list with
        cache_control on the stable prefix so Anthropic caches it across
        the ReAct loop iterations.  Both the Anthropic SDK and LiteLLM
        accept list[dict] for the system parameter.
        """
        docs = getattr(self, "_harness_docs", "")
        if not docs:
            return system
        return [
            {"type": "text", "text": docs, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": f"\n\n---\n\n{system}"},
        ]

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
            _tool_calls_made = 0
            _tools_called: set[str] = set()

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

                # ── ANSWER → done (unless this subclass requires evidence first) ──
                if "answer" in parsed:
                    count_unmet = _tool_calls_made < self._min_tool_calls_before_answer
                    # Real production bug the count check alone can't catch: a
                    # diagnosis called two log-checking tools (both returned no
                    # data), satisfying "at least 1 real tool call," then
                    # answered with a fabricated affected_file/root_cause_snippet
                    # -- having never called a code-reading tool, i.e. never
                    # actually read any real code. A count floor can't tell
                    # "verified something real" apart from "called any tool,
                    # even an irrelevant one that found nothing."
                    required_unmet = bool(
                        self._required_tool_names_before_answer
                    ) and not (_tools_called & self._required_tool_names_before_answer)
                    # Independent of required_unmet above: "called some tool from
                    # this set" and "called this ONE specific tool" are separate
                    # facts. A model can satisfy required_unmet (real code-reading
                    # happened) while still never calling the one tool that
                    # actually finalizes an answer — see _must_call_before_answer's
                    # docstring in __init__ for the real case this was found from.
                    # Predicate check (when set) supersedes tool-name membership --
                    # a REJECTED call still adds the name to _tools_called, so
                    # name-membership alone can't distinguish "called" from
                    # "succeeded." See _must_call_check's docstring in __init__.
                    if self._must_call_check is not None:
                        must_call_unmet = not self._must_call_check()
                    else:
                        must_call_unmet = bool(
                            self._must_call_before_answer
                        ) and self._must_call_before_answer not in _tools_called
                    if count_unmet or required_unmet or must_call_unmet:
                        if i < MAX_ITERATIONS:
                            if must_call_unmet:
                                step.observation = (
                                    f"REJECTED: you must successfully call "
                                    f"{self._must_call_before_answer or 'the required tool'} "
                                    f"to finalize — writing your conclusion as answer text is not "
                                    f"enough, even if it looks complete, and a call that was itself "
                                    f"rejected doesn't count as success. Call "
                                    f"{self._must_call_before_answer} now with your findings."
                                )
                            elif required_unmet:
                                step.observation = (
                                    f"REJECTED: you must call at least one of "
                                    f"{sorted(self._required_tool_names_before_answer)} before "
                                    f"answering — you have called {sorted(_tools_called) or 'nothing'} "
                                    f"so far, which doesn't include any of them. Use one of those "
                                    f"tools now, then answer."
                                )
                            else:
                                step.observation = (
                                    f"REJECTED: you must call at least "
                                    f"{self._min_tool_calls_before_answer} tool(s) to verify your "
                                    f"claims before answering — you have called {_tool_calls_made} "
                                    f"so far. Use one of your verification tools now, then answer."
                                )
                            steps.append(step)
                            messages.append({"role": "user", "content": f"Observation: {step.observation}"})
                            continue
                        # Last iteration and still never verified anything for
                        # real — do NOT accept this answer at face value.
                        # Confirmed in production: a model that couldn't (or
                        # wouldn't) comply fabricated a fake Action/Observation
                        # pair *inside its own answer* to look compliant,
                        # rather than ever calling a real tool — the previous
                        # "always accept on the last iteration" escape hatch
                        # let that fabrication straight through to a real
                        # GitHub PR. Fall through to the same "exceeded max
                        # iterations" result every caller already knows how to
                        # handle (DiagnosisAgent, for one, degrades this to a
                        # confidence=0.0/escalate=True result) instead of
                        # trusting a claim with zero real evidence behind it.
                        step.observation = (
                            f"Never verified any claim via a real tool call after "
                            f"{MAX_ITERATIONS} attempts."
                        )
                        steps.append(step)
                        break

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
                _tool_calls_made += 1
                _tools_called.add(step.action)
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
            logger.error("[%s] Tool '%s' failed: %s", self.__class__.__name__, name, e)
            return f"Error running tool '{name}': {e}"
