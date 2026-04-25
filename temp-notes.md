# Agent Platform — Engineering Analysis Notes

## Self-Critique Pass

### What it is
A second LLM call (claude-haiku-4-5) that runs after `_generate_fix` produces old/new code, before any branch or PR is created. Asks: does this fix address the root cause, or suppress the symptom?

### Why it was added
The fix generation agent sees one function in one file. The primary LLM is optimized to produce a plausible-looking diff — it reliably produces symptom fixes (wrapping JSON.parse in try/catch, converting invalid values, silencing exceptions). These pass blast radius checks and verbatim-match tests. They get merged and the incident recurs.

Self-critique is a cheap adversarial check ($0.001/call, Haiku) inserted between generation and commit. It only needs to flag uncertainty — a NEEDS REVIEW verdict is surfaced in the PR body.

### What it has access to
- old_function / new_function
- incident.diagnosis and error description
- RAG context: up to 4 chunks from related files (callers, dependencies) excluding the fixed file

RAG is what makes self-critique meaningful. Without it the model only sees the diff. With it, it can reason: "the caller sets up the OpenAI API call without response_format — the fix should be there, not in the JSON parsing function below it."

### Known limitation
Cannot catch what RAG does not surface. If the root cause lives in an unindexed file, critique evaluates the diff in isolation and may verdict LOOKS CORRECT. The verdict is advisory, not blocking.

---

## Root Cause vs. Symptom Fix

### Why this matters more for agents than humans
A human tracing a JSON parse error naturally asks "where does this JSON come from?" An LLM fix agent is handed a file and told to fix it — it produces a diff that looks correct at the crash site. Symptom fixes satisfy all downstream checks (syntax, blast radius, verbatim match) and look fine in isolation.

### Common symptom fix patterns the agent produces

**1. Exception suppression**
```javascript
// symptom fix
try { result = JSON.parse(raw); } catch (e) { result = {}; }
```
Stops propagation. Callers silently get wrong data.

**2. Input sanitization at the wrong layer (the classifyFields case)**
```javascript
// symptom fix
const sanitizedJson = match[0].replace(/\\(?!["\\/bfnrt])/g, '\\\\');
result = JSON.parse(sanitizedJson);
```
Correct fix: `response_format: { type: "json_object" }` at the OpenAI API call site. OpenAI then constrains token generation to valid JSON — classifyFields never receives malformed output.

**3. Value conversion instead of rejection**
```python
value = int(value) if str(value).isdigit() else 0  # symptom
```

**4. Defensive null checks masking missing initialization**
```javascript
if (this.client && this.client.isConnected()) { ... }  // symptom
```

### How the prompt prevents this
Explicit rules in _generate_fix:
- Names the bad patterns by example (try/catch wrapping, sanitization after the fact)
- Contrasts with correct approach (fix upstream source, API call config, schema validation)
- RAG context placed before file content so model reasons about the system first
- System prompt: "always fixes root causes, never symptoms"

---

## The classifyFields Incident

**Error:** Unexpected token \ in JSON at position 9655
**Stack trace:** constants/prankCheckerOpenAI.js → classifyFields
**First agent fix (symptom):** Regex sanitization inside classifyFields before JSON.parse
**Code review:** "Symptom fix — security risk from regex JSON manipulation, root cause unaddressed"
**Correct fix:** response_format: { type: "json_object" } on the OpenAI API call in classifyFields

**Why the agent produced the wrong fix:**
- Saw classifyFields in isolation, no visibility into the OpenAI call at the top of the function
- "Minimal change" framing biased toward patching the crash site
- No RAG context at the time

**What changed after this incident:**
1. RAG context injected into _generate_fix and _critique_fix
2. Explicit anti-symptom rules added to fix prompt
3. Output format changed from JSON to <OLD>/<NEW> delimiters — the json.loads failure in _generate_fix was caused by the same unescaped-backslash bug the agent was trying to fix

---

## Stack Trace File Resolution

Only strategy used. Parses error description/diagnosis for lines matching:
```
at functionName (path/to/file.js:123:45)
File "path/to/file.py", line 123, in function_name
```
Validates path exists at PR_BASE via get_file_contents. If not found, fix generation is skipped entirely — no fallback search.

**Why no fallback:** Code search by error type string produces false matches (ecsHelper.js incident — error type string appeared in a comment, agent generated a fix for the wrong file, passed all downstream checks).

---

## PR Branch Strategy

All fix branches forked from PR_BASE = "staging", PR targets staging. Previously branches were forked from main while PRs targeted staging — caused PRs to show all commits in main not yet in staging alongside the fix commit. Fix: get_branch_sha(PR_BASE) for branch creation, get_file_contents(ref=PR_BASE) for reading the file.
