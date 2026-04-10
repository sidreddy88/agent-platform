# Incident Report: LLM Hallucinations in FixGenerationAgent
**Date:** 2026-04-10  
**Component:** `app/agents/fix_generation.py`  
**Outcome:** Resolved — real PR created at `VoyageGroupMag/AllInterviews`

---

## What Is Hallucination?

An LLM "hallucinates" when it generates confident, plausible-sounding output that is factually wrong. In an agent context this is especially dangerous because the agent is expected to take real actions (create GitHub issues, open PRs) using information it gets back from tools. If the model makes up URLs or numbers instead of reading them from tool responses, real side effects happen on wrong data — or don't happen at all.

---

## Issue 1 — Wrong Repo URL

The model was asked to create a GitHub issue and PR, and to include the URLs in its answer. But the prompt never said where to get those URLs from. So the model did what LLMs do when they lack information — it made something up that sounded plausible.

It knew the service was called `allinterviews`, so it constructed `github.com/allinterviews/allinterviews`. The real repo was `VoyageGroupMag/AllInterviews` — a completely different org and casing. The model had no way to know that without actually calling the tool, and it never did.

**The prompt that caused this** (`app/agents/fix_generation.py`, end of the prompt string):
```
Answer with a concise summary including the issue URL and PR URL.
```
No instruction on where to get those URLs from. The model filled in the blank.

**The fix** — changed the prompt ending to:
```
Answer with a concise summary. In your answer, include the EXACT issue URL and PR URL
returned by the tools — do not construct or guess URLs. Copy them verbatim from the
tool responses (they look like https://github.com/VoyageGroupMag/AllInterviews/issues/N
and https://github.com/VoyageGroupMag/AllInterviews/pull/N).
```

---

## Issue 2 — `pr_number` Always Null

Even when the model wrote a PR number in its answer, the code couldn't extract it. The regex looked for `PR #48` but the model wrote `Pull Request: #48`. Those mean the same thing to a human, but the regex saw no match and returned nothing.

**The code that caused this** (`app/agents/fix_generation.py`):
```python
def _parse_fix_result(answer: str, branch: str) -> FixResult:
    pr_number_match = re.search(r"PR #(\d+)", answer)  # ← too narrow
    return FixResult(
        pr_number=int(pr_number_match.group(1)) if pr_number_match else None,
        ...
    )
```

The model's actual answer text was:
```
**Actions completed:**
- **GitHub Issue:** #47 - https://github.com/.../issues/47
- **Pull Request:** #48 - https://github.com/.../pull/48  ← "Pull Request:" not "PR #"
```

**The fix** — stop parsing prose entirely. Extract `pr_number` from the URL, which always contains it in a stable machine-readable format:
```python
pr_url = pr_url_match.group() if pr_url_match else None
pr_number = None
if pr_url:
    num_match = re.search(r"/pull/(\d+)", pr_url)
    if num_match:
        pr_number = int(num_match.group(1))
```

---

## Issue 3 — `files_changed` Always Empty

The tool that created the PR knew which files were changed — it was right there in the function. But that information never made it out. The tool computed `files_changed` locally and returned it as part of a text observation string. The code that parsed the final answer never looked at tool observations, only the model's concluding text.

**The code that caused this** — inside `_create_pr_with_fix` (the tool closure):
```python
files_changed = [file_path]
# ... optionally append test file ...

# This was returned as part of the tool's observation string:
return (
    f"PR #{pr_number} created: {pr_url}\n"
    f"Files: {', '.join(files_changed)}"
)
# ↑ files_changed only existed as text in the observation.
# _parse_fix_result() only parsed result.answer (the model's final text),
# never the tool observation. And the model never mentioned file names.
```

**The fix** — cache it on `self` at the point of truth, same pattern as `self._pr_url`:
```python
files_changed = [file_path]
self._files_changed = files_changed  # ← added this line
```
Then patch it into the result after `run()`:
```python
if self._files_changed:
    fix_result.files_changed = self._files_changed
```

---

## Issue 4 — JSON Parsing Broken for Nested Braces

This was the deepest bug and caused the most downstream damage. The ReAct loop extracted the model's `Action Input:` using this regex in `app/agents/base.py`:

```python
_INPUT_RE = re.compile(r"Action Input:\s*(\{.*?\})", re.DOTALL)
```

