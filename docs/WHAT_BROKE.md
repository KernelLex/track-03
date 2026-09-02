# What Broke

DEVDOC_v6 §20: symptom → root cause → fix → verification. Every entry below
is a real defect I found while implementing this spec in this session — not
a hypothetical, and not edited after the fact to look cleaner than it was.
I fixed each one in both the specification (`DEVDOC_v6.md`) and the code
in the same commit, so the two never drifted back apart.

## 1. A bounds bypass: three regulatory rules were unconditional

**Symptom.** Running `trucommit demo` — a plain `send_reminder` action —
`check_bounds()` refused it, citing `RBI_EMANDATE_PREDEBIT_24H`.

**Root cause.** I'd written `RBI_EMANDATE_PREDEBIT_24H`,
`RBI_EMANDATE_POSTDEBIT`, and `MSMED_INTEREST_BASIS` as unconditional
checks in every prior revision of the spec — they didn't scope themselves
to the one action type they actually govern (presenting a mandate debit;
asserting a statutory interest figure). Taken literally, almost no action
has `mandate.last_notification_at` set, so **no action of any kind could
ever pass `check_bounds()`**. This is the inverse of a bypass — a gate so
broad it blocks legitimate traffic — but it's the same class of defect: a
rule whose actual behaviour doesn't match its intent, which I only found
by running the real system instead of reading the rule.

**Fix.** I guarded each with the same `action.presents_mandate_debit =>` /
`action.type == 'send_statutory_notice' =>` implication pattern
`MANDATE_PARAM_CLAMP` and `NO_MANDATE_ON_DISPUTE` already used. I found
`RBI_EMANDATE_OPTOUT` had the identical defect the same way one commit
later: unscoped, it blocked *every* action while a debtor was opted out —
including `revoke_mandate`, the one action §11.6 requires to run
autonomously specifically *because of* that opt-out.

**Verification.** `tests/agent/test_bounds_engine.py::
test_predebit_24h_only_applies_to_actions_that_present_a_mandate_debit` and
`test_optout_only_blocks_actions_that_present_a_mandate_debit` construct
the exact failing scenario and assert the gate now passes a non-debit
action while still refusing an actual under-notified or opted-out debit.

## 2. An illegal-transition-shaped bug: a gate that could never fire

**Symptom.** None visible in a test — I caught this one by inspection
while translating rules.yaml, not by a failing assertion, which is exactly
why it's worth recording.

**Root cause.** `DISPUTE_FREEZE`'s machine rule checked
`action.type in ['human_escalate', 'none']`. The real action names,
everywhere else in the system (`agent/act/actions.py`, §11.5's table), are
`escalate_human` and `no_action`. The comparison could never match anything
a real `Decision` produces — the gate was permanently, silently inert. My
own `CHANNEL_EXHAUSTION` addition copied the same wrong names one commit
later, propagating the bug into new code before I caught it.

**Fix.** I corrected both rules to the real action names, and added a note
in `DEVDOC_v6.md` §13.2 explaining the mismatch so it can't quietly happen
a third time.

**Verification.** `test_dispute_freeze_allows_escalate_human_and_no_action`
and `test_channel_exhaustion_routes_to_human_instead_of_going_silent` both
construct the exact action types by name and assert the gate passes them —
which would have failed against the old, wrong names.

## 3. A gate with documentation but no enforcement

**Symptom.** `initiate_refund` and `revoke_mandate` are marked human-gated
in §11.5's table and in `HUMAN_GATED_ACTIONS` — but nothing in
`check_bounds()` actually blocked an unapproved dispatch of either one.
`STATUTORY_HUMAN_GATE` only fires on `action.carries_legal_number`, which
neither a refund nor a plain revocation sets.

**Root cause.** Marking an action "human-gated" in a metadata table and
*enforcing* that gate are two different things, and I'd only built the
metadata. I found this while writing a test for the ACT executor that
expected `initiate_refund` without approval to be refused — it wasn't.

**Fix.** I added `REFUND_AND_REVOKE_HUMAN_GATE` to the bounds register,
including the one case that must stay autonomous: revoking a mandate
because the debtor opted out this cycle (§11.6 — refusing to reverse on
opt-out is itself a violation).

**Verification.**
`tests/agent/test_act_executor.py::test_initiate_refund_without_human_approval_is_refused_by_bounds`,
`test_revoke_mandate_without_approval_or_optout_is_refused_by_bounds`, and
`test_revoke_mandate_on_debtor_optout_proceeds_autonomously` all exercise
the real `execute_action()` path, not just the bounds engine in isolation.

## 4. Law 4 unenforced: ACT never wrote to the ledger

**Symptom.** None visible until it blocked the next thing I was building
(the Auditor's bounds-integrity job, which needs a recorded snapshot of
what `check_bounds()` actually saw for an executed action).

**Root cause.** `agent/act/executor.py` — the one stage that moves money —
never appended a single `LedgerEntry`. Every dispatch, refusal, and deduped
retry happened, got recorded in `OutboundActionStore` for idempotency
purposes, and then vanished with no trace in the append-only audit trail
Law 4 requires ("agents coordinate only through the ledger"). This is
arguably the most serious finding in this list: not a rule that was too
strict or too loose, but a safety mechanism I'd simply never wired up, in
the one place it mattered most.

**Fix.** I made `ledger` a required parameter of `execute_action()` — not
optional, so there's no "skip it under time pressure" path — and appended
one `LedgerEntry` per call, whether accepted, refused, or deduped. Doing
this properly needed `BoundsContext` to become serializable
(`to_dict()`/`from_dict()`), so every entry now carries a JSON-safe
snapshot of exactly what was fed into `check_bounds()`.

**Verification.**
`tests/agent/test_act_executor.py::test_a_refusal_still_writes_a_ledger_entry`,
`test_a_successful_dispatch_writes_a_ledger_entry_with_a_bounds_context_snapshot`,
and `test_a_deduped_retry_still_writes_its_own_ledger_entry_marked_duplicate`
all assert directly against `ledger.all_entries()` after calling the real
executor. `tests/agent/test_auditor.py::
test_bounds_integrity_catches_a_gate_that_silently_stopped_matching` then
uses that wiring for its actual purpose: forging a recorded verdict on a
real ledger entry and confirming the Auditor's bounds-integrity job catches
the mismatch.

## 5. A generated doc that broke its own table on every run

**Symptom.** `docs/BOUNDS.md`, freshly generated by `tools/gen_docs.py`,
had several Markdown table rows split across multiple lines, breaking the
table's rendering.

**Root cause.** Several `machine:` fields in `rules.yaml` use YAML's folded
block scalar (`>-`), which only folds newlines between lines at the *same*
indentation level. A few rules had continuation lines indented further
(for visual alignment with an opening parenthesis), which YAML's folding
algorithm treats as "more indented content" and leaves un-folded — so the
literal newline survived into the generated Markdown cell.

**Fix.** Rather than fighting YAML indentation rules in the source (fragile
— the next multi-line rule could reintroduce the same bug), I added a
defensive `_cell()` helper in `tools/gen_docs.py` that collapses any
embedded whitespace before writing a value into a table cell.

**Verification.** `tests/agent/test_gen_docs.py::
test_bounds_md_contains_no_bare_newline_inside_a_table_row` and
`test_cell_collapses_embedded_newlines_and_repeated_whitespace` — the
latter is a direct unit test of the exact bug, not just the visible symptom.

## 6. A closed-world test fixture broke the moment the world grew a channel

**Symptom.** Adding `"telegram"` to `ALL_CHANNELS` (agent/bounds/context.py)
to support a real Telegram messaging channel made
`test_channel_exhaustion_routes_to_human_instead_of_going_silent` fail:
`CHANNEL_EXHAUSTION` returned `PASS` where the test expected `REFUSE`.

**Root cause.** I'd built the test's "debtor has opted out of everything"
fixture by hand-listing the four channels that existed when I wrote it
(`frozenset({"sms", "email", "whatsapp", "ivr"})`) instead of referencing
`ALL_CHANNELS` itself. The rule's own logic
(`len(debtor.opted_out_channels) < len(ALL_CHANNELS)`) was never wrong —
once a fifth channel exists, a debtor who opted out of only the original
four genuinely *hasn't* exhausted their contact options anymore (Telegram
is still open to them), so `PASS` was the correct answer to the question
the stale fixture was accidentally now asking. The bug was the fixture
silently assuming a closed world, not the rule.

