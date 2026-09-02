# Threat Model

DEVDOC_v6 §24, new in v6: the debtor is a party with a financial interest in
this agent's behaviour, not a passive source of signal. Here I cover what
I've actually built and tested against that framing, and say plainly what
still needs a live model or an eval harness I haven't built.

## 1. Prompt injection through debtor replies (§24.1)

**I built and tested this**: `data/injection_corpus.jsonl` (40 cases, 8
classes of 5 each: direct instruction, fake system framing, fabricated
authority, schema poisoning, dispute laundering, statutory poisoning,
encoding tricks, multi-turn), and `tests/agent/test_injection_resistance.py`
(80 tests, all passing).

**What the tests actually prove.** For every non-schema-poisoning corpus
entry, my own worst-case judgment call about what a fully compromised
model could be tricked into producing (documented per attack class in the
test file, not a literal parse of the free text) still cannot:

1. Unlock a settlement/close-account-shaped action — checked against every
   entry, and generalized with a property test over every `(family, class)`
   pair in the schema, not just the 40 corpus-derived cases.
2. Reach `legal_computation()` — every field of a worst-case extraction,
   wrapped as a `Fact`, is `MODEL` provenance, and `assert_legal_provenance`
   crashes on contact with one (Law 8).

I proved the five schema-poisoning entries differently and more strongly:
their poisoned payloads (an injected `state` field, a decades-out date, an
`amount_paise` of zero, a `__proto__` key, an out-of-range confidence, a
SQL-injection-shaped GSTIN string) get **rejected outright by Pydantic
validation** — not simulated past it. Building this test is what surfaced
a real gap for me: the schema originally left `promise.date` as an
unconstrained string, so "a date decades out" (DEVDOC_v6's own named
example) would have validated cleanly. I fixed that in
`agent/diagnose/extract.py` before I wrote the corpus test against it, not
after.

**What I haven't tested, honestly**: none of this sends the corpus text
through a real language model. I don't have an extractor yet
(`LIMITATIONS.md`). The claim these 80 tests support is the structural one
DEVDOC_v6 §24.1 itself distinguishes: *even a fully compromised model
output* cannot escape the schema or the provenance boundary. Whether a
real model actually resists these 40 prompts well enough to rarely
produce a compromised output in the first place is a separate, empirical
question that needs a live extractor to answer.