`.*?` is non-greedy — it stops at the **first** `}` it finds. That works fine for flat JSON like `{"file": "foo.js"}`. But when the model included JavaScript function bodies in the JSON (the `old_function` and `new_function` parameters), those bodies contain many `{` and `}`. The regex stopped at the first closing brace inside the JavaScript code.

**What the model actually wrote:**
```
Action: create_pr_with_fix
Action Input: {"file_path": "routes/services/image.js", "old_function": "async function moveAndRemoveFileFromS3(bucket, imageObj) {\n  if (!imageObj.source) return;\n  await s3.copyObject({...}).promise();\n}", "new_function": "..."}
```

**What the regex captured (WRONG):**
```
{"file_path": "routes/services/image.js", "old_function": "async function moveAndRemoveFileFromS3(bucket, imageObj) {
```
It stopped at the `{` inside the function body. `json.loads()` failed silently, the tool received garbage, returned an error string, and the model then wrote a hallucinated final answer.

**The fix** — replaced the regex with a brace-counting parser in `app/agents/base.py`:
```python
def _extract_json_block(text: str) -> str:
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
            continue          # ignore { and } inside quoted strings
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]   # found the real closing brace
    return "{}"
```
And updated `_parse()` to use it:
```python
# Before:
input_match = _INPUT_RE.search(text)
action_input = input_match.group(1).strip() if input_match else "{}"

# After:
action_input = "{}"
ai_pos = text.find("Action Input:")
if ai_pos != -1:
    action_input = _extract_json_block(text[ai_pos + len("Action Input:"):].lstrip())
```

---

## Issue 5 — Model Answering on Iteration 1 Without Calling Tools

Even after fixing the JSON parser, the model still skipped all tools and answered immediately. The reason was the prompt itself. It included a complete `FIX PATTERN` section showing exactly what the corrected function should look like.

**The section of the prompt that caused this** (`app/agents/fix_generation.py`):
```
FIX PATTERN — prefer option B (specific catch, not existence check):
  // After: catch NoSuchKey specifically
  async function moveAndRemoveFileFromS3(bucket, imageObj) {
    try {
      if (!imageObj.source || !imageObj.destination) return;
      if (imageObj.source === imageObj.destination) return;
      await s3.copyObject({ ... }).promise();
      await s3.deleteObject({ Bucket: bucket, Key: imageObj.source }).promise();
    } catch (error) {
      if (error.code === 'NoSuchKey') {
        console.warn('moveAndRemoveFileFromS3: source key not found, skipping', ...);
        return;
      }
      console.log('moveAndRemoveFileFromS3 error', error, bucket, imageObj);
    }
  }
```

The model read this, understood the complete task, and on iteration 1 output:
```
Thought: I have all the information needed to implement the fix.
Answer: Fix implemented successfully. Issue created: https://github.com/allinterviews/allinterviews/issues/42. PR created: https://github.com/allinterviews/allinterviews/pull/123.
```

It had the repo name from the prompt context, the fix pattern from the `FIX PATTERN` section, and invented the issue/PR numbers. No tool was ever called.

**The fix — two changes:**

1. Removed the standalone `FIX PATTERN` block from the prompt so the model no longer had a complete answer available without fetching the real file.

2. Added a validation gate after `run()`:
```python
if not self._pr_url:
    # self._pr_url is only set inside _create_pr_with_fix when it succeeds.
    # If it's still None, the tool was never called.
    logger.error("[FixGenerationAgent] Agent answered without calling tools.")
    return FixResult(
        pr_url=None,
        fix_description="ERROR: agent did not call tools — no PR was created",
    ), result.steps
```

---

## Issue 6 — Abandoned the ReAct Loop Entirely

After all the prompt fixes, the model was still answering on iteration 1. At this point the team recognized that the ReAct loop itself was the wrong tool for this job.

**What the model output every time, regardless of prompt:**
```
[iter 1] Answer: Issue created: https://github.com/VoyageGroupMag/AllInterviews/issues/42.
                 PR created: https://github.com/VoyageGroupMag/AllInterviews/pull/123.
```

The prompt said `⚠️ STRICT RULE: You MUST call all three tools IN ORDER before writing Answer.` — the model ignored it.

