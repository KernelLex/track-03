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
recommendation string) and I haven't built it — there's no dashboard yet
for it to live in.

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

## What this document is not

This isn't a claim that the system is secure against a motivated,
adaptive attacker with knowledge of exactly how it works — DEVDOC_v6
itself doesn't claim that either. It's a record of which specific, named
exploits I checked, how, and what still needs a live model or a running
eval to actually verify empirically rather than structurally.
