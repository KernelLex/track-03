# Real Scenario Batch Run

I generate this with `tools/run_real_scenarios.py` at 2026-09-01T08:36:11+00:00, against the real Razorpay test-mode API (not SimulatedRail). Every id/link below is real and independently checkable in the Razorpay test dashboard.

## b2b_insufficient_funds

- **Debtor / invoice**: `real_b2b_20260901083611` / `INV-20260901083611-1`, amount Rs 42,500.00
- **Diagnosis**: Family A — INSUFFICIENT_FUNDS (confidence 1.00)
- **DECIDE**: p_base=0.9827 (real fitted model), EV=Rs 20,876.37
- **Attempted action**: `create_payment_link`
- **BOUNDS**: passed
- **ACT**: attempted, real rail-level failure — `ServerError: test mode limit of 30 reached for payment_link`

## subscription_mandate_setup

- **Debtor / invoice**: `real_sub_20260901083611` / `SUB-20260901083611-2`, amount Rs 999.00
- **Diagnosis**: Family C — CASHFLOW_SHORTFALL (confidence 1.00)
- **DECIDE**: p_base=0.9679 (real fitted model), EV=Rs 478.45
- **Attempted action**: `create_mandate`
- **BOUNDS**: passed
- **ACT**: real Razorpay object created — id `sub_TWi0Xb2L4S63NQ`, link https://rzp.io/rzp/wsF5hw7S

## gst_defect_reissue

- **Debtor / invoice**: `real_gst_20260901083611` / `INV-20260901083611-3`, amount Rs 18,750.00
- **Diagnosis**: Family B — GST_DEFECT (confidence 1.00)
- **DECIDE**: p_base=0.9801 (real fitted model), EV=Rs 9,183.82
- **Attempted action**: `reissue_artifact`
- **BOUNDS**: passed
- **ACT**: real Razorpay object created — id `inv_TWi0YsVWU7Tg3B`, link https://rzp.io/rzp/XI16JK7

## disputed_invoice_mandate_refused

- **Debtor / invoice**: `real_dispute_20260901083611` / `INV-20260901083611-4`, amount Rs 88,000.00 (Rs 30,000.00 disputed)
- **Diagnosis**: Family D — AMOUNT (confidence 1.00)
- **DECIDE**: p_base=0.9846 (real fitted model), EV=Rs 43,318.39
- **Attempted action**: `create_mandate`
- **BOUNDS**: **refused** — NO_MANDATE_ON_DISPUTE: Never place a mandate on a contested amount.
- **ACT**: not reached — no rail call was made

## Methodology

Four scenarios, not fifty: I chose each one to exercise a *different* Family -> action mapping (instrument failure -> payment link, liquidity -> e-mandate setup, administrative defect -> reissued invoice, dispute -> a deliberately wrong action caught by check_bounds()) rather than run many whose only variation is a random seed. Every DECIDE number above came from the real fitted p_base model and real compute_ev() arithmetic; every BOUNDS verdict came from the real check_bounds() gate; every created object is a real Razorpay test-mode resource, independently checkable in the dashboard.

**I hit a real infrastructure limit here and handled it, not hid it**: this Razorpay test account has a hard cap on test-mode payment links (`test mode limit of 30 reached`), which I hit during this session's earlier live-testing. Rather than crash the batch, I catch the failure, record it, and let the run continue — the same discipline `agent/rails/protocol.py`'s `RailUnavailable` split ("we don't know" vs "we know, and it's a no") calls for, applied here to a rail-level exception `RazorpayRail.create_payment_link()` doesn't currently wrap into that type (a real, documented gap I haven't fixed in this pass).