# Don't Trust Your Eval: How Testing a Diagnosis Agent Against Unfamiliar Code Found a Real Bug

Every eval story I'd told myself about this agent platform up to this point was self-sourced: real production incidents, replayed against the agent that originally handled them, scored against what actually happened. That's a good eval — until you ask the obvious follow-up question: *how do you know the eval itself is measuring what you think it's measuring?*

This is the story of chasing that question through three layers, on two different codebases, and ending up with a real production bug fixed — not the bug I went looking for, but the one the process was designed to surface.

## Part 1: The regression-eval tool that couldn't pass, by construction

The starting problem was simple: every time I changed `DiagnosisAgent`'s prompt or logic, there was no way to check "did this still find the right root cause in the incidents it used to get right" short of waiting for a new real incident and hoping it went well. So I built a replay tool — take real, merged-fix incidents, re-run them through the current `DiagnosisAgent`, and score PASS/FAIL against what actually shipped.

First run: 0 for 11, every time, no matter what I changed.

That's not a signal, that's a red flag. I dug in and found the actual cause: the replay used the target repo's **current** HEAD, but every case in the dataset was selected because its bug was already fixed (`outcome == "fix_merged"`) — meaning the code no longer contained the bug by definition. `DiagnosisAgent`'s own grounding gate — the mechanism that re-verifies every claimed file/function against the real repo before accepting a diagnosis — correctly refused to ground a diagnosis against code that didn't have the bug anymore. The eval was structurally guaranteed to fail on every case, independent of whether the agent had actually regressed.

The fix: check out each case at an isolated `git worktree`, pinned to the commit right before its fix landed, instead of the shared live clone that always tracks current HEAD.

Fixing that exposed a second bug, one layer deeper. The grounding gate verifies function names two ways — checking the local repo for file/snippet content, and hitting GitHub's Code Search API to confirm a symbol exists. GitHub Code Search only ever searches the **live default branch**. Pin the local clone to a historical commit all you want; the symbol check was still asking "does this exist today," not "did this exist back then." Fixed by branching: when the local repo is pinned to a historical SHA, grep the pinned worktree directly instead of hitting the live API.

Two real bugs, found by refusing to accept a suspiciously clean-looking 0% as a real result.

## Part 2: Testing generalization on a third-party benchmark

With the replay mechanism actually working, a bigger question opened up: everything this agent has ever been evaluated against is the one application it was built for. Does the underlying approach — search for candidate code, read it, verify every claim against the real repo before accepting a diagnosis — generalize at all, or is it just well-tuned pattern matching for one codebase's specific bug shapes?

SWE-bench Verified is a reasonable answer: 500 real GitHub issues and their real merged fixes, across 12 real open-source Python projects (Django, SymPy, scikit-learn, and others), human-validated. Structurally, it's the same task — find the file where the bug actually lives, scored against the real fix.

I scoped this deliberately narrow: **localization only**. Does the agent name the right file? Not "does it also generate a working fix and pass the repo's own tests" — that would require a whole separate piece of infrastructure (patch application, sandboxed test execution against arbitrary Python projects) that doesn't exist yet and shouldn't be built as a side effect of an eval.

One more discipline point before running anything at scale: the raw dataset is 46% one single repo. Running a random sample would have just re-tested Django six times and called it "generalization." I stratified the sample across all 12 repos instead — the same lesson a skewed *internal* eval dataset had already taught me earlier the same day (more on that in a second).

## Part 3: A humbling, explainable number

23 instances, stratified across all 12 repos, real API calls, no shortcuts. Result: **1 pass, 4.3%.**

The instinct here is to be embarrassed by that number. The better instinct is to explain it mechanistically instead of hand-waving it:

- The one thing that makes this agent effective at home is that production crashes hand it an explicit file path directly in the error text — Node.js stack traces are that literal. GitHub issues are prose. That single most-reliable discovery path simply doesn't exist for most of this benchmark.
- Semantic code search was intentionally disabled for this run (building a fresh embeddings index per repo has a real, avoidable cost), leaving only exact-string search — which needs an exact string, which prose rarely hands you.
- The agent's "have I seen something like this before" knowledge base is entirely specific to the one application it was built for. Zero relevance to a bug in a scientific computing library.

A flattering number would have told me nothing. A number I can explain, mechanically, told me exactly where the real constraints are.

## Part 4: Testing the leading hypothesis directly — and it was wrong

The obvious next move was "turn semantic search back on and see if the number moves." Instead of spending the money to re-run all 23 instances with a rebuilt search index, I tested the hypothesis on one repo first — cheap, fast, decisive.

Real embeddings, real semantic search, indexed in under six seconds. Same repo, same two instances that had failed before.

Same result. Both still failed. And more specifically: **the model never even attempted to finalize a diagnosis** — no rejected attempt, nothing, it just used its entire reasoning budget exploring and never converged to an answer at all. Giving it another tool to search with didn't reduce how long it took to converge; if anything, more options extended the exploration phase without it ever reaching a conclusion.

That's a clean, useful negative result. It ruled out "missing tool" as the explanation and pointed somewhere else entirely: something about how much room the agent gets to reach a conclusion, not which tools it has access to while reaching one.

## Part 5: The real bug

Chasing that "something else," I raised the agent's reasoning budget for one specific case and reran it. Same result, and — tellingly — it finished *faster*, not slower. If the budget were actually the constraint, more room should have meant more exploring, not less. That contradiction was the tell that I'd been looking at the wrong layer entirely.

Digging into what the agent actually produced (its internal loop discards this by design, so I had to instrument it to see): it had found the *right* file. It wrote a detailed, technically correct diagnosis, citing the real line of code responsible, and ended with something that read like "Status: diagnosis accepted."

It never called the tool that actually finalizes a diagnosis. It just wrote the conclusion as prose instead, and the system — correctly designed to require *some* evidence of real investigation before accepting an answer — had no way to tell "investigated thoroughly, then wrote up the answer instead of submitting it properly" apart from "never investigated at all." A correct diagnosis was silently discarded and scored as a total failure, at 0% confidence, with no file identified.

My first fix attempt required the finalizing tool to have been *called* at least once before an answer could be accepted. That closed most of the gap — and reopened it exactly one layer deeper. A **rejected** call still counts as "called." The agent called the tool, got rejected once, made a couple more exploratory moves, then wrote *another* free-text "the diagnosis has been accepted" and sailed straight through the same gap, just one attempt later.

The actual fix: stop checking whether the tool was called, and check whether it **succeeded**. The finalize mechanism already sets an internal flag only when a diagnosis genuinely passes every grounding check — file exists, function exists, code snippet actually matches. Gate on that flag directly, not on tool-call history.

Verified against the exact case that started this: before the fix, confidence 0%, no file identified. After — the agent retried eight times, got rejected eight times, corrected itself each time, and on the ninth attempt: the right file, 75% confidence, accepted for real.

## What this actually says

None of this went the way I expected going in. I set out to measure whether an agent generalizes to unfamiliar code, and the most valuable thing I found wasn't a generalization number — it was a real bug that had been silently discarding *correct* answers, on the agent's home turf as much as anywhere else, that a rigorous-looking internal eval had never surfaced because it wasn't designed to catch this specific failure shape.

The throughline across all five parts is the same instinct applied five times: don't trust a clean number, don't trust a suspiciously bad number, don't trust your own fix on the first try either. Every one of those checks caught something real. The measured 4.3% almost certainly understates what this agent can actually do — and I know that not because I assumed it, but because I found the specific mechanism proving it, fixed it, and watched one concrete case flip from a wrong, discarded answer to the right one.
