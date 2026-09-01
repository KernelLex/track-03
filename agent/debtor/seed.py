"""The debtors the demo starts with.

Four seeded businesses spanning the score bands, plus the real person
testing this system on their own Telegram chat. The seeded histories are
*declared*, not lived -- these promises were never made and never kept, and
the register stores `is_seeded=1` on every one of them so nothing
downstream can mistake a fixture for evidence. They exist to show what the
scoring does across its range, which one real debtor with one real
conversation cannot demonstrate.

The real debtor is seeded with **no promise history at all**, deliberately.
Their score is whatever their own replies and payments earn it during a
demo, starting from the same benefit of the doubt any new debtor gets. A
pre-loaded history for the live user would make the one genuinely real row
in this table the one most contaminated by fixtures.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from agent.debtor.registry import Debtor, DebtorRegistry

REAL_DEBTOR_ID = "debtor_live"
"""The person running the demo, reachable on DEMO_CONTACT_TELEGRAM_CHAT_ID."""

_SEEDED = [
    (Debtor(id="debtor_kavya", display_name="Kavya Textiles Pvt Ltd", channel="telegram",
            channel_ref="seed_kavya", invoice_id="INV-2188", invoice_amount_paise=68_000_00,
            is_seeded=True, note="Pays on the date they name, every time."),
     # (days_ago_promised, amount_paise, outcome)
     [(120, 68_000_00, "kept"), (95, 22_000_00, "kept"), (60, 40_000_00, "kept"),
      (30, 18_000_00, "kept")]),

    (Debtor(id="debtor_meridian", display_name="Meridian Logistics", channel="telegram",
            channel_ref="seed_meridian", invoice_id="INV-2194", invoice_amount_paise=1_15_000_00,
            is_seeded=True, note="Mostly good; slipped once on a large instalment."),
     [(110, 50_000_00, "kept"), (80, 35_000_00, "kept"), (45, 60_000_00, "broken"),
      (20, 30_000_00, "kept")]),

    (Debtor(id="debtor_sunrise", display_name="Sunrise Auto Components", channel="telegram",
            channel_ref="seed_sunrise", invoice_id="INV-2203", invoice_amount_paise=37_500_00,
            is_seeded=True, note="Promises readily, keeps about half."),
     [(100, 20_000_00, "broken"), (75, 15_000_00, "kept"), (50, 25_000_00, "broken"),
      (25, 12_000_00, "kept")]),

    (Debtor(id="debtor_orbit", display_name="Orbit Traders", channel="telegram",
            channel_ref="seed_orbit", invoice_id="INV-2211", invoice_amount_paise=92_000_00,
            is_seeded=True, note="Four promised dates, one payment."),
     [(105, 25_000_00, "broken"), (85, 25_000_00, "broken"), (55, 20_000_00, "kept"),
      (28, 22_000_00, "broken"), (14, 30_000_00, "broken")]),
]


def seed_registry(registry: DebtorRegistry, *, today: date | None = None) -> list[str]:
    """Idempotent: safe to call on every boot.

    Promise rows are only written for a debtor with none, so a restart
    can't inflate a seeded history -- and a real debtor's earned score is
    never touched by this at all.
    """
    today = today or date.today()
    written: list[str] = []

    for debtor, promises in _SEEDED:
        registry.upsert(debtor)
        if registry.outcomes_for(debtor.id):
            continue
        for days_ago, amount_paise, outcome in promises:
            registry.record_promise(
                debtor.id, invoice_id=debtor.invoice_id, amount_paise=amount_paise,
                promised_date=(today - timedelta(days=days_ago)).isoformat(),
                outcome=outcome,
                # A kept promise is justified by a capture. These are
                # declared, so the id says so rather than inventing a
                # Razorpay-shaped one that would read as real.
                payment_id=f"seeded_{debtor.id}_{days_ago}" if outcome == "kept" else None,
            )
        written.append(debtor.id)

    chat_id = os.environ.get("DEMO_CONTACT_TELEGRAM_CHAT_ID")
    if chat_id:
        registry.upsert(Debtor(
            id=REAL_DEBTOR_ID, display_name="You (live demo contact)", channel="telegram",
            channel_ref=str(chat_id), invoice_id="INV-2201", invoice_amount_paise=42_500_00,
            is_seeded=False,
            note="Real. Score is earned from your own replies and payments during a demo.",
        ))
        written.append(REAL_DEBTOR_ID)

    return written
