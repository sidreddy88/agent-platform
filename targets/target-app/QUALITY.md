# Target App — Module Health

Grades reflect test coverage, complexity, and incident history.
Update when a module's health materially changes.

Scale: **A** = well-tested, clean, low risk | **B** = adequate coverage, some gaps |
**C** = limited coverage, higher risk | **D** = no coverage, high risk

---

## Modules

### Content Moderation (`constants/prankCheckerMain.js`, `constants/prankCheckerOpenAI.js`)
**Grade: B**

- Unit tests added (Phase 1): `hardBlock`, `looksLikePrank` fully covered
- E2E tests added (Phase 1): `runPrankChecker` full flow covered
- Incident history: production crash from unguarded JSON.parse (fixed — see DECISIONS.md D6)
- Gap: `classifyFields` unit tests deferred (see FUTURE.md F4)

---

### Publish Pipeline (`routes/services/interview-user-service.js`)
**Grade: C**

- No unit or integration tests
- Most complex file in the codebase: S3 upload + WordPress API + MongoDB update in one function
- External dependencies (S3, WordPress) make unit testing harder but not impossible
- Risk: partial-failure states (S3 succeeds, WordPress fails) are not tested

Upgrade path: integration test with mocked S3 + WordPress clients, real mongodb-memory-server.

---

### Referral System (`routes/services/referral-service.js`)
**Grade: B**

- Integration tests added (Phase 1): `isTrashReferral`, `isDoNotContactReferral`, `isDuplicateReferral`
- Uses `count()` (deprecated in Mongoose 6, still functional in 6.8.3 — monitor for removal)
- Gap: `createReferral` end-to-end path not covered

---

### Admin UI (`client/src/components/Admin/`)
**Grade: D**

- No tests (React Testing Library not set up)
- Large surface area — multiple admin views
- Deferred to FUTURE.md F3

---

### Mongoose Models (`models/`)
**Grade: B**

- Production data — model changes are high-risk
- No migration strategy documented
- Constraint enforced: never remove fields, always add defaults (see CONSTRAINTS.md)
- Gap: no automated schema validation tests

---

### API Routes (`routes/api/`)
**Grade: B**

- Thin wrappers over service layer
- Health check covered by E2E test (Phase 1)
- Prank checker route covered by E2E test (Phase 1)
- Gap: submission and publish routes not yet covered

---

### Config + Auth (`config/index.js`, `routes/middleware/authenticateToken.js`)
**Grade: A**

- nconf hierarchy is clean and well-documented
- JWT auth is stateless, no shared session state
- `assertExists` + `assertInValues` provide startup-time contract validation
- Low risk — rarely changes

---

### Helper Functions (`constants/helperFunctions.js`)
**Grade: A**

- Pure functions, all covered by unit tests (Phase 1)
- Zero external dependencies
- High confidence

---

### Parser Utilities (`routes/services/parser.js`)
**Grade: A**

- Pure functions, covered by unit tests (Phase 1)
- Zero external dependencies

---

## Upgrade Priorities

1. Publish Pipeline (C → B): add integration tests with mocked S3 + WordPress
2. Admin UI (D → C): add React Testing Library setup + smoke tests
3. Referral System (B → A): cover `createReferral` path
