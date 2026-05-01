# Publish Pipeline

End-to-end flow from form submission to published article.

---

## Overview

```
Reader submits form
       │
       ▼
POST /api/validationCheck/validate
       │
       ├── hardBlock() — keyword filter (free, in-process)
       │     ├── BLOCKED → return ok:false, log to ValidationLog
       │     └── PASS →
       │           classifyFields() — OpenAI call (paid)
       │                 ├── null response → ok:false, "OpenAI classification failed"
       │                 ├── block field set → ok:false, errors array
       │                 └── PASS → ok:true
       │
       ▼ (ok:true)
POST /api/interview-user/submit
       │
       ├── Validate JWT (authenticateToken middleware)
       ├── Save submission to brand Mongoose model
       └── Create referral record
       │
       ▼
Admin reviews submission in admin UI
       │
       ├── Reject → archived, no further action
       └── Approve →
             POST /api/interview-user/publish
                   │
                   ├── Upload images to S3
                   ├── POST to WordPress REST API (creates draft/published post)
                   └── Mark submission published in MongoDB
```

---

## Key Files

| File | Role |
|---|---|
| `routes/api/validationCheck.js` | Content moderation route |
| `constants/validationMain.js` | `hardBlock()` and `looksLikeFlagged()` logic |
| `constants/validationOpenAI.js` | `classifyFields()` — OpenAI API call |
| `routes/api/interview-user.js` | Submission + publish routes |
| `routes/services/interview-user-service.js` | Business logic, MODEL_MAP, S3, WordPress |
| `routes/services/referral-service.js` | Referral creation + duplicate detection |
| `models/` | One Mongoose model per brand |

---

## Content Moderation Detail

See `docs/CONTENT_MODERATION.md` for the full moderation architecture.

Critical invariants:
1. `hardBlock` always runs before `classifyFields` (cost gate)
2. `classifyFields` always uses `response_format: { type: "json_object" }` (incident fix)
3. Null `classifyFields` response → block (safe default)

---

## Publish Step Detail

`interview-user-service.js` handles publishing. It:

1. Finds the submission in the correct brand model (via MODEL_MAP).
2. Uploads each image in the submission to S3 (`aws.bucket`).
3. Constructs the WordPress post body from the submission fields.
4. Calls the WordPress REST API (`POST /wp-json/wp/v2/posts`) with Basic auth.
5. On WordPress success, updates the MongoDB document's `published` flag.

Failures at S3 or WordPress do not automatically roll back MongoDB state — if the
publish step partially fails, the submission stays in "pending" state and can be
retried from the admin UI.

---

## Referral System

Every submission creates a referral record (who referred whom). The referral service
(`routes/services/referral-service.js`) checks three conditions before saving:

- `isTrashReferral` — known spam patterns
- `isDoNotContactReferral` — suppression list
- `isDuplicateReferral` — already exists for this email + brand

If any check returns true, the referral is silently dropped. The interview submission
proceeds regardless.

---

## Multi-Brand Routing

The same Express routes serve all brands. Brand is determined by a field in the request
body (e.g., `userType` or `brand`). Model selection happens in the service layer via
MODEL_MAP and REFERRAL_MODEL_MAP. See `docs/MULTI_BRAND.md`.