**Fix.** I changed the three "every channel" fixtures in
`tests/agent/test_bounds_engine.py` to opt out of `ALL_CHANNELS` directly
rather than a hardcoded copy of its current members, so the test keeps
asking the question it means to ask ("every channel exhausted") regardless
of how many channels exist later.

**Verification.**
`tests/agent/test_bounds_engine.py::test_channel_exhaustion_routes_to_human_instead_of_going_silent`
passes against the five-channel set; I re-ran the full suite (561 tests at
time of writing) afterward specifically to catch any other place a
channel list had been copied instead of imported — I found none.

## 8. No CI — and an outside reviewer, not me, found the suite red

**Symptom.** An external audit cloned the repo, ran `uv run pytest`, and
reported **3 failed, 842 passed, 11 skipped** on a clean clone. They
bisected it to a specific commit and noted two further commits had shipped
on top of a red suite.

**Root cause, in two parts.** The first is the one that matters: **there
was no CI at all — no `.github/` in the repo.** A project whose entire
pitch is bounded execution and gates that refuse had no gate on itself,
which is exactly why three tests could sit red across three commits
without anyone noticing. That isn't an oversight to fix quietly; it's a
contradiction of the thesis.

The second part I could not reproduce, and I want to be precise rather
than agreeable about it. I ran the full suite on a clean clone at four
commits (`2682128`, `45d72fe`, `5dd89e1`, `5caf3f1`) with no project env
vars exported: green every time, 0 failed. Their run collected 856 tests
(matching my HEAD) with 842 passing; my HEAD passes 845 — and 842 + 3 =
845, so the same three tests pass here and fail there. That points at
order- or platform-dependence (they are likely on Linux; I am on Windows),
not a bad commit. Their diagnosis named a fixture leak on
`agent.api.demo`'s process-global cooldown dicts.

**Fix.** Three things, none of which wait on reproducing it:

1. `.github/workflows/ci.yml` — the suite now runs on **ubuntu-latest**
   (their platform, the difference I could not rule out) on every push and
   PR, with no credentials in the environment.
2. A second CI job runs the suite under **randomised order**
   (`pytest-randomly`), hunting this class of bug deliberately instead of
   waiting to be told about it again.
3. `tests/conftest.py` — the reset of `agent.api.demo`'s global state moved
   out of `tests/agent/test_api_demo.py` and up to suite scope. Their
   diagnosis was structurally right even where I couldn't reproduce the
   symptom: that state was protected only by a fixture living in the one
   module that happened to touch it, which holds exactly until a second
   module does. The module-level fixture now owns only its own fakes, so
   there aren't two resets to drift apart.

**Verification.** Green on a clean clone, and green under four different
random seeds (1, 42, 1337, 90210) locally. CI is the real verification and
it now exists — if their three failures are platform-specific, the Ubuntu
job will show it rather than leaving it as a disagreement between two
machines.

## 9. Three different wrong test counts in the docs

**Symptom.** The same audit found the README claiming 789 tests,
`docs/LIMITATIONS.md` claiming 771, and the suite actually collecting 856.

**Root cause.** Both numbers were hand-written and neither had any reason
to stay true. The real damage isn't the stale figure — it's that in a
project whose whole register is "every number here was checked against a
real run", a reader who catches one unchecked number reasonably starts
discounting the others.

**Fix.** Corrected both, but hand-fixing alone would have left the same
hole open, so the numbers are now *gated*:
`tests/test_documented_test_counts.py` collects the suite in a subprocess
and asserts both documents' stated counts sum to it. A stale count is now
a failing test rather than something an auditor finds.

**Verification.** The guard was written first and observed failing against
the stale numbers (`README claims 789 tests + 11 live = 800; a real
collection finds 858`), then passing once both docs were corrected.

## 10. The demo told every judge the best artifact in the repo didn't exist

**Symptom.** `uv run trucommit demo` — the first command in the README —
closed by printing that the four-arm eval "needs personas and a
pre-registration commit that don't exist yet." Both exist:
`eval/PREREGISTRATION.md` is locked at its own commit and
`docs/RESULTS.md` is generated from it.

**Root cause.** The line was true when written and quietly stopped being
true when the pre-registration was built. Nothing pointed the two at each
other, so the stalest possible claim sat in the highest-traffic place in
the project — the closing lines of the first command a reviewer runs.

**Fix.** `agent/cli.py`'s closing message and module docstring now name
`eval/PREREGISTRATION.md` and `docs/RESULTS.md` directly, including the
exact command that regenerates the results.

**Verification.** `uv run trucommit demo` re-run; its closing block now
points at both files.

## 11. The rate limiter refused the first request after every restart

**Symptom.** CI, on its very first red run, failed exactly three tests on
ubuntu-latest — `test_ivr_can_call_a_caller_supplied_number`,
`test_whatsapp_can_message_a_caller_supplied_number`, and
`test_the_same_supplied_number_has_its_own_cooldown` — each with
`assert 429 == 200` on its *first* request. The same three an external
audit had reported. They had never failed on my machine, at any commit, in
any order, on a clean clone.

**Root cause.** Not a fixture leak, and not a test problem at all — a real
defect in `agent/api/demo.py`:

```python
last = _last_triggered_at_by_number.get(to, 0.0)
if now - last < PER_NUMBER_COOLDOWN_SECONDS:   # 300
```

`time.monotonic()` counts from an arbitrary origin, and on Linux that
origin is machine boot. The `0.0` default therefore doesn't mean "never
contacted", it means "contacted at boot" — so on a freshly-started machine
`now` is small (a CI runner is seconds old), `now - 0.0` is smaller than
the window, and **the first request to any number is refused**. My dev box
had been up for days, so `now` was enormous and the branch could never be
reached. Same bug in the per-channel limiter with its 20-second window.

This is a production defect, not a test artifact: Render's free tier
cold-starts constantly, and every one of those restarts would have refused
real traffic for up to five minutes.

**Fix.** `None` for "never contacted", checked explicitly, in both
limiters — an absent key no longer pretends to be a timestamp.

**Verification.** Two regression tests pin `time.monotonic()` low to
reproduce a seconds-old machine deterministically on any OS, which is the
only way this is testable on the machine where it could never occur
naturally. Both were observed failing against the old logic and passing
against the fix.

