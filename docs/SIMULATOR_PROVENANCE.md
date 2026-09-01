# Simulator Provenance

DEVDOC_v6 §5.5: "Never invent behaviour." This document cites exactly where
I pulled every piece of `SimulatedRail` and `data/failure_taxonomy.yaml`
from, and says plainly where a source is weaker than the spec asked for.

## 1. Failure taxonomy (`data/failure_taxonomy.yaml`)

I fetched this 2026-08-30 from:

- `https://razorpay.com/docs/errors/payments/cards/`
- `https://razorpay.com/docs/errors/payments/upi/`

These are Razorpay's own public documentation pages, which I fetched via
an automated HTML-to-text tool and cross-checked by hand against the
resulting table before writing it into the taxonomy file. **This is
weaker than DEVDOC_v6 §5.5's primary source**, which names
`razorpay.com/docs/build/browser/assets/images/payments_error_reasons.xlsx`
— a binary spreadsheet I couldn't fetch and parse with the tooling
available to me in this build pass. The two error pages above cover the
same content in prose/table form and I used them instead. **Action
before relying on this in a submission:** download the actual
spreadsheet and diff it against `data/failure_taxonomy.yaml`'s `codes`
section.

The `disposition` field (`RETRYABLE` / `TERMINAL`) is **not** copied from
either source page's own "is this retryable" language — it answers a
different, narrower question ("should the system schedule another
`retry_charge` for this"), following the logic in DEVDOC_v6 §11.2's own
four worked examples. Six entries where my own judgment call departs from
the source page's framing are individually justified with a `note:`
field in the YAML itself (e.g. `payment_risk_check_failed` is marked
TERMINAL here despite the source page marking it retryable, because
auto-retrying a fraud-flagged charge is the aggressive-bot behaviour this
whole system exists to avoid).

**Not sourced at all yet:** NACH/eNACH mandate return codes (needs NPCI's
return-code circular, which I haven't pulled into this pass). §12.3's
`REPEAT_NSF` detector works off a *count* of consecutive returns, not
specific reason codes, so it isn't blocked by this gap — but I don't yet
have a human-readable "why did this mandate debit bounce" string
available. See LIMITATIONS.md.

## 2. Object shapes (`agent/rails/types.py`, `agent/rails/simulated.py`)

DEVDOC_v6 §5.5 names the official SDK repos
(`razorpay/razorpay-python`, `-node`, `-ruby`) and their test fixtures as
the source for real response payloads. **I used the installed
`razorpay-python` package's own client interface** (verified directly —
see below) for method names and resource structure, but I did **not**
pull and diff against the SDK's actual test fixture JSON files, since I
didn't exercise network access to that specific repo content in this
pass. The field sets on `Order`, `PaymentLink`, `Invoice`, `Mandate`,
`DebitResult` in `agent/rails/types.py` are a **reasonable, reduced
subset** of the real API's fields (enough for the shape/transition
conformance DEVDOC_v6 §5.4 scopes as in-bounds), not a verified
byte-for-byte mirror.

What I *did* directly verify, by inspecting the installed package
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

**Update, 2026-08-30 — I've now partly done this for real.** With live
test-mode credentials, I built and verified `agent/rails/razorpay_rail.py`
against the actual API (`tests/agent/test_razorpay_rail_live.py`,
9 tests), and the shared conformance suite
(`agent/rails/conformance/suite.py`) passes against both `SimulatedRail`
and the live rail. I found one real shape mismatch this way, not guessed:
real Razorpay subscription statuses are `created`, `authenticated`,
`active`, `pending`, `halted`, `cancelled`, `completed`, `expired` —
`SimulatedRail`'s invented vocabulary (`pending_afa`, `notified_24h`, ...)
doesn't match, because I built it to mirror DEVDOC_v6 §12.5's own
lifecycle diagram literally rather than a real API response.
`RazorpayRail._mandate_status_from_subscription()` maps between them
conservatively. `Order`, `PaymentLink`, `Invoice`, and `Mandate`-via-
Subscription object shapes are now genuinely conformance-tested against
the real API for the fields my reduced schema captures — not diffed
field-by-field against the SDK's full fixture set, which is still the
outstanding action above.

## 3. Webhook envelope and signature scheme (`agent/rails/webhook_signing.py`)

The signature scheme (`hmac.new(secret, body, sha256).hexdigest()`,
verified with `hmac.compare_digest`) matches Razorpay's publicly documented
webhook signature verification approach from my memory of their published
webhook docs, not from a freshly fetched page in this pass. **The
envelope shape** `SimulatedRail._emit()` produces — top-level `event`,
`event_id`, `created_at`, `payload` — is my own convention, built to be
self-consistent and testable, and I've explicitly flagged it in
`agent/ingest/webhooks.py`'s docstring as needing verification against a
real captured Razorpay webhook payload before I build a `RazorpayRail`
webhook route on top of the same `verify_and_ingest()` function. I know
(from general documentation familiarity, not re-verified here) that real
Razorpay webhook payloads nest entities under `payload.<resource>.entity`,
which this convention follows, but the top-level `event_id` field's
exact presence/location in a live payload is unconfirmed.

## 4. RBI/MSMED/TRAI regulatory text

**Update, 2026-08-30 — partially resourced from the primary text itself.**
I found the actual circular via web search (`RBI/DPSS/2026-27/396` ->
`rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13374`, not guessed — a
search for the circular number led me to it) and fetched its section
structure directly. Four of the six regulatory rules now cite a real
section number from the primary document: `RBI_EMANDATE_PREDEBIT_24H` ->
Section 6(a)/6(b), `RBI_EMANDATE_AFA_CEILING` -> Section 8(a)/8(b),
`RBI_EMANDATE_POSTDEBIT` -> Section 7, `RBI_EMANDATE_OPTOUT` -> Section
6(c). **This is still one step short of a verbatim read**: I processed
the fetch through an automated summarizer extracting section structure,
rather than reading it character-by-character myself — treat the exact
sub-clause letters as a strong lead, not a citation ready to withstand
scrutiny unchallenged, and re-verify against the circular directly before
relying on them in a real compliance claim.

`RBI_FPC_HOURS` (calling-hours restriction) is sourced more weakly — to
"para 55" of Master Circular DBR.LEG.BC.21/09.07.005/2024-25, per several
secondary legal-summary/blog sources I found by search, **not verified
against RBI's own primary document** the way I verified the e-mandate
clauses. I flagged this as a materially weaker sourcing tier directly in
`agent/bounds/rules.yaml`'s comment for that rule, rather than silently
presenting it at the same confidence as the others.

`TRAI_DND`'s clause and the MSMED Act's own OM-based trader exclusion
(§14.1, already separately dated and flagged `contested: true` in
`config/statutory_params.yaml`) remain unsourced beyond DEVDOC_v6 itself.
**This still needs external legal review, which I don't have** — better
section-level citations aren't the same thing as legal sign-off. See
`docs/LIMITATIONS.md` and `docs/REGULATORY_MAP.md`.

## 5. Stripe test mode (§5.5, optional)

Not used in this build pass.
