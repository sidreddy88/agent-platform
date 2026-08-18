# AllInterviews — Agent Guide

## What This Platform Is

Multi-brand magazine interview platform. Readers submit interview responses via a web form.
Content is moderated (keyword filter + OpenAI), reviewed by an admin, then published to
WordPress and S3. Multiple brands are served from a single codebase.

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Node.js 18 + Express |
| Database | MongoDB (Mongoose ODM) |
| Config | nconf (`config/index.js`) |
| Auth | JWT (`authenticateToken` middleware) |
| AI | OpenAI (content moderation via `prankCheckerOpenAI`) |
| Storage | AWS S3 (interview images) |
| Publishing | WordPress REST API |
| Frontend | React (`client/`) |

## Key Architectural Pattern — MODEL_MAP

Each brand has its own Mongoose model. The `MODEL_MAP` object routes operations to
the correct model by brand key:

```js
const MODEL_MAP = {
  inspiring: MasterInspiring,
  shoutout: MasterShoutout,
  cr: MasterCr,
  boldjourney: MasterBoldJourney,
};
```

The same pattern appears in `REFERRAL_MODEL_MAP` and the `PublishingApps` array in
`routes/services/interview-user-service.js`. Adding a brand requires updating all three.
See `docs/MULTI_BRAND.md` for the full checklist.

**This duplication is not limited to named constants like `MODEL_MAP`.** Whole route
handlers, middleware, and business logic get copy-pasted per brand — one near-identical
file per brand is a first-class pattern in this codebase, not an exception. **Assume a
bug found in one file exists in every sibling until you've actually searched and
confirmed otherwise — never conclude a fix is complete just because the one file from
the stack trace is fixed.**

**Search for the pattern, don't rely on a memorized file list.** Brands get added and
removed, and this duplication convention isn't confined to files named
`*InterviewUsers.js` — it can show up anywhere similar per-brand logic exists. When you
find a bug, use `search_codebase` / `grep_codebase` / `verify_symbol_in_repo` (whichever
surfaces raw text matches) to search for the same function signature, route path, or
vulnerable code shape across the whole repo — not just the one file the stack trace
pointed at — before finalizing a diagnosis.

**Finding the same code shape is not enough — confirm the bug is still there.** Two
separate checks are both required before you list a file in `blast_radius` or
`additional_fix`, not just one:
  1. Does this file contain the same code shape (same route, same query, same
     structure)?
  2. Does it *currently* still have the vulnerability — or has this specific
     occurrence already been fixed? Siblings get patched independently and
     asynchronously — a prior incident, a manual fix, an earlier PR that only touched
     some of them. A file matching the route/model naming pattern is not automatically
     still broken. Read the actual current code at that location (`get_file_contents`
     / `read_file`, not a memory of what it looked like earlier in this session) and
     check whether the SAME missing safeguard (validation, type check, `.catch()`) is
     genuinely still absent. A file that already has the fix is not part of the blast
     radius — recommending a "fix" for code that's already fixed wastes a real PR and
     confuses whoever reviews it.

For calibration, here's what this looked like in a real incident (illustrative, not an
exhaustive or current list — always search AND verify current state rather than trust
this): an unvalidated `previewCode` param cast to `Number` with no `.catch()` crashed
the process from `inspiringInterviewUsers.js`, and a proper search found the identical
handler verbatim in 7 more `routes/api/*InterviewUsers.js` siblings (artistOfTheDay,
boldJourney, cityNational, cr, highlightApp, shoutout, smallBusinessOfTheDay) — at the
time, none had crashed yet but all had the same bug waiting to. By the time a later
incident re-diagnosed this exact bug, 5 of those 7 had already been fixed (in earlier,
separate incidents) and only 2 were still genuinely vulnerable — but the diagnosis
listed all 7 as needing the fix again, because it matched on route/naming pattern
without re-checking each file's current code for the actual guard.

List every affected file you actually find AND confirm is still vulnerable in
`blast_radius` — not files that already have the fix, and not just the one from the
stack trace.

## Content Moderation Pipeline

`hardBlock()` runs first (free, keyword-based). Only if it passes does `classifyFields()`
call OpenAI (costs money). Never swap this order. See `docs/CONTENT_MODERATION.md`.

## Running Locally

```bash
npm install
npm run dev          # starts Express server

cd client
npm install
npm start            # starts React dev server
```

## Running Tests (from this harness directory)

```bash
npm run check        # runs all tests
npm test             # jest --forceExit
```

Tests live in `tests/` alongside this file. They are authored to run from the AllInterviews
repo root (relative imports like `../../constants/helperFunctions`).

## Topic Docs

| Topic | File |
|---|---|
| Production safety rules + architectural invariants | `CONSTRAINTS.md` |
| Architectural decisions with rationale | `DECISIONS.md` |
| Deferred features with pre-conditions | `FUTURE.md` |
| End-to-end publish pipeline | `docs/PUBLISH_PIPELINE.md` |
| How to add a new brand | `docs/MULTI_BRAND.md` |
| Content moderation architecture | `docs/CONTENT_MODERATION.md` |
| Session handoff + bootstrap contract | `PROGRESS.md` |
| Module health grades | `QUALITY.md` |

---

## Work Rules

1. **WIP=1.** Work on exactly one task at a time. Finish (tests passing, PR open) before
   starting the next.

2. **Sprint contract before non-trivial work.** Write this before touching any code:
   ```
   Scope:      <exactly what files/functions will change>
   Exclusions: <what will NOT change>
   Done when:  <acceptance criterion — specific and verifiable>
   ```

3. **Do not expand scope mid-task.** If you discover related work, record it in PROGRESS.md
   under Next Steps and finish the current task first.

4. **Verification order.** A task is done only when:
   - `npm run lint` passes (Phase 4+)
   - `npm test` passes — no new failures
   - New behavior has at least one test
   - No CONSTRAINTS.md violations
   - Only files in the sprint contract scope were modified

5. **PR discipline.**
   - Run `npm test` locally before opening a PR. All tests must pass.
   - Open PR only when tests are green.
   - Never merge to master yourself — a human reviews and merges.

6. **Commit discipline.**
   - One commit per logical unit of work (not one commit per session).
   - Format: `type: what was done and why` (feat/fix/test/docs).
   - Never bundle unrelated changes in one commit.

7. **PROGRESS.md is the single source of truth.** Update it at the start and end of every
   session. Do not maintain a parallel list anywhere else.

8. **Session exit checklist** (verify all five before ending a session):
   - [ ] `npm test` passes — no new failures
   - [ ] PROGRESS.md "Current State" is accurate
   - [ ] "Next Steps" has a concrete first action
   - [ ] No debug `console.log` or temp files in modified code
   - [ ] Active branch has a PR open, or next step recorded in In Progress
