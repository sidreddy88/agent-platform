# app/agents — Architecture

## BaseAgent and the ReAct Loop

All agents extend `BaseAgent` (`base.py`). The loop runs up to **10 iterations**:

```
1. Build system prompt with tool descriptions
2. Send conversation history to Claude (Sonnet 4.6 default, Haiku 4.5 for TriageAgent)
3. Parse LLM response for one of:
   - Thought / Action / Action Input (JSON) → execute tool → append Observation → loop
   - Answer: <text>                         → return AgentResult to caller
4. Context compression fires automatically when token usage exceeds 70%
```

The parser reads `Thought:`, `Action:`, `Action Input:` (JSON block), and `Answer:` from
raw LLM text. It is whitespace-sensitive. **Do not modify the parser or `_run_loop()`** —
changes affect every agent in the platform.

---

## Tool Registration

Two equivalent patterns:

```python
# Pattern 1 — explicit
agent.register_tool("tool_name", async_fn, "description shown to LLM")

# Pattern 2 — decorator
@agent.tool("tool_name", "description shown to LLM")
async def my_tool(**kwargs) -> str:
    ...
```

**Rules:**
- Tool functions must be `async`
- Must accept `**kwargs` (the ReAct parser passes Action Input as keyword arguments)
- Must return `str` — the string becomes the Observation in the next loop iteration
- Tool errors should return an error string, not raise — the agent can read the error and retry

---

## `incident_id_ctx` — Required for Pipeline Agents

```python
from app.agents.base import incident_id_ctx
incident_id_ctx.set(incident.id)   # call before awaiting any agent
```

This `ContextVar` links agent runs to their incident in `agent_tracker`. It propagates
automatically to child coroutines via Python's asyncio context. Set it once at the top of
each pipeline task (`_process()`, `resume_fix()`). If you add a new pipeline path that
runs agents, set it.

---

## `FixGenerationAgent` — Exception to the ReAct Pattern

`FixGenerationAgent` bypasses the ReAct loop entirely. It uses direct LLM calls + GitHub
API calls sequenced in `fix_with_steps()`.

**Public API:** `fix_with_steps(incident) → tuple[FixResult, list[str]]`

`fix()` is a thin wrapper that discards the steps list. **In tests, always mock
`fix_with_steps`, not `fix`:**

```python
loop._fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, []))
```

---

## Do Not Modify

| Component | Location | Reason |
|---|---|---|
| `_run_loop()` | `base.py` | Affects every agent |
| ReAct parser | `base.py` | Shared format contract with all LLM prompts |
| Context compression | `base.py` | Silently breaks long-running agents if changed |
| `incident_id_ctx` definition | `base.py` | Defined before service imports to avoid circular imports — order is intentional |

---

## Checklist: Adding a New Agent

1. Create `app/agents/my_agent.py` extending `BaseAgent`
2. Register all tools in `__init__`
3. Add a `run(incident)` or equivalent public method
4. In `app/services/incident_loop.py`:
   - Instantiate the agent in `IncidentLoop.__init__`
   - Call it at the right pipeline stage in `_process()`
   - Set `incident.status` before and after the agent runs
5. In tests, instantiate via `IncidentLoop.__new__(IncidentLoop)` (bypasses `__init__`),
   then set `loop._rag = None`, `loop._dedup_stats = {...}`, and mock your agent:
   ```python
   loop._my_agent = AsyncMock()
   loop._my_agent.run.return_value = AgentResult(...)
   ```
6. If the agent run should be tracked, ensure `incident_id_ctx` is set before it runs

---

## Model Selection

| Agent | Model | Reason |
|---|---|---|
| TriageAgent | `claude-haiku-4-5` | High volume, low complexity; speed matters |
| All others | `claude-sonnet-4-6` | Default; balance of quality and speed |

Override in `BaseAgent.__init__` by passing `model=` to `LLMService`.

---

## Fix Generation — Root Cause Rules

**Never generate a symptom fix.** The four patterns to reject:

1. **Exception suppression** — wrapping the crash site in `try/catch` without addressing why the exception occurs.
2. **Input sanitization at the wrong layer** — sanitizing output at the consumer instead of fixing the producer. Example: regex-cleaning JSON inside a parsing function instead of setting `response_format: {type: "json_object"}` at the OpenAI API call site.
3. **Value coercion instead of rejection** — converting bad input to a default value (`int(x) if str(x).isdigit() else 0`) instead of validating at the entry point.
4. **Defensive null checks masking missing initialization** — `if obj && obj.isReady()` instead of ensuring the object is always initialized before use.

**The classifyFields reference case:**
Error was `Unexpected token \ in JSON`. Agent saw the parsing function and added regex sanitization. Correct fix: `response_format: {type: "json_object"}` on the upstream OpenAI call. Lesson: always ask "where does this data come from?" before patching the crash site.

**Stack trace resolution is the only file-finding strategy.**
Code search by error type string produces false positives — the string can appear in comments or logs in unrelated files. If no file resolves from the stack trace, skip the fix entirely. No fallback.
