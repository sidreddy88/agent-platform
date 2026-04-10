# Incident Report: LLM Hallucinations in FixGenerationAgent
**Date:** 2026-04-10  
**Component:** `app/agents/fix_generation.py`  
**Outcome:** Resolved — real PR created at `VoyageGroupMag/AllInterviews`

---

## What Is Hallucination?

An LLM "hallucinates" when it generates confident, plausible-sounding output that is factually wrong. In an agent context this is especially dangerous because the agent is expected to take real actions (create GitHub issues, open PRs) using information it gets back from tools. If the model makes up URLs or numbers instead of reading them from tool responses, real side effects happen on wrong data — or don't happen at all.

---

## Background

The `FixGenerationAgent` was designed to:
1. Fetch `routes/services/image.js` from GitHub
2. Create a GitHub Issue documenting the incident
3. Generate a fix, commit it on a branch, and open a PR

It used a **text-based ReAct loop** — the model reasons in plain text (`Thought: / Action: / Action Input: / Observation:`) and the framework parses that text to call real tools. This design caused every hallucination in this incident.

---

## Step-by-Step: What Went Wrong and Why

### Step 1 — Wrong repo URL in answer text

**Error produced:**
```
"issue_url": "https://github.com/allinterviews/allinterviews/issues/47"
"pr_url":    "https://github.com/allinterviews/allinterviews/pull/48"
```

**What should have happened:**  
URLs should point to `VoyageGroupMag/AllInterviews`.

**Root cause:**  
The model never called the tools. It wrote a final `Answer:` directly, inventing both URLs. The prompt said "include the issue URL and PR URL" but didn't say where to get them from, so the model guessed. It used `allinterviews/allinterviews` — a plausible-sounding but completely wrong repo name it constructed from the service name `allinterviews`.

**How we diagnosed it:**  
The `pr_number` field was `null` even though `pr_url` was set. This exposed the inconsistency — if the tool had actually been called, `self._pr_number` would have been cached and patched in. The fact it wasn't meant the tool was never called.

**Fix applied:**  
Added instruction to the prompt: "Copy URLs verbatim from tool responses — do not construct or guess them."

---

### Step 2 — `pr_number` always null

**Error produced:**
```json
{ "pr_number": null, "pr_url": "https://github.com/.../pull/48" }
```

**Root cause:**  
The regex used to extract the PR number from the model's answer was:
```python
re.search(r"PR #(\d+)", answer)
```
But the model wrote `"Pull Request: #48"` (not `"PR #48"`), so the regex never matched. Since `pr_number` was null, the downstream code that called the code reviewer with a specific PR number had nothing to work with.

**How we diagnosed it:**  
Inspected the `fix_description` field (first 500 chars of the answer text) and found `"Pull Request: #48"` — the number was there but in a different format than the regex expected.

**Fix applied:**  
Stopped relying on prose parsing entirely. Instead, extracted `pr_number` directly from `pr_url` using `/pull/(\d+)` — the URL always contains the number regardless of how the model phrases its answer. Also changed the code to always prefer `self._pr_number` (set by the actual tool call) over anything parsed from text.

---

### Step 3 — `files_changed` always empty

**Error produced:**
```json
{ "files_changed": [] }
```
Even when a fix was committed, the list was empty.

**Root cause:**  
`files_changed` was only populated inside the `_create_pr_with_fix` tool function and returned as part of the tool's observation string. But `_parse_fix_result()` only parsed the model's final *answer* text — not the tool observation. The answer text never mentioned which files changed.

**How we diagnosed it:**  
Noticed `files_changed` was always `[]` regardless of what actually happened. Traced the data flow: `files_changed` was set inside the tool closure but never cached on `self`.

**Fix applied:**  
Added `self._files_changed = files_changed` inside `_create_pr_with_fix` (same pattern as `self._pr_url`). After `run()`, patched it into the result just like the other cached values.

---

### Step 4 — JSON parsing broken for nested braces (the core bug)

**Error produced:**  
Tool calls silently failed. `self._pr_url` was never set. Model hallucinated round numbers like `#1234` and `#5678`.

**Root cause:**  
The ReAct loop used this regex to extract `Action Input`:
```python
_INPUT_RE = re.compile(r"Action Input:\s*(\{.*?\})", re.DOTALL)
```
`.*?` is non-greedy — it stops at the **first** `}` found. When the model included JavaScript function bodies in the JSON (`old_function`, `new_function`), those bodies contain many `{` and `}`. The regex stopped at the first `}` inside the JavaScript code, producing truncated invalid JSON:

