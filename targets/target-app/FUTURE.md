# TargetApp — Deferred Features

Items here have clear pre-conditions. Do not start them until the pre-condition is met.

---

## F1 — Stage 2: migrate tests + harness docs to TargetApp permanently

**What:** Move `agent-platform/targets/target-app/` contents into the TargetApp
repo root. Enable branch protection on `master` (require `test` CI check before merge).
Remove tests from agent-platform once they live in TargetApp.

**Why deferred:** Tests must prove stable across several agent PRs before committing
them permanently to a repo used by thousands of people. A bad test in TargetApp
blocks all PRs until fixed.

**Pre-condition:** Tests are green across 5+ agent PRs on TargetApp.

---

## F2 — Branch protection on `master` (TargetApp)

**What:** Enable branch protection requiring the `test` CI check to pass before any PR
can be merged to `master`.

```bash
gh api repos/TargetOrg/TargetApp/branches/master/protection \
  --method PUT \
  --field required_status_checks='{"strict":true,"contexts":["test"]}' \
  --field enforce_admins=false \
  --field required_pull_request_reviews=null \
  --field restrictions=null
```

**Why deferred:** Branch protection with a required check only makes sense once the
check is reliably green. Enabling it before Stage 2 would lock out merges.

**Pre-condition:** F1 (Stage 2 migration) complete.

---

## F3 — Frontend React tests (`client/src/`)

**What:** React Testing Library tests for admin UI components in `client/src/components/Admin/`.

**Why deferred:** Backend tests stabilize first. React test infra (jest + jsdom + RTL)
is separate from the backend test setup and adds complexity.

**Pre-condition:** Backend tests proven stable (F1 complete).

---

## F4 — `classifyFields` unit tests

**What:** Unit tests for the OpenAI classification function — mock the OpenAI client,
test JSON parsing, null handling, and the safe fallback (block) path.

**Why deferred:** The E2E flagged checker tests already cover the full moderation flow.
`classifyFields` unit tests add value but are not blocking.

**Pre-condition:** Phase 1 E2E tests stable across 3+ runs.

---

## F5 — Harness Simplification Protocol

**What:** Monthly review pass to remove constraints and arch-check rules that model
capability improvements have made unnecessary. Constraints that guard against model
weaknesses that no longer exist add noise and reduce agent autonomy unnecessarily.

**Why deferred:** Needs a benchmark suite to verify that removing a constraint doesn't
reintroduce the original failure mode.

**Pre-condition:** Benchmark suite in place (not yet scoped).
