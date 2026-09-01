# The persona-free headline (DEVDOC_v6 §26)

I generate this with `tools/compute_at_risk_headline.py` at 2026-09-01T08:30:23+00:00.

**I constructed 191 of 1000 synthetic mandates with a real, structural defect (undersized headroom or an expiry preceding the next debit) — `check_mandate_health()` detected all 191 of them, zero missed, zero false alarms. Rs 9,172,435.00 in upcoming debits was structurally guaranteed to fail before I ever checked this batch.**

No persona, no `p_base`, no `lift_prior` — this is arithmetic on the mandate's own object shape (`max_amount_paise < upcoming_debit_paise`, `end_at < next_debit_date`), not a prediction about how anyone behaves.

- Detection: 191/191 true positives, 0 false negatives, 0 false positives (expect 0/0 — detection here is deterministic, not probabilistic)
- Rs at risk: **Rs 9,172,435.00** across 191 mandates

## What's a real, zero-assumption claim here, and what isn't

My build has no real production mandate corpus yet, so I constructed this batch of 1000 **synthetically**: I deliberately built 12% with a headroom breach, 8% with an expiry breach (independently, so some carry both) — **construction parameters I declared, not a measured real-world defect rate**. What *is* a genuine, zero-persona claim: given a mandate already has one of these defects, the detector catches it every time — an equality/inequality check on fields the mandate object already has, the same detection code (`agent/mandate/health.py::check_mandate_health()`) that runs in my live orchestrator and in `agent/mandate/lifecycle.py`'s real repair-then-debit flow.
