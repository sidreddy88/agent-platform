# AllInterviews — Constraints

Hard rules. Violations block PR approval.

---

## Production Data Safety

- **NEVER drop MongoDB collections.** Dropping a collection in production destroys all
  interview submissions for that brand. Use migrations that add/rename only.

- **NEVER remove fields from Mongoose models.** Existing documents will fail validation
  on next read. Only add new fields, always with a default value.

- **New required fields MUST have a default value.** This keeps existing documents valid
  without a backfill migration.

---

## Content Moderation Pipeline

- **Always run `hardBlock()` before `classifyFields()`.** `hardBlock` is a keyword filter
  (free). `classifyFields` calls OpenAI (costs money per call). Swapping the order wastes
  API budget on submissions that would have been blocked for free.

- **`classifyFields()` MUST use `response_format: { type: "json_object" }`.** Without
  this, OpenAI sometimes returns plain text instead of JSON, causing `JSON.parse` to throw
  and crash the moderation route. This happened in production — do not remove the option.

- **Always handle a null/empty `classifyFields` response with a safe fallback (block).**
  OpenAI can return null. The safe default is to block the submission, not to approve it.

---

## Multi-Brand Changes

- **Changing shared service logic → verify all MODEL_MAP entries still pass tests.**
  A change to `master-service.js` or `referral-service.js` affects all brands.

- **Adding a new brand → update all three registries:** `MODEL_MAP`, `REFERRAL_MODEL_MAP`,
  and the `PublishingApps` array in `routes/services/interview-user-service.js`.
  See `docs/MULTI_BRAND.md` for the full checklist.

---

## Deployment

- **NEVER push directly to `master`.** The `master.yml` workflow deploys to production
  ECS immediately on push. Every change goes through a branch + PR.

- **PRs must have a green CI run before converting from draft to ready for review.**
  A failing test means the feature is not done.

---

## Review Feedback Promotion

When a code review catches a recurring violation pattern, encode it as an arch-check rule:
add a grep-based check to `scripts/arch-check.js` with an agent-oriented error message
(what the violation is, why it's banned, how to fix it).

Loop: review catches pattern → arch-check encodes it → pattern never recurs.
