# Incident Report: CodeReviewAgent Hallucination and GitHub 422 Error
**Date:** 2026-04-11  
**Component:** `app/agents/code_review.py`, `scripts/triage_replay.py`  
**Related:** [2026-04-10 incident](hallucination_incident_2026-04-10.md) — same root cause (ReAct loop bypass)  
**Outcome:** Resolved — review now posts correctly to GitHub PR

---

## Issue 1 — CodeReviewAgent Fabricating Reviews

After the fix pipeline was confirmed working end-to-end, `CodeReviewAgent` was wired as Step 4. The review that came back looked authoritative and detailed — but referenced **React 18 compatibility**, **XSS vulnerabilities**, and **educational content quality**. The actual PR was a two-line try/catch in a JavaScript S3 helper. None of those topics had any connection to the change. The review also never appeared in the GitHub PR.

**Root cause:** Same as the previous day's `FixGenerationAgent` Issue 6. The `CodeReviewAgent` used `super().run()`, which runs the ReAct loop. On iteration 1, the model wrote:

```
Thought: I have enough information to write the review.
Answer: ## Code Review ...
  Critical security vulnerabilities in user input handling and XSS prevention...
  Technical accuracy issues with React 18 compatibility...
```

It never called `fetch_pr`. It had no idea what was actually in the PR. It generated a plausible-looking review from its training data, not the real diff. The PR number passed to `post_to_github` was correct, but the model's fabricated review was what got submitted — or the post itself failed silently (see Issue 2).

**The fix** — replaced `super().run()` in `CodeReviewAgent.run()` with direct sequential Python calls:

```python
# Before: one super().run() call the model could bypass
return await super().run(prompt)

# After: three explicit sequential calls, no opportunity to skip fetch_pr
pr_summary = await fetch_pr(owner, repo, pr_number, self._github)
for filename in files:
    analysis = await analyze_file(filename, self._github, self._llm)
    analysis_parts.append(f"### {filename}\n{analysis}")
review = await generate_review(owner, repo, pr_number, file_analyses, ...)
```

`fetch_pr` is now a guaranteed Python call. The model only sees real diff content when generating the review.

---

## Issue 2 — 422 Unprocessable Entity When Posting Review

After fixing the ReAct loop, the review was correctly generated from the real PR diff but failed to post to GitHub with `422 Unprocessable Entity`.

**Root cause:** The `generate_review` function detected the word `REQUEST_CHANGES` in the review text and passed `event="REQUEST_CHANGES"` to `POST /repos/{owner}/{repo}/pulls/{pr_number}/reviews`. GitHub rejects `REQUEST_CHANGES` (and `APPROVE`) when the reviewer is the same user who opened the PR. The pipeline uses a single PAT for everything — it created the PR and was now trying to request changes on its own PR.

**The event detection code that caused this** (`app/agents/code_review.py`):
```python
event_map = {
    "APPROVE": "APPROVE",
    "REQUEST_CHANGES": "REQUEST_CHANGES",  # ← rejected for self-review
    "NEEDS_DISCUSSION": "COMMENT",
}
event = "COMMENT"
for key, val in event_map.items():
    if key in review_text:
        event = val
        break
```

The review said `## Recommendation: REQUEST_CHANGES` (it found real issues). That matched the map, set `event="REQUEST_CHANGES"`, and GitHub returned 422.

**What made it hard to diagnose:** The error was silently appended to the review text as `"Warning: failed to post to GitHub: ..."`, but `triage_replay.py` truncated the output to 2000 characters. The warning appeared after character 2000 and was never printed. The terminal looked completely clean.

**The fix — two changes:**

1. Always use `"COMMENT"` as the event. `COMMENT` is accepted regardless of PR authorship. The recommendation (`APPROVE` / `REQUEST_CHANGES`) is communicated in the review body text anyway:
```python
# Before: detect event from text, risk 422
event = "COMMENT"
for key, val in event_map.items():
    if key in review_text:
        event = val
        break
await github.post_pr_review(..., event=event)

# After: always COMMENT — recommendation is in the body
await github.post_pr_review(..., event="COMMENT")
```

2. Removed the 2000-char output truncation in `triage_replay.py` and added an explicit posted/not-posted status line:
```python
# Before:
print(review_result.answer[:2000])   # ← errors after char 2000 invisible

# After:
print(review_result.answer)
posted = "✓ Review posted to GitHub." in review_result.answer
print(f"  Review : {'posted to GitHub' if posted else 'NOT posted — see warning above'}")
```

---

## Summary Table

| # | Where the problem was | What it was | Fix |
|---|---|---|---|
| 1 | Architecture — CodeReviewAgent ReAct loop | Model fabricated review without calling `fetch_pr` | Replace `super().run()` with direct sequential calls |
| 2 | Code — review event detection + output truncation | `REQUEST_CHANGES` rejected by GitHub for self-review; 422 error hidden after 2000-char truncation | Always use `COMMENT` event; remove output truncation |

---

## When to Use a ReAct Agent vs Direct Code

Both incidents (2026-04-10 and 2026-04-11) share the same root cause. The pattern now applies to any agent in this codebase:

**Use a ReAct agent when** the model must decide which tools to call and in what order based on intermediate observations — open-ended investigation, unknown number of steps, branching based on results. Examples: `TriageAgent`, `DiagnosisAgent`.

**Use direct code when** the steps are a fixed sequence with deterministic inputs. Examples: fix generation (fetch → generate → issue → PR), code review (fetch PR → analyze each file → generate review).

The test: *If you could write the complete tool call sequence as a Python function without any if-branches based on LLM output, it should be direct code, not a ReAct loop.*

---

## Plain English Summary

**Issue 1 — CodeReviewAgent Fabricating Reviews**

The code review step was given a real PR number and asked to review it. The review that came back was detailed and confident — but it talked about React 18, XSS vulnerabilities, and educational content. The actual PR was a two-line error handling fix in a Node.js file. None of that was real.

The agent was using the ReAct loop, which works by suggesting tools for the model to call. The model read the prompt, decided it understood the situation well enough, and wrote a final answer on the first iteration without calling any tools — including the `fetch_pr` tool that would have fetched the real code changes. It generated a review from its training data, not from the actual PR.

This is the exact same failure mode that happened with `FixGenerationAgent` the day before. ReAct loops give the model an exit: it can always write a final answer and skip the tools entirely. For a fixed sequence of steps, that's a fatal flaw.

The fix was the same: replace the loop with direct Python calls. `fetch_pr` is called first in Python code, unconditionally, before the model ever sees any content. The model can't skip it.

---

**Issue 2 — 422 Error When Posting Review**

Once the review was being generated from the real diff, it failed to post to GitHub. The error code was 422 — "Unprocessable Entity."

The review found real issues and recommended `REQUEST_CHANGES`. The code read that phrase from the review text and used it as the GitHub API event type. GitHub's rules say you cannot request changes on a PR you opened yourself — and this pipeline uses a single token for everything, so the same account that created the PR was trying to block it. GitHub rejected the request.

The tricky part was that this error was completely invisible. The error message was appended to the end of the review text, but the terminal was only printing the first 2000 characters of output. The error appeared at character 2001. The terminal looked clean.

The fix was to always post reviews as a `COMMENT` instead of `REQUEST_CHANGES` or `APPROVE`. Comments are accepted regardless of who opened the PR, and the recommendation is still visible in the review body. The output truncation was also removed so any future errors will actually be visible.