**What this cost, and what it bought.** I told the auditor I could not
reproduce their three failures and reported that honestly — four commits,
a clean clone, four random seeds, no project env vars. All of that was
true and none of it mattered, because the variable I could not vary was my
own machine's uptime. The audit's central point was never really about
those three tests: it was that a project selling gates that refuse had no
gate on itself. CI found this on its first run, which is the argument for
CI in one line.

## 12. The stop rules refused the escalation they exist to trigger

**Symptom.** Wiring the live conversation to the real DECIDE -> BOUNDS path
surfaced it immediately: chase a debtor three times, and the gate refused
the fourth chase (correct) *and* refused escalating the case to a human
(not correct). Past six attempts, same again. The case simply went quiet.

**Root cause.** `TOUCH_BUDGET` exempted regulatory notices but not
`escalate_human` / `no_action`:

```yaml
machine: "action.is_regulatory_notice == True or debtor.touches_7d < 3"
```

`ATTEMPT_CEILING` (`invoice.recovery_attempts < 6`) exempted nothing at
all. So both counted "hand this to a person" as though it were another
contact with the debtor — and once they fired, every available action was
refused, leaving silence.

Silence is the one outcome this project argues against most explicitly.
`CHANNEL_EXHAUSTION` exists precisely to route a case to a human "instead
of going silent", and its own inline comment asserted that a notice "stays
exempt here the same way TOUCH_BUDGET exempts it" — a claim about
`TOUCH_BUDGET` that `TOUCH_BUDGET` did not implement. The comment
described the intended design; the rule didn't.

Neither implementation was wrong relative to the other, which is why the
5,000-case differential test never caught it: `human_twin.py` faithfully
reproduced the same gap. A differential test proves two implementations
agree, not that either is right — `docs/LIMITATIONS.md` already says this
in as many words, and this is the first time that caveat has actually
cost something.

**Fix.** Both rules now exempt `escalate_human` and `no_action`, in
`rules.yaml` and in the independently-written `human_twin.py`, matching
`CHANNEL_EXHAUSTION`'s existing exemption. Routing a case to an internal
queue is not a contact with the debtor and was never what a *contact*
budget was counting; stopping is not a *chase* and was never what a chase
ceiling was counting.

**Verification.** The 5,000-case differential test still passes, so the two
implementations still agree after both were changed separately. The
progression is now: three chases, then escalation, and escalation stays
available indefinitely rather than being refused into silence. One
existing test (`test_a_bounds_refusal_still_refuses_under_dry_run`) had
been relying on Family D eventually being refused, and was rewritten to
use a genuinely refusable action — its intent was right, its premise was
the bug.

## 13. The e-mandate would have charged away the discount it had just offered

**Symptom.** Caught by a test before it ever ran against the rail, which is
the only reason it isn't a worse entry on this list.

`create_plan_mandates()` groups plan legs by amount, because one Razorpay
subscription carries a *fixed* per-cycle amount and can therefore only
cover legs that cost the same. The first version grouped on
`leg.amount_paise` — the face value of the instalment.

**Root cause.** Face value is not what gets debited.
`payment_plan.build_plan()` prices each leg independently, and a leg
falling inside the 10-day early-payment window is discounted while a later
one is not. So a plan of two Rs 21,250 legs — visibly, arithmetically
"equal" — is actually Rs 20,825 and Rs 21,250 once priced. Grouping on the
face value would have merged them into one subscription at Rs 21,250 and
debited the discounted leg at the undiscounted price.

The system would have offered a 2% discount in the message and then taken
it back in the mandate, silently. Worse than not offering one: the debtor
agrees to a number and is charged a different, higher one, with a real
authorization behind it.

**Fix.** Group on `leg.payable_paise`, the priced amount. Unequal legs get
one mandate each rather than being merged.

The general shape is worth naming because it recurs: a *displayed* amount
and a *charged* amount that are equal in the common case and diverge in a
specific one. The common case was two undiscounted legs, which is what
made the wrong key look right.

**Verification.**
`test_a_discount_on_only_one_leg_correctly_splits_them` constructs exactly
the divergence — two identical face amounts, one inside the discount
window and one outside — and asserts the mandates differ. It fails against
the face-value grouping. `test_no_leg_is_ever_authorized_for_more_than_it_is_worth`
holds the broader line: no leg is ever authorized above its own face
value, so the "just use the larger amount" shortcut stays closed too.

## 14. The gate refused an action, and the system reported it as allowed

**Symptom.** Found in a live run, not by a test -- which is the part worth
noting, because everything downstream of it worked correctly.

A real debtor replied "I can pay 21000 on the 5th and rest later". The
timeline recorded:

```
decided  action=send_reminder  allowed=True  refusals=['PROMISE_COOLDOWN']
```

`check_bounds()` refused `send_reminder`, and the system named
`send_reminder` as the allowed next step anyway.

**Root cause, part one.** `_decide_next_step()` tried the diagnosis's
natural action, then escalation, and if both were refused fell through to
the refused action:

```python
chosen = action_type.value          # the refused action, still
if not result.passed:
    if _gate("escalate_human").passed:
        chosen, escalated = "escalate_human", True
# ... no else
```

`allowed` was then computed as `chosen == action_type.value` -- True both
when the gate passed *and* when every fallback failed and the refused
action was silently reused. Two different outcomes, one label.

**Root cause, part two.** Why escalation was also refused:
`PROMISE_COOLDOWN` had no exemption for the actions that mean *stop*.

```yaml
machine: "debtor.state != 'PROMISED' or (promise_date is not None and ...)"
```

This is WHAT_BROKE #12 again, in a rule that sweep didn't cover. #12 fixed
`TOUCH_BUDGET` and `ATTEMPT_CEILING` and stopped there. Refusing
`no_action` under a cooldown is incoherent on its face -- it says "you may
not do nothing", when doing nothing is exactly what the rule is asking
for.

**Fix, in three parts.**

`PROMISE_COOLDOWN` now exempts `escalate_human` and `no_action`, in
`rules.yaml` and in the independently-written `human_twin.py`.

`_decide_next_step()` falls back to `no_action` when everything is
refused, never to the refused action, and `allowed` now reports
`result.passed` -- what the gate actually said.

And a distinction that did not exist before: **not every refusal means "a
person should look at this."** Escalating a debtor who just named a
payment date over-reacts to what a cooldown asked for, and buries the
queue a real escalation needs to stay useful. `REFUSALS_THAT_MEAN_WAIT`
(`PROMISE_COOLDOWN`, `RBI_FPC_HOURS`, `EV_FLOOR`) route to `no_action`;
everything else -- a dispute, an exhausted channel, a statutory gate --
still escalates.

**Verification.** The 5,000-case differential test still passes, so the two
bounds implementations agree after both were changed separately.
`test_promise_cooldown_exempts_the_actions_that_mean_stop` and
`test_promise_cooldown_still_refuses_a_chase_inside_the_window` pin the
rule from both sides. `test_allowed_reflects_the_gate_not_the_fallback`
fails against the old `chosen == proposed` computation, and
`test_a_dispute_still_escalates_to_a_person` holds the line against the
wait/escalate split becoming a universal shrug.

**What this cost.** Nothing, this time -- the reply that went out was
correct and useful, and the send itself was governed by a separate gate
(`_bounds_gate_followup`) that passed honestly. What was wrong was the
*reported* decision. For a project whose central claim is that the gate is
a real chokepoint, a decision record that says `allowed` about a refusal
is the kind of small dishonesty that makes the whole claim unverifiable.