```
# What the model wrote:
Action Input: {"file_path": "foo.js", "old_function": "async function f() { if (x) { return; } }"}

# What the regex captured (WRONG — stopped at first }):
{"file_path": "foo.js", "old_function": "async function f() {
```

`json.loads()` failed on this. The agent framework caught the exception silently and called the tool with the raw malformed string instead of a dict. The tool received wrong arguments, returned an error string, and the model — seeing a failed observation — just wrote a final answer with made-up values.

**How we diagnosed it:**  
Added `fix_with_steps()` to expose all ReAct iterations. Saw `[iter 1] Answer:` — the agent was answering on the very first iteration without calling any tools. That was the smoking gun: tools weren't being called, so the model must be reasoning past them somehow.

**Fix applied:**  
Replaced the regex with a proper brace-counting parser that understands string quoting:
```python
def _extract_json_block(text: str) -> str:
    # Count { and } depth, ignoring characters inside "..." strings
    # Only a } at depth=0 ends the JSON object
```
This correctly handles any amount of nesting and quoted content.

---

### Step 5 — Model answering on iteration 1 without calling any tools

**Error produced:**
```
[iter 1] Answer: Issue created: https://github.com/.../issues/42.
                 PR created: https://github.com/.../pull/123.
```
No tool calls at all. Numbers `#42` and `#123` were invented.

**Root cause:**  
Even after fixing the JSON parser, the model skipped all tool calls and answered immediately. Why? The prompt included a complete `FIX PATTERN` section showing exactly what the fixed function should look like:
```javascript
async function moveAndRemoveFileFromS3(bucket, imageObj) {
  try { ... } catch (error) { if (error.code === 'NoSuchKey') { ... } }
}
```
The model saw this, understood the full task, and decided it had enough information to write the final answer without fetching the real file or calling any APIs. The instruction "call these tools in order" is advisory to the model — it can ignore it if it thinks it already knows the answer.

**How we diagnosed it:**  
`fix_with_steps()` showed `[iter 1] Answer:` again — same symptom. The validation gate (`if not self._pr_url: return FixResult with error`) now caught it and prevented a hallucinated PR number from flowing downstream.

**Fix applied:**  
Two changes:
1. **Prompt redesign** — removed the standalone FIX PATTERN block (it was giving the model a complete answer without needing tools). Restructured prompt to use the exact `Action:` / `Action Input:` format so the model starts generating a tool call naturally.
2. **Validation gate** — after `run()`, check `if not self._pr_url`. If it's None, the tool was never called. Return a hard failure instead of using whatever the model hallucinated.

---

### Step 6 — Abandoned ReAct loop entirely

**Error produced:**
```
[iter 1] Answer: Issue created: https://github.com/allinterviews/allinterviews/issues/42.
```
Model still answered on iteration 1 despite the prompt redesign.

**Root cause:**  
The text-based ReAct loop is fundamentally unsuited for deterministic sequential tasks. The model is *always* capable of writing `Answer:` on the first iteration — no prompt constraint can guarantee it won't. The loop was designed for open-ended reasoning where the model needs to decide *which* tools to call and in what order. For fix generation, the sequence is fixed: fetch → generate → issue → PR.

**Fix applied:**  
Replaced the ReAct loop entirely with direct Python API calls in `fix_with_steps()`:
```python
# Step 1: GitHub API call (no model involved)
content, sha = await self._github.get_file_contents(...)

# Step 2: Single focused LLM call for function generation only
old_fn, new_fn = await self._generate_fix(content)

# Step 3: GitHub API call
issue_number, issue_url = await self._github.create_issue(...)

# Step 4: GitHub API calls
await self._github.create_branch(...)
await self._github.update_file(...)
pr_number, pr_url = await self._github.create_pull_request(...)
```
The LLM is now only used for what it's actually good at: transforming one function into another. All API calls are direct Python code with proper error handling.

---

### Step 7 — `old_function` not found in file

**Error produced:**
```
✗ old_function not found verbatim in file — cannot apply patch
```

**Root cause:**  
The LLM was asked to copy the function text "exactly as it appears in the file" so we could do a string replacement. But the model returned slightly modified whitespace, indentation, or minor characters — not character-for-character identical. Since the replacement uses `str.replace()`, even a single space difference causes it to fail.

**How we diagnosed it:**  
The step log showed `old=198 chars` and `new=168 chars`. The old function was found by the LLM (198 chars is plausible), but `str.replace()` couldn't find it in the 32,988-char file.

**Fix applied:**  
Stopped asking the LLM to copy the function. Instead:
1. `_extract_js_function()` — a brace-counting Python function that finds and extracts the exact function text directly from the file content (same brace-counting technique as the JSON parser fix)
2. Pass only that extracted text to the LLM
3. LLM returns only the new fixed version