**Why this cannot be fixed with prompting:**  
The ReAct loop's system prompt in `app/agents/base.py` reads:
```
When you have enough information to answer the user, output:

Thought: <final reasoning>
Answer: <your final answer to the user>
```
The model's job is to decide when it has "enough information." A powerful model reading a detailed prompt about `moveAndRemoveFileFromS3` with file paths, error types, and fix patterns concludes it has enough information immediately — because it does. Prompting it to call tools first is a soft instruction; the model's assessment of "enough information" takes precedence.

**The fix** — replaced the entire ReAct loop in `FixGenerationAgent` with direct Python:

```python
# Before: one big self.run(prompt) that the model could bypass
result = await self.run(prompt)

# After: four explicit sequential calls, no model decision-making
content, file_sha = await self._github.get_file_contents(owner, repo, file_path)
old_function, new_function = await self._generate_fix(content)   # one LLM call
issue_number, issue_url = await self._github.create_issue(...)
pr_number, pr_url = await self._github.create_pull_request(...)
```
The LLM is now called exactly once, only for what it's actually good at: transforming one function into another. API calls are deterministic Python.

---

## Issue 7 — `old_function` Not Found in File

The new direct approach asked the LLM to return the original function text exactly as it appeared in the file, so the code could do `str.replace(old_function, new_function)`.

**The prompt sent to the LLM:**
```
FILE: routes/services/image.js
```javascript
[full file content]
```

TASK:
1. Find the complete `moveAndRemoveFileFromS3` function in the file above.
2. Generate a fixed version...

Output ONLY the two blocks below:

OLD_FUNCTION:
<copy the function text EXACTLY as it appears in the file above, character for character>
END_OLD

NEW_FUNCTION:
<the fixed version>
END_NEW
```

**What the LLM returned (198 chars):**
```
OLD_FUNCTION:
async function moveAndRemoveFileFromS3(bucket, imageObj) {
  if(!imageObj.source || !imageObj.destination) return;   ← space removed after "if"
  ...
END_OLD
```

**What was actually in the file (not found by str.replace):**
```javascript
async function moveAndRemoveFileFromS3(bucket, imageObj) {
  if (!imageObj.source || !imageObj.destination) return;  ← space after "if"
```

One character difference (`if(!` vs `if (!`). `str.replace()` does exact byte matching — it found nothing.

**The fix** — extract the function using code, never the LLM:

```python
def _extract_js_function(self, content: str, function_name: str) -> str:
    """Find the function declaration, then count braces to find the closing }."""
    patterns = [
        rf'async\s+function\s+{re.escape(function_name)}\s*\(',
        rf'function\s+{re.escape(function_name)}\s*\(',
    ]
    start_pos = -1
    for pattern in patterns:
        m = re.search(pattern, content)
        if m:
            start_pos = m.start()
            break

    if start_pos == -1:
        return ""

    # Count braces from the opening { to find the matching closing }
    brace_start = content.find("{", start_pos)
    depth = 0
    in_string = False
    string_char = ""
    escape_next = False

    for i in range(brace_start, len(content)):
        c = content[i]
        if escape_next:
            escape_next = False; continue
        if c == "\\" and in_string:
            escape_next = True; continue
        if in_string:
            if c == string_char: in_string = False
            continue
        if c in ('"', "'", "`"):
            in_string = True; string_char = c; continue
        if c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return content[start_pos : i + 1]   # exact text from file
    return ""
```
The LLM now only receives the `old_function` text (which came from the file verbatim) and returns only `new_function`. No risk of whitespace mismatch.

---

## Issue 8 — Wrong Default Branch

The code hardcoded `ref="main"` everywhere. The actual repo used `master`.

**The hardcoded value in `app/agents/fix_generation.py`** (original `_create_pr_with_fix` tool):
```python
base_sha = await gh.get_branch_sha(owner, repo, "main")   # ← hardcoded
await gh.create_branch(owner, repo, branch_name, base_sha)
...
pr_number, pr_url = await gh.create_pull_request(
    owner, repo, pr_title, pr_body, branch_name, "main",  # ← hardcoded
    ...
)
```

**Also in `app/services/github.py`:**
```python
async def get_file_contents(
    self, owner: str, repo: str, path: str, ref: str = "main"  # ← hardcoded default
) -> tuple[str, str]:
```

**Error produced:**
```
GitHub API error 404: No commit found for the ref main
```

**The fix** — added `get_default_branch()` to `GitHubService`:
```python
async def get_default_branch(self, owner: str, repo: str) -> str:
    async with self._client() as client:
        response = await client.get(f"/repos/{owner}/{repo}")
        await self._raise_for_status(response)
        return response.json()["default_branch"]   # "master" or "main" or anything
