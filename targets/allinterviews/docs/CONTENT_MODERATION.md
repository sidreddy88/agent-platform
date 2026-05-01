# Content Moderation Architecture

The prank/spam detection system that gates every interview submission.

---

## Two-Layer Architecture

```
Layer 1: hardBlock()          — keyword filter, free, in-process
Layer 2: classifyFields()     — OpenAI GPT call, costs money per request
```

Layer 1 always runs first. Layer 2 only runs if Layer 1 passes. This is a cost gate —
most spam is caught cheaply. See DECISIONS.md D3 and CONSTRAINTS.md.

---

## Layer 1 — `hardBlock()`

File: `constants/prankCheckerMain.js`

Checks each answer field against a list of banned words and phrases. The match pattern
uses surrounding spaces (` word `) to avoid false positives on substrings.

```js
// Example: " badword " matches but "badwords" does not
const pattern = ` ${word} `;
if (text.toLowerCase().includes(pattern)) return true;
```

Returns `true` (blocked) if any field contains a banned word. Returns `false` (pass) if
all fields are clean.

**Adding to the block list:** Add words/phrases to the constants array in
`constants/prankCheckerMain.js`. Test with the unit tests in `tests/unit/prankChecker.test.js`.
Use surrounding space padding if you want exact-word matching.

---

## Layer 2 — `classifyFields()`

File: `constants/prankCheckerOpenAI.js`

Sends the interview answers to OpenAI for semantic classification. Returns a JSON object
with per-field moderation scores or block flags.

**Critical requirement:** The API call MUST include `response_format: { type: "json_object" }`.

**Why this is required (production incident):** Without `response_format`, OpenAI
occasionally returns a plain-English explanation instead of JSON. `JSON.parse` throws,
the route crashes, and all submissions are blocked until the server restarts.

**Safe fallback for null response:** If `classifyFields` returns null or throws, the
caller must treat the result as a block, not a pass. The safe default for unknown
moderation state is to reject the submission.

---

## `looksLikePrank()`

A lighter heuristic in `constants/prankCheckerMain.js` that checks for patterns
suggesting the submission is low-effort or a test (e.g., repeated characters, very short
answers, nonsense strings). Returns `true` if the submission looks like a prank.

This is distinct from `hardBlock()`:
- `hardBlock` catches explicit banned content (hard block — submission is definitely spam)
- `looksLikePrank` flags low-effort submissions (softer signal — may warrant manual review)

---

## `PrankCheckerLog` Model

File: `models/PrankCheckerLog.js`

Every moderation decision (pass or block) is logged to MongoDB with:
- The original submission fields
- Which layer blocked it (if blocked)
- The `classifyFields` response (if called)
- Timestamp

This log is used to audit moderation decisions and tune word lists.

---

## When to Add to the Word List vs. Improve the OpenAI Prompt

| Situation | Action |
|---|---|
| New explicit spam phrase (specific, unambiguous) | Add to `hardBlock` word list |
| Pattern requiring semantic understanding | Improve `classifyFields` prompt |
| High false-positive risk | Improve `classifyFields` prompt, not `hardBlock` |
| Recurring prank pattern (short/nonsense answers) | Tune `looksLikePrank` heuristics |

Prefer `hardBlock` for cost efficiency. Use `classifyFields` prompt tuning for nuanced
cases where keyword matching would produce false positives.

---

## Test Coverage

`tests/unit/prankChecker.test.js` covers:
- `hardBlock()` — clean text, single match, phrase match, case-insensitive, boundary (no false positive on substrings)
- `looksLikePrank()` — clean submission, repeated characters, very short answers, nonsense

`tests/e2e/prankChecker.test.js` covers the full `runPrankChecker` function:
- Clean text → `ok: true`
- Moderation flagged → `ok: false`
- `classifyFields` returns null → `ok: false, error: "OpenAI classification failed"`
- `classifyFields` returns block → `ok: false, errors array`
