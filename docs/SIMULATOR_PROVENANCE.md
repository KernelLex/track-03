# Simulator Provenance

DEVDOC_v6 §5.5: "Never invent behaviour." This document cites exactly where
every piece of `SimulatedRail` and `data/failure_taxonomy.yaml` came from,
and says plainly where a source is weaker than the spec asked for.

## 1. Failure taxonomy (`data/failure_taxonomy.yaml`)

Fetched 2026-08-30 from:

- `https://razorpay.com/docs/errors/payments/cards/`
- `https://razorpay.com/docs/errors/payments/upi/`

These are Razorpay's own public documentation pages, fetched via an
automated HTML-to-text tool and cross-checked by hand against the resulting
table before it was written into the taxonomy file. **This is weaker than
DEVDOC_v6 §5.5's primary source**, which names
`razorpay.com/docs/build/browser/assets/images/payments_error_reasons.xlsx`
— a binary spreadsheet the tooling available in this build pass could not
fetch and parse. The two error pages above cover the same content in
prose/table form and were used instead. **Action before relying on this in
a submission:** download the actual spreadsheet and diff it against
`data/failure_taxonomy.yaml`'s `codes` section.

The `disposition` field (`RETRYABLE` / `TERMINAL`) is **not** copied from
either source page's own "is this retryable" language — it answers a
different, narrower question ("should the system schedule another
`retry_charge` for this"), following the logic in DEVDOC_v6 §11.2's own
four worked examples. Six entries where this build's judgment call departs
from the source page's framing are individually justified with a `note:`
field in the YAML itself (e.g. `payment_risk_check_failed` is marked
TERMINAL here despite the source page marking it retryable, because
auto-retrying a fraud-flagged charge is the aggressive-bot behaviour this
whole system exists to avoid).

**Not sourced at all yet:** NACH/eNACH mandate return codes (needs NPCI's
return-code circular, not pulled into this pass). §12.3's `REPEAT_NSF`
detector works off a *count* of consecutive returns, not specific reason
codes, so it isn't blocked by this gap — but a human-readable "why did this
mandate debit bounce" string is not yet available. See LIMITATIONS.md.

## 2. Object shapes (`agent/rails/types.py`, `agent/rails/simulated.py`)

DEVDOC_v6 §5.5 names the official SDK repos
(`razorpay/razorpay-python`, `-node`, `-ruby`) and their test fixtures as
the source for real response payloads. **This build used the installed
`razorpay-python` package's own client interface** (verified directly —
see below) for method names and resource structure, but did **not** pull
and diff against the SDK's actual test fixture JSON files, since no network
access to that specific repo content was exercised in this pass. The field
sets on `Order`, `PaymentLink`, `Invoice`, `Mandate`, `DebitResult` in
`agent/rails/types.py` are a **reasonable, reduced subset** of the real
API's fields (enough for the shape/transition conformance DEVDOC_v6 §5.4
scopes as in-bounds), not a verified byte-for-byte mirror.

What *was* directly verified, by inspecting the installed package
(`razorpay==2.0.1`) at build time rather than assuming it:

- `razorpay.errors` exposes exactly `BadRequestError`, `GatewayError`,
  `ServerError`, `SignatureVerificationError` — used in `tools/probe_rails.py`.
- `client.order`, `.payment_link`, `.invoice`, `.customer`, `.plan`,
  `.subscription` all expose `.create()` and `.all()`; `client.settlement`
  exposes only `.all()` (no `.create()`, correctly reflected in the probe).

**Action before relying on this in a submission:** clone
`razorpay/razorpay-python`, pull its test fixtures into `data/rail_fixtures/`
(the directory exists, empty, for exactly this), and diff `SimulatedRail`'s
emitted object shapes against them.