## 15. A secret that was configured, and a secret that matched, are different things

**Symptom.** The Telegram webhook was registered, the server was deployed,
and a real reply got no answer. Nothing in the server logs, because nothing
reached the server.

**Root cause.** `getWebhookInfo` had it:

```
last_error_message: Wrong response from the webhook: 403 Forbidden
```

The trailing `-` of `TELEGRAM_WEBHOOK_SECRET` had been dropped while
pasting the value into Render's environment UI. Telegram was sending
`...ydu2EM5h-`; the server expected `...ydu2EM5h`. Every delivery was
correctly rejected.

**The reasoning error is the part worth recording.** Before triggering the
run I *had* checked the endpoint, with a deliberately wrong token, and got
a `403` instead of a `503`. I read that as "the secret is configured" and
moved on. It does prove that -- and it proves nothing about whether the
configured value is the *right* one. The endpoint returns `403` for a wrong
secret and `403` for a different-but-present secret; those are the same
response. I had built a test that could not distinguish the passing case
from the failing one and then treated it as evidence.

**Fix.** The registration now uses the value the server actually holds,
established by probing candidate variants against the live endpoint with a
`chat_id` that is not the demo contact -- a correct secret returns 200
`not_the_demo_contact` (auth passed, nothing sent to anyone), a wrong one
returns 403. That distinguishes the two cases without messaging a real
person.

**Prevention.** `docs/SETUP.md` now documents `getWebhookInfo` as the
verification step, names this exact `last_error_message`, and says plainly
that a 403 proves a secret is present rather than correct. The generated
secret would be better without trailing punctuation, but the durable fix is
checking the thing you actually care about instead of a proxy for it.

## 16. Two debtors could share one channel, and the wrong one got the credit

**Symptom.** A test asserted that a rail-confirmed capture keeps the
debtor's open promise. It came back `pending`.

**Root cause.** The test registered a debtor on the demo's Telegram chat
id -- and the app already seeds `debtor_live` against that same
`DEMO_CONTACT_TELEGRAM_CHAT_ID` at startup. `channel_ref` had no uniqueness
constraint, so two debtors held the same address. `by_channel_ref()` does
`WHERE channel_ref = ?` and takes the first row, so the promise was
recorded against one debtor and settled against the other.

In a test this is a confusing failure. In production it is worse: a real
payment silently improves the wrong debtor's score, and the debtor who
actually paid keeps a broken promise on their record and the stricter terms
that come with it.

**Fix.** `CREATE UNIQUE INDEX ... ON debtors(channel_ref)`, with `upsert()`
raising `ChannelRefTaken` rather than swallowing the integrity error --
a conversation attributed to the wrong person is exactly the thing that
must not fail quietly.

## 17. Whether a payment counted depended on whether we could message someone

**Symptom.** Found by an end-to-end run against a locally started server,
not by the unit tests -- which passed, because they all had a channel
configured. With `TELEGRAM_BOT_TOKEN` unset: `promise_settled: false`, the
promise still `pending`, and an empty timeline.

**Root cause.** `_notify_payment_outcome()` returned early when no channel
was configured:

```python
if not chat_id or not token:
    return None
```

and the call that settles the promise lived *after* that guard. So a
deployment with no Telegram token silently stopped scoring debtors
altogether. Nothing errored; the score was simply never updated, which is
invisible until someone asks why a debtor's terms look wrong.

The design error is a layering one. Whether a payment counts toward a
debtor's record is a fact about the *payment*. Whether anyone can be told
about it is a fact about the *channel*. Putting the first inside the second
made a bookkeeping guarantee depend on a delivery capability.

**Fix.** Settling and recording happen first and unconditionally; only the
send is conditional. The response reports `notified: false` with a reason
rather than staying silent about it -- an operator needs to know the
message did not go out, and claiming otherwise would be worse than the bug.

**Verification.** `TestScoringDoesNotDependOnMessaging` runs the whole
webhook path with no channel configured at all and asserts the capture
still settles, the timeline still records it, and the response says
honestly that nobody was told. All three fail against the old ordering.

## 18. The one generated document the staleness gate did not cover

**Symptom.** Found by an external audit, not by me, and not by the two
gates I had just built for this exact bug class. A reviewer ran
`uv run python eval/report.py` -- the command the README invites them to
run -- and got a diff against the committed `docs/RESULTS.md`.

| Metric | Committed | Regenerates as |
|---|---|---|
| Arm C human escalation | 9.6% | 8.0% |
| Arm C contact-exhausted | 0.4% | 0.6% |
| Arm C mean touches | 1.78 | 1.80 |
| Arm C autonomy rate | 90.4% | 92.0% |
| Lift sweep | — | every row shifted |

**Root cause.** #12, #14 and #17 changed escalation and attribution
behaviour. The eval moved under the doc and the doc was never regenerated.

Recovered fraction (98.4%) and bounds violations (0) held, which is
precisely why nobody noticed: the headline claims were unaffected, so
nothing looked wrong. The numbers had also moved in my favour -- the
committed doc understated the system -- which is the kind of drift that
never prompts a second look.

**The part that stings.** I had already fixed this bug class twice.
`tools/gen_docs.py --check` gates `BOUNDS.md`, `REGULATORY_MAP.md` and
`LEDGER.md`, and CI runs it. `RESULTS.md` is generated by
`eval/report.py`, a different script, so it sat outside that gate
entirely -- and it is the single most important generated document in the
repo, the one the README's headline claim points a reader at.

Fixing an instance twice and still missing the instance that mattered most
is a specific failure: I gated the documents the tool I was already
touching happened to produce, rather than asking which documents are
generated. The right question was never "what does `gen_docs.py` make", it
was "what in this repo claims to be derived from something else".

**Fix.** `eval/report.py --check` regenerates in memory and compares
without writing, mirroring `gen_docs.py --check`. Wired into both the
suite and CI, so both generated-doc paths are now gated by the same
discipline.

**Verification.** Two consecutive runs of the generator are byte-identical,
so gating it is meaningful rather than flaky -- checked before wiring it
up, because a nondeterministic generator behind a gate produces a test that
fails at random and gets disabled.

**Also fixed, from the same audit.** The README said "984 tests" while a
run reports 996 collected. Both were true -- 11 need live Razorpay
credentials -- but a reader seeing two numbers stops to work out which is
wrong. It now reads "996 collected: 985 run without credentials, 11
skipped", and the gate asserts the collected figure directly rather than
only that the parts sum, which the old form allowed a wrong split to hide
inside.

## 19. The same message, extracted two different ways

**Symptom.** A live regression run, twice, with the identical message: "I
can pay 21000 today and rest on 5 th".

```
run 1:  promise={'date': '2026-09-01', 'amount_paise': 2100000}
run 2:  promise={'date': '2026-09-05', 'amount_paise': None}
```

On run 2 the missing amount hit the "a date with no amount means the full
balance" rule, so the system offered **Rs 41,650 on the 5th** -- a single
payment of the whole invoice -- to a debtor who had just proposed splitting
it. The composed reply then contradicted itself, because the model could
see the raw message in the user turn while the computed plan in the context
block said something else:

> "Noting your proposal of Rs 21,000 today and the remaining Rs 21,500 on
> the 5th -- to confirm, we have a plan of Rs 41,650 on 2026-09-05"

