# Plan: split `targets/target-app/` into a separate private repo

**Status:** deferred — not started. Revisit when ready to act on it.

## Why

`targets/target-app/` was already genericized in place (org/repo identity strings
like the target's company/app name removed — see git history for that PR). But it
still contains content that **can't** be safely genericized the same way: real
per-brand and Mongoose-model names (e.g. brand keys and their corresponding
`Master*` model names referenced in `AGENTS.md`, `docs/MULTI_BRAND.md`, and
`tests/integration/referralService.test.js`).

Those aren't cosmetic branding — they're ground-truth vocabulary the agent
actually depends on:
- `tests/integration/referralService.test.js` mocks real model file paths
  (`jest.mock('../../models/MasterX', ...)`); this file gets copied into the
  sandbox and run against the real target repo during fix validation. If the
  names don't match real files there, the mocks fail to resolve.
- `AGENTS.md`'s `MODEL_MAP` snippet and its real-incident calibration example
  get injected into every `DiagnosisAgent` call. Swapping in fake brand names
  would make the injected context disagree with what the model actually finds
  when it reads the real repo via `get_file_contents` — the same class of
  context-vs-reality mismatch that caused several hallucinations found and
  fixed this session.

So genericizing this content in place would either break real functionality or
require a translation/alias layer (map fake names ↔ real names at every tool-call
boundary) — a bigger, riskier piece of work that adds a new place for a silent
mismatch bug to hide.

**Conclusion:** if the goal is "the public `agent-platform` repo contains zero
brand-identifying content," the right tool is moving this content to a **private**
separate repo, not further in-place scrubbing.

## Scope

### 1. New private repo
Extract `targets/target-app/` (currently ~108K) into a new private repo, e.g.
`agent-platform-target-harness`. Fresh copy without history is fine — the content
is small and its history isn't independently valuable outside agent-platform's
own commit narrative. ~30–60 min.

### 2. Runtime fetch mechanism — the real design decision

| | Build-time (`git clone` in Dockerfile) | Startup-time (entrypoint fetch) | S3-backed |
|---|---|---|---|
| Auth | PAT/deploy key as a Docker build secret | PAT/deploy key via SSM (matches existing secret pattern) | IAM role only — no token to rotate |
| Update cadence | Requires image rebuild to pick up harness changes | Picked up on next redeploy/restart — no rebuild needed | Same as startup-time |
| New infra | None | None (SSM param) | New S3 bucket + IAM policy + a sync step in the harness repo's own CI |
| Failure mode | Fails the *build* (safe, caught in CI) | Fails at *runtime* in prod — must degrade gracefully + alert | Same as startup-time |
| Fits existing patterns | Partial | Good (SSM-managed secrets already the norm here) | Best (this codebase already prefers OIDC/IAM over static tokens — see `infra/agent_platform/github_oidc.tf`) |

**Recommendation:** S3-backed, fetched at container startup. No token to rotate,
reuses IAM roles this stack already trusts (`task_execution_role_arn` /
`task_runtime_role_arn` already exist as Terraform outputs), and decouples
"harness content changed" from "code changed" — a redeploy naturally picks up
the latest harness content, no cross-repo CI triggering needed.

Build-time git-clone is the fastest to stand up first if starting simple and
migrating later is preferred instead.

### 3. Code changes needed, regardless of which option is chosen
- `app/services/sandbox.py` (`_TARGETS_DIR`) and `app/core/config.py`
  (`harness_docs_path`) — **no changes**. Both already just expect content to
  exist at `targets/target-app/` inside the container; only *how* it gets there
  changes.
- New: an entrypoint script. The Dockerfile currently has no custom entrypoint —
  just `CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]`.
  The fetch step needs to run before that.
- New: an explicit failure check after the fetch. `BaseAgent._load_harness_docs()`
  (`app/agents/base.py`) already fails silently open on any exception (returns
  `""`, no crash) — good for uptime, bad for visibility. A fetch failure must log
  at ERROR/alert level, not just silently degrade, or this becomes the next
  multi-month undetected outage (same shape as the Langfuse-keys and
  `FIX_TARGET_REPO` incidents already found this session).

### 4. CI cleanup
`.github/workflows/deploy.yml`'s `paths-ignore` exception for
`targets/target-app/AGENTS.md`/`CONSTRAINTS.md` becomes dead code once those
files no longer live in this repo — remove it.

### 5. Rough total effort
Half a day to a full focused session (repo setup + infra/IAM + entrypoint script
+ failure-path testing + live verification), not a quick PR. Same rigor as every
other config-wiring change from this session: test the failure path explicitly,
verify live in the actual container, not just green CI.

## Open decision when resuming this

Pick one of the three fetch mechanisms above before starting implementation.
Recommendation is S3-backed + startup-time fetch; build-time git-clone is the
faster-to-implement fallback if this needs to ship sooner with less new
infrastructure.
