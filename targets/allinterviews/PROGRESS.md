# AllInterviews — Progress

Single source of truth for session handoff. Update at the start and end of every session.

---

## Bootstrap Contract

Before starting any work, verify all of the following are true:

- [ ] `npm install` completes without error (in AllInterviews repo root)
- [ ] `cd client && npm install` completes without error
- [ ] `npm test` passes (only documented known failures below are allowed)
- [ ] "Current State" section below is accurate
- [ ] "Next Steps" has a concrete first action

If any item fails, fix it before starting new feature work.

---

## Current State

All four phases authored in `agent-platform/targets/allinterviews/`. Not yet pushed to
AllInterviews. Tests are authored to work once pushed to the AllInterviews repo root
(relative imports assumed from there).

Two source patches documented but not yet applied to AllInterviews:
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

1. **Push harness to AllInterviews as a PR**
   - Apply patches: `config-index.patch.md` + `server-export.patch.md`
   - Add devDependencies + scripts from `package-overrides.json` to AllInterviews package.json
   - Commit all harness files (tests, docs, .eslintrc.json, scripts/) via GitHub API
   - Run `npm test` locally — must be green before opening PR
   - Open PR to AllInterviews `master`
   - Done when: `npm test` passes locally and PR is open
   - State: **not started**

2. **Open PR to AllInterviews for Phase 4 files**
   - `.eslintrc.json`, `scripts/arch-check.js` — go in the same PR as Phase 1 or a follow-up
   - State: **not started**

---

## Known Issues

*(none)*

---

## Completed

| Task | PR | Notes |
|---|---|---|
| Phase 1: 3-layer test suite authored in agent-platform | (not yet pushed) | 14 files, ~700 lines |
| Phase 2: AGENTS.md, CONSTRAINTS.md, DECISIONS.md, docs/, FUTURE.md | (not yet pushed) | All in agent-platform |
| Phase 3: PROGRESS.md, QUALITY.md | (not yet pushed) | All in agent-platform |
| Phase 4: .eslintrc.json, scripts/arch-check.js, package-overrides.json | (not yet pushed) | All in agent-platform |