**Root cause.** `PromiseFields` held exactly one `(amount_paise, date)`
pair. A two-payment offer has nowhere to go in that schema, so the
extractor had to collapse it into one slot -- and with no rule saying which
half to keep, it kept a different half each time. The instability was not
the model being unreliable; it was the schema forcing a lossy choice and
leaving which loss unspecified.

The "assume the full balance" rule made it worse rather than causing it.
That rule is right for "I'll pay on the 5th", where no amount was ever
stated. Applied to a message where the debtor *did* state an amount and the
schema dropped it, it turns a lost field into a confident misreading.

**Fix.** `PromiseFields.schedule: list[PromiseLeg]` -- every payment they
named, in order. A leg with `amount_paise: None` means "the rest", which is
a real thing people say; the remainder is arithmetic this side does,
because the model is explicitly told not to compute it. Inventing an amount
the debtor did not say is what `Promise` refuses everywhere else.

`_legs_from_schedule()` refuses rather than repairs: named amounts over the
invoice total, more than one unnamed "rest", a leg with no date, or fully
specified legs that don't sum. Each of those is a real disagreement about
what was offered, and guessing would put words in their mouth. The new
`stated` plan shape marks every leg `proposed_by: "debtor"`, because
nothing in it is ours.

Additive by construction: `amount_paise` and `date` still carry the
single-payment case, and a schedule with fewer than two legs takes the
original path untouched.

**Verification.** `TestTheDebtorsOwnScheduleIsHonoured` covers both dates
being used, "the rest" resolving to the remainder, no leg being marked as
our proposal, and each refusal case. `test_a_single_leg_schedule_takes_the_ordinary_path`
holds the additive property.

**Still open:** the extractor's *stability* is improved by giving it
somewhere to put the second leg, but not proven. Two runs of one message is
not a measurement, and nothing here establishes how often the schedule is
populated correctly on real replies.

## 20. A careful refusal falling through to a confident wrong assertion

**Symptom.** Found by probing the real extractor with hard messages after
#19 was fixed, rather than by waiting for a live run to hit one.

| Debtor said | System offered |
|---|---|
| "half now and half at month end" | the entire Rs 42,500 **today** |
| "make it the 7th instead of the 5th" | the entire Rs 42,500 on the **7th** |
| "50000 on the 5th and 20000 later" | the entire Rs 42,500 on the **5th** |

**Root cause.** `_legs_from_schedule()` refused all three correctly -- two
legs with no amounts, a leg with no date, named amounts exceeding the
invoice. The refusals were right. What happened next was not:

```python
stated = int(promise.amount_paise) if promise.amount_paise else total
```

A missing amount read as the full balance. So every careful refusal fell
through into an assertion the debtor never made -- and worse, one that
would have had a real e-mandate issued against it.

This is #19 not fully fixed. I corrected the schema so a two-leg offer had
somewhere to go, and left the fallback that fires when the offer still
cannot be reconciled. The schema was the cause of the *instability*; this
default was the cause of the *wrongness*, and fixing one made the other
easier to see, not to go away.

**Fix, in two parts.**

A schedule with two or more legs that cannot be reconciled now builds no
plan at all. "Half now and half at month end" names no amounts, and there
is no arithmetic that recovers what they meant -- so the composer
acknowledges the offer and asks for the numbers, which is the honest reply.

A bare date with a plan already on the table is treated as a *change* to
that plan rather than a new promise to pay everything. Deliberately no plan
rather than a guessed re-date: which instalment they meant is genuinely
ambiguous, and the composer already receives the outstanding proposal and
can put the question back.

**Verification.** Re-probed against the real extractor, not just the tests:
"half now and half at month end" and "50000 on the 5th and 20000 later" now
produce no plan; "make it the 7th instead of the 5th" produces no plan when
a plan is outstanding and still produces an ordinary full-balance promise
when nothing is on the table, which is the case the guard must not swallow.

**What still fails, recorded rather than fixed.** "Either 21000 today or
the whole thing on the 10th" is an alternative, not a schedule. The
extractor returns the first branch (confidence 0.55) and the system builds
a split with a system-proposed second date. It is labelled as a proposal,
so nothing is misattributed to the debtor, but the reply does not address
the choice they actually offered. Representing alternatives needs a schema
change beyond `schedule`, and it is not made here.

**Also open: the server resolves "today" in UTC.** `date.today()` on Render
is UTC, so for an Indian debtor texting between 00:00 and 05:30 IST,
"today" and "tomorrow" resolve one day early. This was visible in a live
run -- "21000 today" extracted as 2026-09-01 while the debtor's date was
2026-09-02.

## 21. The gate was told a channel the send was not going over

**Symptom.** None. Found by reading call sites before pushing
`WHATSAPP_SESSION_WINDOW`, specifically to check whether the new rule could
be bypassed. It could.

**Root cause.** `agent/api/app.py`'s lifespan prefers WhatsApp the moment
both credentials exist:

```python
if whatsapp_phone_id and whatsapp_token:
    app.state.orchestrator_channel = WhatsAppChannel(...)
```

and the call site, 350 lines away, hardcoded the channel it told the gate:

```python
result = run_pipeline(..., channel_tag="telegram", channel=state.orchestrator_channel, ...)
```

So `channel` and `channel_tag` could disagree, and the bounds gate reasoned
about a channel the message was not going over.

Two consequences, one old and one new. `TRAI_DND` checked *telegram's*
opt-out list while sending on WhatsApp — a debtor who had opted out of
WhatsApp would still have been messaged there. And
`WHATSAPP_SESSION_WINDOW`, which only fires on `channel == 'whatsapp'`,
could never fire on the one automated path capable of sending there, so the
rule would have been inert in production while passing every test.

**Latent, not active**: `WHATSAPP_ACCESS_TOKEN` is unset, so the lifespan
falls through to Telegram and the tag was accidentally correct. It would
have become wrong the moment the WhatsApp template was approved — which is
to say, at the least convenient possible time.

**Fix.** The lifespan records `orchestrator_channel_tag` beside the channel
it selected, and the call site passes that. One place decides, and the two
cannot drift.

**The general shape**, worth naming because it is not specific to channels:
a fact was derived in one place and re-asserted as a literal in another. It
stayed correct only because the default happened to match. Every gate this
project has is only as truthful as the context handed to it, and a
hardcoded context field is a quiet way to lie to your own gate.

**Verification.**
`TestTheOrchestratorDeclaresItsRealChannel` starts the app with each
credential set and asserts the tag matches the channel object actually
constructed. The WhatsApp case fails against the old hardcoding.

## 22. Eleven consecutive red CI runs, reported as green

**Symptom.** CI failed on every push from #16 to #26 — eleven runs — while
I reported the suite as passing each time. Both statements were true and
that is the problem: the suite passes locally, and I was reading the local
result and never opening the CI page. Nobody caught it because the person
who introduced it was also the person confirming it.

**Root cause.** `docs/RESULTS.md` cites the commit that last touched
`eval/PREREGISTRATION.md`, obtained with:

```python
subprocess.run(["git", "log", "-1", "--format=%H", "--", "eval/PREREGISTRATION.md"], ...)
```

`actions/checkout@v4` defaults to `fetch-depth: 1`. In a one-commit clone
**every file looks newly added at HEAD**, so that command does not fail and
does not return empty — it returns the HEAD sha. A plausible-looking wrong
answer, and a different one on every push.

So the gate I added in #16 to catch RESULTS.md drift (WHAT_BROKE #18)
compared a doc citing `1f3b503…` against a regeneration citing whatever
had just been pushed, and failed forever. The gate was working perfectly;
it was reporting a difference that CI itself was creating.

