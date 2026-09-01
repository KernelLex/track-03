# Adversarial personas (DEVDOC_v6 §24.3)

I run each of 100 synthetic personas through the real `check_bounds()` gate under three mechanically-distinct exploit strategies, over a 90-day window. The one number that matters: **cases permanently stalled**, which must be 0 for my §24.2 fixes to be doing their job.

| Strategy | n | Cases permanently stalled | Mean attempts before first successful recontact/escalation |
|---|---|---|---|
| `SERIAL_PROMISER` | 100 | **0** | 1.0 |
| `DISPUTE_ABUSER` | 100 | **0** | 1.0 |
| `CHANNEL_HOPPER` | 100 | **0** | 1.0 |

**Total cases permanently stalled across all three strategies and all 300 simulated runs: 0.**

## What each strategy actually does, and what fixed it

- **`SERIAL_PROMISER`** — promises on every contact, never pays. The naive rule (a full, fixed cooldown per promise) lets this stall collection forever for one sentence per cycle. My fix: cooldown scales by `promise_credibility` (`kept/(kept+broken)` over the trailing 5 promises), which decays toward `ConfigCtx.promise_credibility_floor` as promises keep breaking — a serial promiser's own cooldown shrinks the more they exploit it.
- **`DISPUTE_ABUSER`** — asserts an unsubstantiated dispute on first contact. `DISPUTE_FREEZE` correctly blocks a plain collection touch against the disputed amount, and `escalate_human` correctly passes every time it's tried while the case is genuinely un-escalated yet — the case reaches a human, it doesn't go silent. **A real finding I surfaced while building this**: `recovery_attempts` in this simulation counts every logged attempt including escalations, so `ATTEMPT_CEILING` (`< 6`) can eventually block *further* re-escalation attempts too — but since the case already reached a human on its first attempt, later attempts being blocked isn't a stall, it's the case correctly sitting in the human queue rather than being re-escalated on a loop.
- **`CHANNEL_HOPPER`** — opts out of one channel per contact. `CHANNEL_EXHAUSTION` correctly keeps allowing contact on whichever channels remain, and once every channel is opted out, `escalate_human` correctly passes (channel-untagged, since escalation isn't itself a commercial communication on any channel — an earlier version of my harness tagged it with the debtor's last-used, already-opted-out channel, which let the unrelated `TRAI_DND` rule block escalation too; I fixed that before generating this evidence, not after).

**I don't simulate `INJECTOR` here.** Its exploit is prompt injection through free debtor text, which my harness has no live model call or free-text path to exercise at all — `tests/agent/test_injection_resistance.py` (80 tests, a 40-case corpus) already proves this against the real schema and action-set mapping; re-implementing a weaker stand-in here would look like coverage without adding any.