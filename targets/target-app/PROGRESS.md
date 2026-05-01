# TargetApp — Progress

Single source of truth for session handoff. Update at the start and end of every session.

---

## Bootstrap Contract

Before starting any work, verify all of the following are true:

- [ ] `npm install` completes without error (in TargetApp repo root)
- [ ] `cd client && npm install` completes without error
- [ ] `npm test` passes (only documented known failures below are allowed)
- [ ] "Current State" section below is accurate
- [ ] "Next Steps" has a concrete first action

If any item fails, fix it before starting new feature work.

---

## Current State

Phase 1 test infrastructure authored in `agent-platform/targets/target-app/`. Not yet
pushed to TargetApp. Phase 2 harness docs also complete in agent-platform.

Tests have not been run against the live TargetApp codebase yet — they are authored
to work once pushed to the TargetApp repo root (relative imports assumed from there).

Two source patches documented but not yet applied to TargetApp:
- `patches/config-index.patch.md` — add "test" to valid NODE_ENV values
- `patches/server-export.patch.md` — export app, guard app.listen()

---

## Session Exit Checklist

Before ending any session, verify all five:

- [ ] `npm test` passes — no new failures introduced
- [ ] "Current State" section is accurate
- [ ] "Next Steps" has a concrete first action
- [ ] No debug `console.log` or temp files left in modified code
- [ ] Active branch has a PR open, or next step is recorded in In Progress below

---

## In Progress

*(nothing active)*

Sprint contract template:
```
Scope:      <exactly what files/functions will change>
Exclusions: <what will NOT change>
Done when:  <acceptance criterion — specific and verifiable>
```

---

## Next Steps

1. **Push Phase 1 test suite to TargetApp as a draft PR**
   - Apply patches: `config-index.patch.md` + `server-export.patch.md`
   - Add devDependencies + scripts from `package-overrides.json` to TargetApp package.json
   - Commit test files + CI workflow + config.test.json via GitHub API
   - Open draft PR to TargetApp `master`
   - Wait for CI green, then convert to ready for review
   - Done when: CI `test` workflow is green on the PR branch
   - State: **not started**

2. **Phase 4 — ESLint + arch-check**
   - Write `.eslintrc.json` and `scripts/arch-check.js` in `targets/target-app/`
   - State: **not started** — pre-condition: Phase 1 PR green

---

## Known Issues

*(none)*

---

## Completed

| Task | PR | Notes |
|---|---|---|
| Phase 1: 3-layer test suite authored in agent-platform | (not yet pushed) | 14 files, ~700 lines |
| Phase 2: AGENTS.md, CONSTRAINTS.md, DECISIONS.md, docs/, FUTURE.md | (not yet pushed) | All in agent-platform |