**Two fixes, because one was not enough.**

`fetch-depth: 0` on both checkout steps, so git can actually answer.

And the generators now refuse to answer when they cannot know: both check
`git rev-parse --is-shallow-repository` and exit with an explanation rather
than emitting a hash derived from a history that isn't there. Depending on
CI configuration alone would leave the same trap for anyone cloning
shallowly, and the failure mode is a *silently wrong citation* — the worst
kind, because the document still looks authoritative.

**What this actually cost**, stated plainly: nothing in the product, and a
great deal in credibility. Eleven commits went out with a red CI badge on a
repository whose central argument is that its claims are checkable. A judge
who opened the Actions tab before reading anything else would have seen
that first.

**The process failure is the real entry here.** "The suite passes" and "CI
passes" are different claims, and I substituted the cheap one for the one
that mattered while writing "green CI" in commit messages. The mechanical
fix is above; the discipline is to read the CI result rather than infer it
from a local run, which is exactly the "verify, don't assume" rule this
list already contains four other instances of.

**Verification.** Reproduced by cloning this repository with `--depth 1`
and observing `git log -1 -- eval/PREREGISTRATION.md` return the HEAD sha
rather than `1f3b503…`; confirmed a full clone returns the correct hash;
confirmed the hardened generator exits with its message under `--depth 1`.

**A second failure was hiding behind the first.** With the shallow-clone
bug fixed, CI failed again — on the suite itself this time, because the
earlier step had been failing first and masking it.

`agent/clock.py` made every relative date resolve against IST rather than
the server's clock (WHAT_BROKE #20). Three test files still measured
against `date.today()`. On a machine set to IST those are the same date; on
a UTC runner they differ for five and a half hours a day. So the tests
passed locally, always, and failed in CI for part of every day —
the same shape as #22 itself: a check that could not distinguish the
passing case from the failing one, because the environment made the
distinction invisible.

Fixed by having the tests use `business_today()`, the same clock the code
does. Verified by running the whole suite at four timezone offsets
(`TRUECOMMIT_TIMEZONE_OFFSET_MINUTES` of local, −1400, +780 and 0) and
confirming it is now genuinely timezone-independent rather than
accidentally aligned.

## 23. The dashboard's "use my own number" field did nothing

**Symptom.** None visible, which is the problem. The live console has a
recipient field so someone trying the demo can have the call reach their own
phone. It looked like it worked -- the request succeeded, a call was placed,
a message went out. It just always went to the server's own configured
contact.

**Root cause.** The browser never talks to the backend directly for a
secret-gated action; it posts to a serverless function that attaches
`DEMO_TRIGGER_SECRET` server-side. That function forwarded three fields:

```js
body: JSON.stringify({
  secret: DEMO_TRIGGER_SECRET,
  channel: payload.channel,
  scenario: payload.scenario,
})
```

`to` is not in that list. The frontend sent it, the proxy dropped it, and
the backend fell through to `DEMO_CONTACT_PHONE_NUMBER` exactly as it is
designed to when no recipient is supplied. Every layer behaved correctly and
the feature did not exist.

Found while adding the same field to the subscription alert -- reading the
proxy to copy its shape, rather than from anything failing.

**Why it went unnoticed for so long.** Locally the frontend can be pointed
straight at the backend, where `to` travels fine. The proxy only sits in the
path on a *deployed* site. So the field worked in every test and in every
local run, and silently did nothing in the one environment a judge would use.

**Fix.** Both proxies (Vercel and Netlify) now forward `to`. The backend
already validated it as E.164 and applied a per-number cooldown; it was
never given the chance.

## 24. Two bots, one chat id -- and then the over-correction

**Symptom.** Caught before shipping, by checking the second bot's chat id
rather than assuming it would differ.

A second Telegram bot was added so the subscription demo would be a visually
separate conversation. Telegram's private-chat id is the **user's** id, not
a per-bot one -- so both bots reported `8327566456` for the same person.

Keying conversations on that alone would have merged them: one transcript,
one outstanding proposal, and a payment plan offered by the b2b bot
acceptable to the subscription bot. Two bots that look separate in Telegram
and are a single conversation underneath is worse than not splitting them at
all, because the separation is now believed.

**Fix, and then the fix's own bug.** The subscription thread is namespaced
`sub:<chat_id>`. That separated the conversations -- and also separated
something that should not have been separated.

`_terms_for_conversation()` looks a debtor up by channel address. Given
`sub:8327566456` it found nobody, so the subscription conversation scored on
no-history defaults and recorded no promises at all. A debtor could break a
promise about their subscription and their credibility would be untouched.

**The distinction that was missing.** Conversation state is per-*thread*:
transcript, outstanding proposal, handled-message claims. A plan offered on
one bot must not be acceptable on the other. Identity and record are
per-*person*: someone who breaks a promise has broken a promise, and their
score should not reset because a different bot carried the message.

`_channel_ref_of()` strips the namespace for identity lookups and leaves it
in place for conversation state.

**Verification.** `TestTheThreadsDoNotCollide` asserts the namespace exists,
that an alert records against the namespaced thread, and -- the one that
matters -- that the b2b thread stays empty when a subscription alert fires.
Identity is checked separately: both thread ids resolve to the same debtor
and the same band.

**And the same collision existed in the UI.** The Case file called
`/demo/timeline` with no filter, so it showed both threads interleaved and a
mandate warning appeared inside the invoice story. There is now a thread
switcher, and in "All" mode subscription rows carry a tag.

## 25. The webhook's "only the demo contact" guard failed open

**Symptom.** Found by a preflight probe against the deployed service before
running a live test. A message posted from chat id `"1"` -- an id belonging
to nobody in this demo -- came back `{"ok": true, "handled": true}` instead
of `not_the_demo_contact`. It was diagnosed by a real model call and a reply
was attempted.

**Root cause.** Both Telegram webhooks guarded the same way:

```python
demo_contact = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
if demo_contact and chat_id != str(demo_contact):
    return {"ok": True, "handled": False, "reason": "not_the_demo_contact"}
```

The `demo_contact and` is the bug. With the variable unset the whole
comparison is skipped and *every* chat is accepted -- on a public endpoint.
The intent was "if we know who the contact is, enforce it"; the effect was
"if we don't know, let anyone in".

Both the b2b and subscription webhooks had it. The b2b one had never
misbehaved for one reason only: its variable happened to be set. It was one
missing environment variable away from letting any stranger who found the
bot drive the conversation, spend real model budget, and receive real
replies.

**This is the same shape as WHAT_BROKE #22.** A check that cannot
distinguish the passing case from the failing one, because the environment
made the distinction invisible. There, IST and UTC agreed on my machine.
Here, the guard and no-guard behave identically whenever the variable is
set -- which it always was, everywhere it was tested.

**Fix.** Fail closed. An unset contact refuses with
`demo_contact_not_configured` rather than opening up. There is no legitimate
case for this endpoint talking to an unknown chat, so "we don't know who to
talk to" is a reason to refuse, not a reason to accept.

**Verification.**
`TestTheContactGuardFailsClosed` deletes the variable and asserts both
webhooks refuse -- and, separately, that the configured contact still gets
through, measured by whether the *extractor was reached* rather than by the
reply. A guard that rejects at the door never gets that far, which is
precisely the difference being tested.