Now the `old_function` is extracted by code (guaranteed to match), and the LLM only generates the replacement.

---

### Step 8 — Wrong default branch (`main` vs `master`)

**Error produced:**
```
✗ get_file_contents failed: GitHub API error 404: No commit found for the ref main
```

**Root cause:**  
The code hardcoded `ref="main"` everywhere. The `VoyageGroupMag/AllInterviews` repo uses `master` as its default branch.

**Fix applied:**  
Added `GitHubService.get_default_branch()` which calls `GET /repos/{owner}/{repo}` and reads the `default_branch` field. `FixGenerationAgent` now calls this at the start and uses the result for all branch operations.

---

### Step 9 — GitHub token permissions

**Errors produced (three separate 403s at different steps):**
```
403: Resource not accessible by personal access token  ← get_file_contents
403: Resource not accessible by personal access token  ← create_issue
403: Resource not accessible by personal access token  ← create_pull_request
```

**Root cause:**  
A fine-grained PAT was used. Fine-grained tokens require explicit per-resource permissions. The token had `metadata:read` (auto-granted, allows `GET /repos/{owner}/{repo}`) but was missing:
- `contents:read+write` — needed to read files and create branches/commits
- `issues:read+write` — needed to create issues
- `pull_requests:read+write` — needed to open PRs

The `GET /repos/{owner}/{repo}` returning `permissions: {admin: true}` was misleading — that endpoint works with just `metadata:read` and reflects the user's org role, not the token's API scope.

**Fix applied:**  
Added the three missing permissions to the fine-grained PAT in GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens.

---

## Summary of Root Causes

| # | Root Cause | Category |
|---|---|---|
| 1 | Model invented repo name from service name | Prompt design — no source constraint |
| 2 | Regex didn't match model's prose style | Fragile output parsing |
| 3 | Tool response data not cached | Missing data pipeline |
| 4 | `{.*?}` regex breaks on nested braces in JSON | Parser bug |
| 5 | FIX PATTERN in prompt gave model a complete answer | Prompt design — too much context |
| 6 | ReAct loop can't force tool calls | Wrong architecture for deterministic tasks |
| 7 | LLM can't copy code character-for-character reliably | Wrong task for LLM |
| 8 | Default branch hardcoded as `main` | Configuration assumption |
| 9 | Fine-grained PAT missing three permissions | Environment setup |

---

## How to Detect Hallucinations Faster

### 1. Check if tools were actually called
Always cache values set by tool calls (`self._pr_url`, `self._pr_number`) and treat them as the authoritative source. If they're `None` after the agent finishes, the tool was never called — reject the result immediately.

```python
if not self._pr_url:
    # Model hallucinated — return failure, not the made-up URL
    return FixResult(fix_description="ERROR: tools were not called")
```

### 2. Add step-by-step visibility early
`fix_with_steps()` returning the list of steps was the key diagnostic tool. Add this from day one — not after chasing bugs.

### 3. Watch for suspiciously round numbers
PR #42, #123, #1234, #5678 are all classic model placeholders. Real GitHub numbers in an active repo are in the thousands and sequential. If you see a low round number in a repo with 1978+ PRs, it's hallucinated.

### 4. Never use a text-based ReAct loop for deterministic pipelines
If the steps are fixed (always: fetch → process → write), use direct code. ReAct loops are for open-ended reasoning where the model must decide which tools to call. For scripted sequences, they add fragility with no benefit.

### 5. Never ask the LLM to copy text verbatim for structural use
If you need exact text from a file for a string replacement, extract it with code. The LLM is not a copy machine — it will paraphrase, change whitespace, or modify formatting even when asked not to.

### 6. Parse structured data from the right source
- **Use `self._pr_url`** (set by the actual API call) — not a regex on the model's prose
- **Use `/pull/(\d+)` on the URL** — not a regex on "PR #N" in a sentence
- **Never construct URLs** from known parts — always copy from the API response

### 7. Test permissions before running the full pipeline
```bash
curl -sI -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/{owner}/{repo}/contents/README.md
# 200 = Contents:Read OK
# 403 = missing permission
```

---

## Architecture Decision: When to Use an Agent vs Direct Code

| Use an agent (ReAct loop) when... | Use direct code when... |
|---|---|
| Steps are unknown upfront | Steps are always the same sequence |
| Model must decide which tools to call | Tool call order is fixed |
| Information gathering is open-ended | Each step has deterministic inputs |
| Multiple valid paths to the answer | Errors at each step are specific and handleable |

Fix generation is a **direct code** task. The agents that benefit from a ReAct loop are `TriageAgent` and `DiagnosisAgent` — they do open-ended reasoning where the model decides what to investigate next.