```
Called once at the start of `fix_with_steps()` and used throughout:
```python
default_branch = await self._github.get_default_branch(self._owner, self._repo)
content, file_sha = await self._github.get_file_contents(
    self._owner, self._repo, file_path, ref=default_branch   # ← dynamic
)
...
base_sha = await self._github.get_branch_sha(self._owner, self._repo, default_branch)
...
pr_number, pr_url = await self._github.create_pull_request(
    ..., base=default_branch   # ← dynamic
)
```

---

## Issue 9 — GitHub Token Permissions (Three Separate 403s)

Three separate `403: Resource not accessible by personal access token` errors appeared at different steps.

**What was misleading:** Running `GET /repos/{owner}/{repo}` returned:
```json
{ "permissions": { "admin": true, "push": true, "pull": true } }
```
This looked like full access. But this endpoint only requires `metadata:read` (auto-granted to all fine-grained tokens). The `permissions` field reflects the **user's role in the org**, not what this specific **token is allowed to do via the API**.

**The three failures and which permission fixed each:**

| Step | API call that failed | Missing permission |
|---|---|---|
| Fetch file | `GET /repos/{owner}/{repo}/contents/{path}` | `Contents: Read` |
| Create issue | `POST /repos/{owner}/{repo}/issues` | `Issues: Read and write` |
| Create PR | `POST /repos/{owner}/{repo}/pulls` | `Pull requests: Read and write` |

Note: `Contents: Write` was also needed (for `create_branch` and `update_file`) but `Contents: Read` failing came first.

**How to test permissions before running the pipeline:**
```bash
# Test Contents:Read
curl -sI -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/VoyageGroupMag/AllInterviews/contents/README.md" \
  | head -1
# HTTP/2 200 → OK, HTTP/2 403 → missing Contents:Read

# Test Issues:Write
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -X POST "https://api.github.com/repos/VoyageGroupMag/AllInterviews/issues" \
  -d '{"title":"permission test - delete me"}'
# 201 → OK, 403 → missing Issues:Write
```

---

## Summary Table

| # | Where the problem was | What it was | Fix |
|---|---|---|---|
| 1 | Prompt — final instruction | "include the issue URL and PR URL" — no source specified | Explicitly tell model to copy URLs from tool responses |
| 2 | Code — `_parse_fix_result()` | Regex `PR #(\d+)` didn't match `"Pull Request: #48"` | Extract number from URL with `/pull/(\d+)` instead |
| 3 | Code — tool closure | `files_changed` only existed in tool's local scope | Cache as `self._files_changed`, patch into result |
| 4 | Code — `base.py` `_parse()` | Regex `{.*?}` stopped at first `}` inside JavaScript | Brace-counting parser that respects string quoting |
| 5 | Prompt — FIX PATTERN section | Complete answer shown in prompt — model skipped tools | Remove FIX PATTERN; add `self._pr_url` validation gate |
| 6 | Architecture — ReAct loop | Model can always write `Answer:` — prompting can't stop it | Replace loop with direct sequential Python API calls |
| 7 | Prompt — OLD_FUNCTION request | LLM can't copy code character-for-character | Extract function from file with brace-counting code |
| 8 | Code — hardcoded `"main"` | Target repo uses `master` | `get_default_branch()` API call at start |
| 9 | Environment — token scopes | Fine-grained PAT missing Contents/Issues/PR write | Add three permissions in GitHub Settings |

---

## When to Use a ReAct Agent vs Direct Code

The core architectural lesson from this incident:

**Use a ReAct agent when** the model must decide which tools to call and in what order based on what it observes (open-ended investigation, unknown number of steps, branching based on results). Examples: `TriageAgent`, `DiagnosisAgent`.

**Use direct code when** the steps are always the same fixed sequence with deterministic inputs. Examples: fix generation (always: fetch → generate → issue → PR), any ETL-style pipeline.

The test: *If you could write the tool call sequence as a Python function without any if-branches based on LLM output, it should be direct code.*
