# Multi-Brand Architecture

How the platform serves multiple magazine brands from one codebase.

---

## How Brands Are Identified

Each API request carries a brand identifier in the request body (field varies by route —
`userType`, `brand`, or similar). The service layer maps this to the correct Mongoose model
via `MODEL_MAP`.

---

## The MODEL_MAP Pattern

```js
// routes/services/interview-user-service.js (and master-service.js)
const MODEL_MAP = {
  inspiring:   MasterInspiring,
  shoutout:    MasterShoutout,
  cr:          MasterCr,
  boldjourney: MasterBoldJourney,
  // ...other brands
};

// Usage:
const Model = MODEL_MAP[userType];
if (!Model) throw new Error(`Unknown userType: ${userType}`);
```

The same pattern appears in:
- `MODEL_MAP` — interview submissions
- `REFERRAL_MODEL_MAP` — referral records
- `PublishingApps` array — WordPress publish config per brand

---

## Adding a New Brand — Full Checklist

Do all of these in a single PR. Missing any one will cause runtime errors for the new brand.

- [ ] Create `models/Master<BrandName>.js` — Mongoose schema for the brand's questions
- [ ] Create `models/Referral<BrandName>.js` — Mongoose schema for referral records
- [ ] Add to `MODEL_MAP` in `routes/services/interview-user-service.js`
- [ ] Add to `REFERRAL_MODEL_MAP` in `routes/services/interview-user-service.js`
- [ ] Add to `PublishingApps` array with WordPress credentials config key
- [ ] Add to `MODEL_MAP` in `routes/services/master-service.js`
- [ ] Add brand key to `constants/index.js` brand arrays (check what arrays exist)
- [ ] Add WordPress config key to `config/index.js` `assertExists` checks
- [ ] Add WordPress config values to `config/config.development.json`,
     `config/config.production.json`, and `config/config.test.json`
- [ ] Write at least one integration test verifying the new brand routes correctly
     through MODEL_MAP

---

## Current Brands

| Brand key | Mongoose Model |
|---|---|
| `inspiring` / `voyage` | MasterInspiring |
| `shoutout` | MasterShoutout |
| `cr` | MasterCr |
| `boldjourney` | MasterBoldJourney |
| `citynational` | MasterCityNational |
| `music` | MasterMusic |
| `highlight` | MasterHighlight |
| `inspiringseries` | MasterInspiringSeries |

(Verify against current MODEL_MAP in source — this table may lag.)

---

## Why One Model Per Brand (not discriminators)

See DECISIONS.md D1. Short version: each brand has a materially different question
structure. A shared model with optional fields makes validation useless and makes it
harder to enforce brand-specific required fields.

---

## Testing Multi-Brand Changes

When changing shared service logic (master-service, referral-service, interview-user-service):

1. Run the full test suite — all MODEL_MAP entries are exercised.
2. Verify the change doesn't assume a brand-specific field is universally present.
3. If adding a field to one brand's model, do not reference it in shared service code
   without a null/undefined guard.