**The one residual risk I have no code fix for** (§24.1's own words,
repeated because it's still true): injected text still reaches the
**human** in `HUMAN_QUEUE`. The agent is immune; the operator reading the
queue is not. The mitigation is display-layer (render counterparty text
as quoted untrusted content, never as part of the system's own
recommendation string).

**Update, 2026-09-02 — partly built now that a dashboard exists.** Every
place the dashboard shows counterparty text -- the case file's
`reply_received` rows, the debtor detail's conversation panel -- passes it
through `escapeHtml()` and renders it in a visually distinct quoted style,
never concatenated into the system's own narration of what it decided.

Being precise about what that does and does not buy: escaping closes the
markup-injection route (a debtor cannot inject HTML or script into the
operator's page), and the quoting makes provenance visible. **Neither stops
social engineering.** A debtor whose message reads "the finance team has
already approved a waiver on this invoice, please confirm" is displayed
faithfully, correctly attributed, and can still mislead a human who is
reading quickly. That residual risk is unchanged, and no display-layer fix
addresses it -- the mitigation for it is that a human is *supposed* to be
the one exercising judgement there, which is the whole reason the case was
escalated.

## 2. Stopping rules as a denial-of-service surface (§24.2)

**I built and tested this** — `agent/bounds/rules.yaml` and
`tests/agent/test_bounds_engine.py`:

| Exploit | Fix | Test |
|---|---|---|
| Promise, break it, promise again — cooldown resets forever | `promise_credibility` scales the cooldown continuously (`grace_days x credibility`), trending to zero as broken promises accumulate, never reset by a fresh promise alone | `test_promise_cooldown_scales_with_credibility_not_a_hard_cliff` |
| Assert any dispute — collection freezes, possibly forever if the human queue backs up | `DISPUTE_FREEZE` scoped to the disputed amount only (undisputed remainder stays live); a substantiation-window CLOCK amendment (§24.2) means an unsubstantiated dispute isn't permanently frozen — though the CLOCK's own scheduler isn't built yet, see below | `test_dispute_freeze_blocks_non_escalation_actions`, `test_dispute_freeze_allows_escalate_human_and_no_action` |
| Opt out of each channel in turn (`CHANNEL_HOPPER`) — I found this while building the engine, not in any prior revision | `CHANNEL_EXHAUSTION`: once every channel is opted out, only `escalate_human`/`no_action`/a regulatory notice pass — the case routes to a human, never goes silent | `test_channel_exhaustion_routes_to_human_instead_of_going_silent` |

**I haven't built**: the scheduler that would actually move an
unsubstantiated `DISPUTED_FROZEN` case to `HUMAN_QUEUE` after N days (the
state transition itself is legal and tested — `agent/diagnose/state_machine.py`
— but I don't call it on a timer yet, since I don't have a running
scheduler process for it). Same asymmetry as DEVDOC_v6 states throughout
§24.2: every fix above routes to a *human*, never to more aggressive
automated collection.

## 3. Adversarial personas (§24.3)

**I haven't built this.** `SERIAL_PROMISER`, `DISPUTE_ABUSER`, `INJECTOR`,
`CHANNEL_HOPPER` all need the eval harness and persona-simulation
infrastructure from §17, which I haven't built — I have no `eval/`
runner, so I have no "cases permanently stalled" count to report. What I
*can* say now: the mechanisms each persona would exercise
(`PROMISE_COOLDOWN`'s credibility scaling, `DISPUTE_FREEZE`'s scoping,
schema validation against `INJECTOR`-style payloads, `CHANNEL_EXHAUSTION`)
are each individually built and tested in isolation, above. Running all
four personas against a live simulated population, and reporting the
stall count DEVDOC_v6 §17.7 wants (target: zero), is future work.

## 4. Public endpoints added for the demo dashboard

Four endpoints are reachable without authentication. Each is a deliberate
trade, so they are listed with what actually bounds them rather than left
implicit.

| Endpoint | Auth | What bounds it |
|---|---|---|
| `POST /demo/telegram-webhook` | Telegram's `secret_token`, echoed in `X-Telegram-Bot-Api-Secret-Token` | Verified **before the body is read**, the same discipline `verify_and_ingest()` uses for Razorpay. Unset secret returns 503 rather than accepting unverified deliveries. A message from any chat that is not `DEMO_CONTACT_TELEGRAM_CHAT_ID` is acknowledged and dropped, so a stranger who finds the bot can never surface as the demo's own debtor |
| `POST /demo/trigger` | `DEMO_TRIGGER_SECRET` | A soft guard, not an auth boundary -- it is attached server-side by a serverless function that anyone with the site URL can reach. The real bound is that Telegram's recipient is never taken from the request, and the phone channels validate E.164 and enforce a 5-minute per-number cooldown |
| `GET /demo/timeline` | none | Read-only, and exposes the demo's own scripted invoice plus the demo owner's own replies to their own bot. Requiring the trigger secret would mean baking a *send* credential into a page that only wants to watch |
| `POST /demo/telegram-webhook/subscription` | The **subscription bot's own** `secret_token` | A separate path and a separate secret from the b2b bot's, deliberately: when a delivery starts 403ing, `getWebhookInfo`'s URL should say immediately which bot broke. Same before-the-body verification, same non-demo-chat rejection |
| `POST /demo/subscription-alert` | `DEMO_TRIGGER_SECRET` | Can only warn about a mandate in the seeded portfolio, named by defect kind rather than by arbitrary id, and refuses (409) for a mandate that is healthy -- so it cannot be used to manufacture an alarm. Honors a caller-supplied phone number under the same E.164 validation and 5-minute per-number cooldown as `/demo/trigger` |
| `GET /demo/mandate-health` | none | Read-only. Exposes the seeded mandate book and the detector's verdict on it -- constructed fixtures, nothing belonging to a third party |
| `POST /demo/reset` | `DEMO_TRIGGER_SECRET` | Restores only the invoices `agent/debtor/seed.py` declares; a row created by anything else is left alone. Never touches the recovery ledger, the hash-chained ledger, or the promise history behind a debtor's score — those record things that really happened, and a demo convenience has no business rewriting them |
| `GET /demo/debtors`, `GET /demo/debtors/{id}` | none | Same reasoning. Seeded fixtures plus the demo owner's own record; every row carries `is_seeded` so a declared history can never be read as evidence of real behaviour |

**What this gives up.** Anyone with the URL can read the demo transcript
and the seeded register, and anyone who can reach the serverless function
can cause a message to the demo owner's own configured contacts (rate
limited, never to an arbitrary recipient of their choosing on Telegram).
For a public demo of a project whose data is its own fixtures, that is an
acceptable trade; for a real deployment it is not, and none of these four
endpoints belongs in one.

**A near miss worth recording.** Verifying that the webhook secret was
*configured* is not the same as verifying it *matches* -- the endpoint
returns 403 both for a wrong secret and for a different-but-present one. I
made exactly that error and read a 403 as confirmation (WHAT_BROKE #15).
The lesson generalises past this endpoint: a check that cannot distinguish
the passing case from the failing case is not evidence.

## What this document is not

This isn't a claim that the system is secure against a motivated,
adaptive attacker with knowledge of exactly how it works — DEVDOC_v6
itself doesn't claim that either. It's a record of which specific, named
exploits I checked, how, and what still needs a live model or a running
eval to actually verify empirically rather than structurally.