**Update, 2026-08-30 — this has now partly happened for real.** With live
test-mode credentials, `agent/rails/razorpay_rail.py` was built and
verified against the actual API (`tests/agent/test_razorpay_rail_live.py`,
9 tests), and the shared conformance suite
(`agent/rails/conformance/suite.py`) passes against both `SimulatedRail`
and the live rail. One real shape mismatch was found this way, not
guessed: real Razorpay subscription statuses are `created`, `authenticated`,
`active`, `pending`, `halted`, `cancelled`, `completed`, `expired` —
`SimulatedRail`'s invented vocabulary (`pending_afa`, `notified_24h`, ...)
doesn't match, because it was built to mirror DEVDOC_v6 §12.5's own
lifecycle diagram literally rather than a real API response.
`RazorpayRail._mandate_status_from_subscription()` maps between them
conservatively. `Order`, `PaymentLink`, `Invoice`, and `Mandate`-via-
Subscription object shapes are now genuinely conformance-tested against
the real API for the fields this build's reduced schema captures — not
diffed field-by-field against the SDK's full fixture set, which is still
the outstanding action above.

## 3. Webhook envelope and signature scheme (`agent/rails/webhook_signing.py`)

The signature scheme (`hmac.new(secret, body, sha256).hexdigest()`,
verified with `hmac.compare_digest`) matches Razorpay's publicly documented
webhook signature verification approach from memory of their published
webhook docs, not from a freshly fetched page in this pass. **The envelope
shape** `SimulatedRail._emit()` produces — top-level `event`, `event_id`,
`created_at`, `payload` — is this build's own convention, built to be
self-consistent and testable, and is explicitly flagged in
`agent/ingest/webhooks.py`'s docstring as needing verification against a
real captured Razorpay webhook payload before a `RazorpayRail` webhook
route is built on top of the same `verify_and_ingest()` function. Real
Razorpay webhook payloads are known (from general documentation
familiarity, not re-verified here) to nest entities under
`payload.<resource>.entity`, which this convention follows, but the
top-level `event_id` field's exact presence/location in a live payload is
unconfirmed.

## 4. RBI/MSMED/TRAI regulatory text

**Update, 2026-08-30 — partially resourced from the primary text itself.**
Found the actual circular via web search (`RBI/DPSS/2026-27/396` ->
`rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374`, not guessed — a
search for the circular number led to it) and fetched its section
structure directly. Four of the six regulatory rules now cite a real
section number from the primary document: `RBI_EMANDATE_PREDEBIT_24H` ->
Section 6(a)/6(b), `RBI_EMANDATE_AFA_CEILING` -> Section 8(a)/8(b),
`RBI_EMANDATE_POSTDEBIT` -> Section 7, `RBI_EMANDATE_OPTOUT` -> Section
6(c). **This is still one step short of a verbatim read**: the fetch was
processed by an automated summarizer extracting section structure, not
read character-by-character by a person — treat the exact sub-clause
letters as a strong lead, not a citation ready to withstand scrutiny
unchallenged, and re-verify against the circular directly before relying
on them in a real compliance claim.

`RBI_FPC_HOURS` (calling-hours restriction) is sourced more weakly — to
"para 55" of Master Circular DBR.LEG.BC.21/09.07.005/2024-25, per several
secondary legal-summary/blog sources found by search, **not verified
against RBI's own primary document** the way the e-mandate clauses were.
Flagged as a materially weaker sourcing tier directly in
`agent/bounds/rules.yaml`'s comment for that rule, not silently presented
at the same confidence as the others.

`TRAI_DND`'s clause and the MSMED Act's own OM-based trader exclusion
(§14.1, already separately dated and flagged `contested: true` in
`config/statutory_params.yaml`) remain unsourced beyond DEVDOC_v6 itself.
**Compliance still requires external review, which this project does not
have** — better section-level citations are not the same thing as legal
sign-off. See `docs/LIMITATIONS.md` and `docs/REGULATORY_MAP.md`.

## 5. Stripe test mode (§5.5, optional)

Not used in this build pass.
