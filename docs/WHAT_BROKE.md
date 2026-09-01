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

## What this list is for

I found every one of these by actually building against DEVDOC_v6, not by
re-reading it more carefully. That's my argument for building early and
literally, rather than treating a specification as settled until code
proves otherwise.
