# What Broke

DEVDOC_v6 §20: symptom → root cause → fix → verification. Every entry below
is a real defect found while implementing this spec in this session — not
a hypothetical, and not edited after the fact to look cleaner than it was.
Each one was fixed in both the specification (`DEVDOC_v6.md`) and the code
in the same commit, so the two never drifted back apart.

## 1. A bounds bypass: three regulatory rules were unconditional

**Symptom.** Running `trucommit demo` — a plain `send_reminder` action —
`check_bounds()` refused it, citing `RBI_EMANDATE_PREDEBIT_24H`.

**Root cause.** `RBI_EMANDATE_PREDEBIT_24H`, `RBI_EMANDATE_POSTDEBIT`, and
`MSMED_INTEREST_BASIS` were written as unconditional checks in every prior
revision of the spec — they didn't scope themselves to the one action type
they actually govern (presenting a mandate debit; asserting a statutory
interest figure). Taken literally, almost no action has
`mandate.last_notification_at` set, so **no action of any kind could ever
pass `check_bounds()`**. This is the inverse of a bypass — a gate so broad
it blocks legitimate traffic — but it's the same class of defect: a rule
whose actual behaviour doesn't match its intent, found only by running the
real system instead of reading the rule.

**Fix.** Guarded each with the same `action.presents_mandate_debit =>` /
`action.type == 'send_statutory_notice' =>` implication pattern
`MANDATE_PARAM_CLAMP` and `NO_MANDATE_ON_DISPUTE` already used. `RBI_EMANDATE_OPTOUT`
turned out to have the identical defect, found the same way one commit
later: unscoped, it blocked *every* action while a debtor was opted out —
including `revoke_mandate`, the one action §11.6 requires to run
autonomously specifically *because of* that opt-out.

**Verification.** `tests/agent/test_bounds_engine.py::
test_predebit_24h_only_applies_to_actions_that_present_a_mandate_debit` and
`test_optout_only_blocks_actions_that_present_a_mandate_debit` construct
the exact failing scenario and assert the gate now passes a non-debit
action while still refusing an actual under-notified or opted-out debit.

## 2. An illegal-transition-shaped bug: a gate that could never fire

**Symptom.** None visible in a test — this one was caught by inspection
while translating rules.yaml, not by a failing assertion, which is exactly
why it's worth recording.

**Root cause.** `DISPUTE_FREEZE`'s machine rule checked
`action.type in ['human_escalate', 'none']`. The real action names,
everywhere else in the system (`agent/act/actions.py`, §11.5's table), are
`escalate_human` and `no_action`. The comparison could never match anything
a real `Decision` produces — the gate was permanently, silently inert. My
own `CHANNEL_EXHAUSTION` addition copied the same wrong names one commit
later, propagating the bug into new code before it was caught.

**Fix.** Corrected both rules to the real action names; added a note in
`DEVDOC_v6.md` §13.2 explaining the mismatch so it can't quietly happen a
third time.

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
*enforcing* that gate are two different things, and only the metadata
existed. Found while writing a test for the ACT executor that expected
`initiate_refund` without approval to be refused — it wasn't.

**Fix.** Added `REFUND_AND_REVOKE_HUMAN_GATE` to the bounds register,
including the one case that must stay autonomous: revoking a mandate
because the debtor opted out this cycle (§11.6 — refusing to reverse on
opt-out is itself a violation).

**Verification.**
`tests/agent/test_act_executor.py::test_initiate_refund_without_human_approval_is_refused_by_bounds`,
`test_revoke_mandate_without_approval_or_optout_is_refused_by_bounds`, and
`test_revoke_mandate_on_debtor_optout_proceeds_autonomously` all exercise
the real `execute_action()` path, not just the bounds engine in isolation.

## 4. Law 4 unenforced: ACT never wrote to the ledger

**Symptom.** None visible until it blocked the next thing being built (the
Auditor's bounds-integrity job, which needs a recorded snapshot of what
`check_bounds()` actually saw for an executed action).

**Root cause.** `agent/act/executor.py` — the one stage that moves money —
never appended a single `LedgerEntry`. Every dispatch, refusal, and deduped
retry happened, was recorded in `OutboundActionStore` for idempotency
purposes, and then vanished with no trace in the append-only audit trail
Law 4 requires ("agents coordinate only through the ledger"). This is
arguably the most serious finding in this list: not a rule that was too
strict or too loose, but a safety mechanism that was simply never wired up,
in the one place it mattered most.

**Fix.** Made `ledger` a required parameter of `execute_action()` — not
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
— the next multi-line rule could reintroduce the same bug), added a
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

**Root cause.** The test built its "debtor has opted out of everything"
fixture by hand-listing the four channels that existed when it was written
(`frozenset({"sms", "email", "whatsapp", "ivr"})`) instead of referencing
`ALL_CHANNELS` itself. The rule's own logic
(`len(debtor.opted_out_channels) < len(ALL_CHANNELS)`) was never wrong —
once a fifth channel exists, a debtor who opted out of only the original
four genuinely *hasn't* exhausted their contact options anymore (Telegram
is still open to them), so `PASS` was the correct answer to the question
the stale fixture was accidentally now asking. The bug was the fixture
silently assuming a closed world, not the rule.

**Fix.** Changed the three "every channel" fixtures in
`tests/agent/test_bounds_engine.py` to opt out of `ALL_CHANNELS` directly
rather than a hardcoded copy of its current members, so the test keeps
asking the question it means to ask ("every channel exhausted") regardless
of how many channels exist later.

**Verification.**
`tests/agent/test_bounds_engine.py::test_channel_exhaustion_routes_to_human_instead_of_going_silent`
passes against the five-channel set; the full suite (561 tests at time of
writing) was re-run afterward specifically to catch any other place a
channel list had been copied instead of imported — none found.

## What this list is for

Every one of these was found by actually building against DEVDOC_v6, not
by re-reading it more carefully. That's the argument for building early and
literally, rather than treating a specification as settled until code
proves otherwise.