## 26. A debtor who paid was recorded as having broken their word

**Symptom.** Found by reading the live timeline after a real Rs 42,500
capture, not by any test. Two things were wrong at once.

```
2026-09-01T20:01:32  payment_captured  pay_TWtgGvmFoyrXAX  promise_settled: False
2026-09-01T20:01:33  payment_captured  pay_TWtgGvmFoyrXAX  promise_settled: False

debtor_live:  strict 0%  (0 of last 1 kept)  grace=1d
   broken   Rs 21,000  due 2026-09-01
```

The money arrived. The promise it answered was scored **broken**, and the
debtor's band fell from `trusted` to `strict` -- from ten days of grace and
four instalments to one day and no plan at all. Every one of those
consequences is real and every one was wrong.

**Root cause one: two different namespaces for "invoice id".** The capture
carried `inv_TWte5TwAYXxtq8` -- Razorpay's own invoice object id. The
promise was recorded against `INV-2201`, the merchant's reference, which is
what a debtor is actually told and what the conversation is about. These are
unrelated identifier spaces that happen to share a variable name.

`settle_promise()` scoped its lookup by invoice id, matched nothing, and
returned False. The promise stayed `pending`, its date passed, and
`expire_overdue_promises()` did exactly what it is supposed to do to a
promise nobody kept.

The function's own docstring says "keeps the debtor's **oldest open
promise**". The invoice scoping was an optimisation for the case where the
two ids align -- a merchant propagating its reference through the payment's
notes -- and it had quietly become the only way to match. It now falls back
to the oldest open promise, which is what the docstring always described.

**Root cause two: the same payment was announced twice.** Two
`payment.captured` events, one second apart, same `payment_id`. INGEST
de-duplicates per `(source, event_id)` and those were two different events;
`RecoveryLedger` de-duplicates per `UNIQUE(payment_id)` and correctly
attributed once. Neither stops a second *event about the same payment* from
producing a second message, and a real person received two identical
"payment received" notices.

Fixed with the same claim primitive the conversation path already uses --
`claim_message("payment_outcome:<payment_id>")`, a UNIQUE constraint rather
than a prior read. A storage failure there announces anyway: telling someone
twice is a far smaller harm than never telling them.

**Why no test caught either.** Every fixture used the same invoice id on
both sides of the match, and no test ever delivered the same payment twice.
Both are things only a real rail does. The tests were not weak so much as
polite -- they exercised the system the way it expects to be used.

**Fix, and one more thing it needed.** There was no way to correct a record
the system had got wrong. `/demo/reset` now accepts `clear_promises`, off by
default and separate from `clear_conversation`, because it deletes a record
of real events -- but a score the system computed from its own bug has to be
correctable.

**Verification.** `TestBothBugsFoundInProduction` delivers a capture whose
invoice id deliberately does not match the promise's and asserts it is kept;
delivers one payment as two events and asserts one message; and asserts a
genuinely different payment is still announced, so the claim cannot silence
real news.

## 27. I wrote the baseline against my own answers, and it nearly worked

**Symptom.** The keyword baseline built to make the golden set's accuracy
figure meaningful scored **94% class accuracy** on a 29-way problem. A
60-line regex should not do that. The number was suspicious in the
direction that mattered -- it was about to be published as the bar the
extractor cleared.

**Root cause.** I authored 49 of the 50 replies, and then wrote the regexes
with those replies on screen. Several patterns were lifted straight out of
individual items:

```python
(Family.C, CASHFLOW_SHORTFALL, r"... |account is empty|collection is (very )?bad|not paid us"),
(Family.A, INSTRUMENT_EXPIRED,  r"expired|expiry|card is old"),
```

`collection is (very )?bad` matches exactly one item, `g001`. `account is
empty` matches `g003`. And `card is old` matches `g045` -- whose own
committed note reads *"a keyword baseline should miss this"*. I had written
the test case to demonstrate the baseline's blind spot and then, forty
minutes later, patched the blind spot without noticing it was the point.

That is not a baseline. It is the answer key with extra steps, and every
percentage point it earned made the extractor's margin look smaller while
making the comparison meaningless.

**Fix.** `baseline.py` is now restricted to vocabulary a collections domain
expert would list *before* seeing the set -- `gst`, `challan`, `utr`,
`mandate`, `otp`, `cash flow` -- with a docstring stating the constraint so
the next edit has to honour it. It scores 90%.

**What the fix did not fix, and the real finding.** 90% is still very high,
and the honest reading is not that the baseline is good -- it is that **my
golden set is too easy**. Unambiguous exemplars are exactly what regexes
handle. The consequence is published rather than smoothed over: the
extractor's class-accuracy win over the baseline is **not statistically
significant** (+8.0 pp, p = 0.092), and `docs/evidence/
EXTRACTION_ACCURACY.md` says so in its own results section. The set can
show the extractor is not *worse* than a regex; it cannot show it is
better. Only family accuracy, which is what actually gates the action set,
clears the bar (p = 0.041).

**Why no test caught it.** There was no test, because the baseline was
itself the measuring instrument -- nothing was checking the checker. There
are now two: one asserting the baseline clears 50% family accuracy (below
that, beating it proves nothing) and one asserting it does not reach 100%
class accuracy (at that point the model is unnecessary, which would be a
finding of its own). Neither would have caught this specific bug. The thing
that caught it was the number looking too good.

**The near miss.** Had I written a slightly weaker baseline, it would have
scored 60%, the extractor's 98% would have looked like a decisive win, and
I would have published a comparison against a strawman I had built without
realising it. The overfitting is what made the number implausible enough to
check.

## 28. Half the first real batch failed, and the report blamed the safety gate

**Symptom.** The very first run of `tools/run_real_batch.py` against the live
account, ten decisions:

```
RB-09021254-00  INVOICE_NOT_RECEIVED  reissue_artifact  inv_TXAwJ5FkfpuzHw  PASS
...
RB-09021254-05  ERROR  BadRequestError: Too many requests
RB-09021254-06  ERROR  BadRequestError: Too many requests
RB-09021254-07  ERROR  BadRequestError: Too many requests
RB-09021254-08  ERROR  BadRequestError: Too many requests
RB-09021254-09  ERROR  BadRequestError: Too many requests
```

**Root cause one: Razorpay rate-limits invoice creation, and the batch had
no spacing and no backoff.** Five creates in rapid succession went through;
the sixth did not.

**Why no test caught it, and why none could have.** `SimulatedRail` has no
rate limit. There is no quota to exceed, so the failure mode does not exist
in the test double — the bug lived in precisely the place the doubles do not
model. The tempting fix is to teach `SimulatedRail` a quota, and that is
wrong: `docs/SIMULATOR_PROVENANCE.md` forbids putting a number in the
simulator that I have not measured, and I do not know Razorpay's actual
limit. `tests/test_real_batch_rate_limiting.py` injects the exception
instead, with the message string copied from the live traceback. Injecting
an observed failure is not an invention; inventing a threshold would be.

Fixed with two seconds between creates plus up to three retries with linear
backoff, and `_is_rate_limit()` so that only a rate limit retries. That last
part matters: the payment-link lifetime cap returns a *permanent* error, and
retrying it three times would produce the same answer three times while
making a blocked account look like a flaky one.

**Root cause two, found while writing this up: the generated report blamed
`check_bounds()` for it.** `REAL_BATCH.md` rendered all five errored rows as
`REFUSED:` with an empty reason list, because the gate column was written as

