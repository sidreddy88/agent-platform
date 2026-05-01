# AllInterviews — Architectural Decisions

Decisions are recorded here so agents don't re-litigate them. Each entry includes the
rationale and the constraint that follows from it.

---

## D1 — One Mongoose model per brand (not shared model + discriminator)

**Decision:** Each brand has its own Mongoose model and MongoDB collection.

**Why:** Interview question structures differ significantly across brands. A shared model
with a discriminator would require a superset schema with many optional fields, making
validation meaningless and queries harder to index. Separate models let each brand
evolve its schema independently.

**Constraint:** Adding a brand requires a new model file + registration in MODEL_MAP.
See `docs/MULTI_BRAND.md`.

---

## D2 — MongoDB over SQL

**Decision:** MongoDB (Mongoose) is the primary datastore.

**Why:** Interview submissions have brand-specific question/answer structures that vary
over time. A flexible document schema avoids costly migration overhead each time a brand
adds or renames a question field. SQL foreign keys and rigid schemas would make this
harder.

**Constraint:** Never use SQL for interview data. Do not add a relational DB dependency.

---

## D3 — `hardBlock` runs before `classifyFields`

**Decision:** The two-phase moderation runs keyword filter first, OpenAI second.

**Why:** `hardBlock` is free (in-process string matching). `classifyFields` makes an
OpenAI API call that costs money per request. Running the cheap check first short-circuits
the expensive one for obvious spam. See `docs/CONTENT_MODERATION.md`.

**Constraint:** The order is mandatory. Never call `classifyFields` before `hardBlock`.

---

## D4 — JWT for authentication (stateless)

**Decision:** Admin routes use JWT middleware (`authenticateToken`).

**Why:** The platform runs across multiple brand subdomains. Stateless JWT tokens work
without shared session storage — each request is self-validating. A session store would
require a centralized Redis or DB session table shared across deployments.

**Constraint:** Do not introduce session-based auth. New protected routes must use the
`authenticateToken` middleware.

---

## D5 — nconf config hierarchy with `assertExists` / `assertInValues`

**Decision:** All config is loaded via nconf in `config/index.js`. Missing or invalid
values cause an immediate `process.exit(1)`.

**Why:** Failing fast on bad config is safer than discovering a missing key mid-request.
The `assertExists` and `assertInValues` guards make the full config contract explicit and
machine-checkable at startup.

**Constraint:** Adding a new config key requires: (1) adding `assertExists` in
`config/index.js`, (2) adding the key to all environment config files including
`config/config.test.json`.

---

## D6 — `response_format: { type: "json_object" }` on `classifyFields`

**Decision:** The OpenAI call in `classifyFields` always sets `response_format: { type: "json_object" }`.

**Why:** In a production incident, OpenAI returned a non-JSON response (plain English
explanation instead of structured output). `JSON.parse` threw, the catch block was
missing, and the moderation route crashed. Adding `response_format` constrains the model
to always return valid JSON, preventing the parse failure.

**Constraint:** Never remove `response_format` from the `classifyFields` OpenAI call.
See CONSTRAINTS.md and `docs/CONTENT_MODERATION.md`.

---

## D7 — Stage 1 / Stage 2 test infrastructure rollout

**Decision:** Tests and harness docs live in `agent-platform/targets/allinterviews/`
during Stage 1. They migrate to the AllInterviews repo in Stage 2.

**Why:** Stage 1 lets the test suite prove itself (green across 5+ agent PRs) before
committing it permanently to a production repo used by thousands of people. The cost of
a bad test in a production repo is higher than the cost of the temporary split.

**Pre-condition to move to Stage 2:** 5+ agent PRs with green CI on AllInterviews.
See `FUTURE.md`.
