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

**Not independently sourced in this pass at all.** Every `clause_ref` in
`agent/bounds/rules.yaml` for the RBI e-mandate rules is a literal `TODO`
placeholder (see `docs/BOUNDS.md`, generated, which flags every one). The
circular name and date
(`RBI/DPSS/2026-27/396, 21 April 2026`), the ₹15,000 AFA-free ceiling, the
24-hour pre-debit notification requirement, and the MSMED Act sections
(2(b), 2(n), 15, 16, 23, 43B(h)) all come from DEVDOC_v6 itself, not from
this build independently pulling and reading the primary legal texts.
**This is the single most important sourcing gap in the project** — see
`docs/LIMITATIONS.md` and `docs/REGULATORY_MAP.md`'s "honestly not
implemented" section.

## 5. Stripe test mode (§5.5, optional)

Not used in this build pass.