```python
gate = "pass" if r.get("bounds_passed") else "REFUSED: " + ...
```

and `bounds_passed` is simply *absent* on a row that raised before the gate
ran. So a published evidence document asserted that the safety gate refused
five actions it had never seen — while its own summary table two rows above
said `refused by check_bounds(): 0` and `rail errors: 5`. The document
contradicted itself, and the half that was wrong was the half that made the
gate look busy.

This is the more embarrassing of the two. A missing rate limiter is an
ordinary integration gap; a report that misattributes an infrastructure
failure to the component whose entire job is refusing things is the kind of
error that corrupts the evidence rather than the code. Absent is now a third
state (`n/a — rail error before dispatch`), distinct from both pass and
refuse.

**What held up.** On a run that was half errors: the per-row exception
handler recorded each failure with its type rather than stranding
half-created objects, the batch continued to completion, the ledger hash
chain verified, and all five created invoices were subsequently paid and
confirmed `paid` by fetching status back from Razorpay rather than trusting
what the agent recorded at creation time.

**The pattern this is the seventh instance of.** Every single run against a
real rail in this project has found a defect the test suite missed —
#13, #16, #22, #23, #24, #25, #26, and now this. That is not a comment on
the tests, which are extensive; it is the argument for the real batch
existing at all.

## 29. The agent issued a real mandate and then told the debtor to relax

**Symptom.** The first real WhatsApp exchange after inbound was wired up.
The debtor said *"I can pay 21,000 on the 5th and the rest by month end"*,
and the timeline shows the pipeline doing everything right:

```
13:35:57  reply_received   "I can pay 21,000 on the 5th and the rest by month end"
13:36:05  diagnosed        C / PROMISE_STATED  confidence 0.88  promise.date 2026-09-05
13:36:05  decided          escalate_human (proposed send_reminder, allowed: False)
13:36:09  plan_built       shape=stated  band=trusted  instalment_plan_offered
13:36:09  mandate_issued   sub_TXBeQY5swx95DX  https://rzp.io/rzp/0XUkcMiB
13:36:15  compose_failed   ComposeFailed: model returned no text
13:36:15  agent_replied    "Understood -- no rush, it'll confirm itself once it's paid."
```

Extraction, the gate, the plan, and a **real Razorpay e-mandate** — all
correct. Then the reply threw the mandate away and told the debtor there
was nothing to do.

**Root cause.** `_agent_reply_for()` took exactly one argument: the
diagnosis family. It could not mention a mandate because it was never told
one existed. When `compose_reply()` failed, `_compose_or_fallback()` called
it with the family alone and discarded `plan`, which held the links.

**Why this is worse than a bland fallback.** A fallback is allowed to be
bland. This one was *false*: "no rush, it'll confirm itself once it's paid"
tells someone no action is required at the exact moment an authorization is
waiting for their signature. The agent had done the entire job and then
withheld the only artifact that mattered. From the debtor's side the system
looks like it ignored a concrete offer.

There is a comment three lines above the bug, added by an earlier fix, that
describes this precise scenario as the reason compose failures are now
recorded rather than only logged. That fix made the degradation *visible*.
It did not make it *correct*, and nobody noticed the difference until a real
message went out.

**Fix.** `_agent_reply_for(family, *, mandate_links=None)`. When the
exchange produced something the debtor must act on, that outranks every
bland acknowledgement: the fallback names the links, says they need
authorizing, and keeps the "this only schedules the debit, it doesn't take
any money now" reassurance -- which is the sentence that makes someone
actually click.

**Why no test caught it.** `_agent_reply_for` had no tests at all. It was
treated as a constant lookup table rather than as the thing a debtor reads
when the model is unavailable. It has 14 now, including one asserting
Family D never receives a payment link under any argument -- answering a
disputed debt with "authorize this debit" is the Fair Practices problem the
bounds gate exists to prevent, and the fallback path bypasses the composer
entirely.

**Still unexplained: why compose returned nothing.** Reproduced locally with
the same inputs and it works — `stop_reason: end_turn`, 96 output tokens, a
correct reply naming both legs and the link. Extraction had succeeded on
Render seconds earlier, so the API was reachable. The empty response was
transient and I could not reproduce it, so I have not claimed a cause. What
changed is that a transient composer failure no longer costs the debtor the
mandate link.

## 30. Two gates, one conversation, contradictory answers

**Symptom.** Found by reading the timeline after the second live WhatsApp
exchange. The debtor made a concrete offer; the decision came back:

```
14:30:00  decided  action=escalate_human  proposed_action=send_reminder  allowed=false
                   refusals: ["PROMISE_COOLDOWN", "WHATSAPP_SESSION_WINDOW"]
```

`PROMISE_COOLDOWN` is correct — two promises inside an hour.
`WHATSAPP_SESSION_WINDOW` is not. That rule refuses a free-form WhatsApp
send when the debtor's last inbound message is older than 24 hours, and we
were **at that moment handling their inbound message**.

**Root cause.** `_decide_next_step`'s `_gate()` built a `BoundsContext`
without `last_inbound_at`, so it defaulted to `None` and rule 20 saw no
inbound at all. Every disjunct evaluated false and the rule refused:

```
action.channel != 'whatsapp'                     false
action.uses_approved_template == True            false
action.type in ['escalate_human', 'no_action']   false   (send_reminder)
last_inbound_at is not None and now < ...        false   ← never populated
```

The consequence was systematic, not occasional: **on WhatsApp, every
message-type action was refused and fell through to `escalate_human`**, so
the channel could never act autonomously. Telegram, which rule 20 does not
apply to, behaved completely differently on identical input — a divergence
with no principled basis.

Worse, two gates were running on the same conversation and disagreeing.
`_bounds_gate_followup()` sets `last_inbound_at` and allowed the reply;
`_decide_next_step` did not and refused the action. The reply went out
seconds after the gate said the window was shut.

**Why it survived a live run.** It fails safe. The wrong refusal produces
over-escalation to a human, never a prohibited send, so nothing visibly
broke — the debtor got a sensible answer and only the timeline showed the
contradiction. Failing safe is what a gate should do when uncertain; it is
also what lets a bug live quietly.

**Fix.** `last_inbound_at` is now a parameter of `_decide_next_step`, passed
by all three callers — every one of which exists solely to handle a message
that just arrived. Nine tests, the important ones being cross-channel: the
same message must reach the same action on Telegram and WhatsApp, because
nothing about the debtor's words changed. Two negative tests keep the fix
from being mistaken for switching rule 20 off — it still refuses with no
inbound timestamp, and still refuses with one four days old.

**One thing deliberately not fixed here, because it is larger.**
`BoundsContext.now` defaults to a fixed `datetime(2026, 1, 1)` and neither
this path nor `_bounds_context_for` overrides it. So the window comparison
is `2026-01-01 < <real inbound time> + 24h`, which is true for a reason
that has nothing to do with the window. The fix above makes the two paths
*agree*, which is the actual defect; moving `now` to the real clock is a
separate change that would simultaneously activate `RBI_FPC_HOURS` (20:00
IST is outside permitted calling hours) and every other time-based rule at
once. That is worth doing and is recorded here rather than smuggled in
beside an unrelated fix.

## What this list is for

I found every one of these by actually building against DEVDOC_v6, not by
re-reading it more carefully. That's my argument for building early and
literally, rather than treating a specification as settled until code
proves otherwise.
