"""A book of recurring mandates, and the money already lost inside it.

The subscription half of this project had no live surface. `check_mandate_health()`
has existed, been tested, and produced the Rs 91,72,435 headline since early on
-- and it was reachable from no endpoint. An offline tool ran it once, wrote a
markdown file, and that was the entire demonstration.

That is the wrong thing to leave unreachable, because it is the strongest claim
here. Detection is **arithmetic on the mandate's own fields**, not a prediction:
`max_amount_paise < upcoming_debit_paise` is a comparison. There is no persona,
no fitted probability, no assumed behaviour. A defect provable from an object's
shape needs none.

So this module is the portfolio that detector reads, and it exists so a person
can watch the real check run against real numbers rather than read a number
somebody else computed.

**What is declared and what is measured**, since the distinction is the whole
point: the *defect rates in this book are declared* -- these mandates were
constructed with breaches, exactly as `docs/evidence/AT_RISK_HEADLINE.md`
declares its 12%/8%. Nothing here measures how often real Indian subscriptions
carry a headroom breach. What is genuinely zero-assumption is the conditional:
given a mandate has the defect, the detector catches it, every time, because it
is an inequality.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.clock import business_now
from agent.db import connect
from agent.mandate.health import (
    HealthCheckInput,
    MandateSnapshot,
    check_mandate_health,
)


@dataclass(frozen=True, slots=True)
class PortfolioMandate:
    """One recurring mandate, with the next debit it has to survive."""

    mandate_id: str
    customer: str
    plan: str
    max_amount_paise: int
    upcoming_debit_paise: int
    end_at: str
    next_debit_date: str
    status: str = "active"
    afa_scheduled: bool = False
    consecutive_nsf: int = 0
    issuer_failure_rate: float | None = None
    cycle_was_attempted: bool = True
    is_real: bool = False
    """True only for a mandate that exists on the real Razorpay account.
    Surfaced everywhere it is shown, so a constructed row can never be read
    as evidence of a real one -- the same discipline `is_seeded` carries in
    the debtor register."""

    def to_health_input(self) -> HealthCheckInput:
        """Feeds the *real* detector. No parallel implementation, no
        simplified copy for the demo -- this is the same function the
        headline evidence and the repair lifecycle both call."""
        return HealthCheckInput(
            mandate=MandateSnapshot(
                max_amount_paise=self.max_amount_paise,
                end_at=datetime.fromisoformat(self.end_at),
                status=self.status,
                afa_scheduled=self.afa_scheduled,
                consecutive_nsf=self.consecutive_nsf,
                issuer_failure_rate=self.issuer_failure_rate,
            ),
            upcoming_debit_paise=self.upcoming_debit_paise,
            next_debit_date=datetime.fromisoformat(self.next_debit_date),
            cycle_was_attempted=self.cycle_was_attempted,
        )


class MandatePortfolio:
    def __init__(self, db_path: str = "debtors.db"):
        self.db_path = db_path
        self._conn = connect(db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_mandates (
                mandate_id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                plan TEXT NOT NULL,
                max_amount_paise INTEGER NOT NULL,
                upcoming_debit_paise INTEGER NOT NULL,
                end_at TEXT NOT NULL,
                next_debit_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                afa_scheduled INTEGER NOT NULL DEFAULT 0,
                consecutive_nsf INTEGER NOT NULL DEFAULT 0,
                issuer_failure_rate REAL,
                cycle_was_attempted INTEGER NOT NULL DEFAULT 1,
                is_real INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MandatePortfolio":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add_if_absent(self, m: PortfolioMandate) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO portfolio_mandates (mandate_id, customer, plan, max_amount_paise, "
                "upcoming_debit_paise, end_at, next_debit_date, status, afa_scheduled, "
                "consecutive_nsf, issuer_failure_rate, cycle_was_attempted, is_real) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m.mandate_id, m.customer, m.plan, m.max_amount_paise, m.upcoming_debit_paise,
                 m.end_at, m.next_debit_date, m.status, int(m.afa_scheduled), m.consecutive_nsf,
                 m.issuer_failure_rate, int(m.cycle_was_attempted), int(m.is_real)),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            return False

    def upsert(self, m: PortfolioMandate) -> None:
        self._conn.execute("DELETE FROM portfolio_mandates WHERE mandate_id = ?", (m.mandate_id,))
        self._conn.commit()
        self.add_if_absent(m)

    def all(self) -> list[PortfolioMandate]:
        rows = self._conn.execute(
            "SELECT mandate_id, customer, plan, max_amount_paise, upcoming_debit_paise, end_at, "
            "next_debit_date, status, afa_scheduled, consecutive_nsf, issuer_failure_rate, "
            "cycle_was_attempted, is_real FROM portfolio_mandates ORDER BY is_real DESC, next_debit_date"
        ).fetchall()
        return [
            PortfolioMandate(
                mandate_id=r[0], customer=r[1], plan=r[2], max_amount_paise=r[3],
                upcoming_debit_paise=r[4], end_at=r[5], next_debit_date=r[6], status=r[7],
                afa_scheduled=bool(r[8]), consecutive_nsf=r[9], issuer_failure_rate=r[10],
                cycle_was_attempted=bool(r[11]), is_real=bool(r[12]),
            )
            for r in rows
        ]


def scan(mandates: list[PortfolioMandate]) -> dict[str, object]:
    """Run the real detector across the book and total what is at risk.

    "At risk" counts a mandate's upcoming debit **once**, however many
    defects it carries -- a mandate with both a headroom breach and an
    early expiry is one debit that will fail, not two. Summing per defect
    would inflate the rupee figure, which is exactly the kind of number
    that falls apart the moment someone checks it.
    """
    rows: list[dict[str, object]] = []
    at_risk_paise = 0
    defect_counts: dict[str, int] = {}

    for m in mandates:
        defects = check_mandate_health(m.to_health_input())
        if defects:
            at_risk_paise += m.upcoming_debit_paise
        for d in defects:
            defect_counts[d.defect.value] = defect_counts.get(d.defect.value, 0) + 1
        rows.append({
            "mandate_id": m.mandate_id,
            "customer": m.customer,
            "plan": m.plan,
            "max_amount_paise": m.max_amount_paise,
            "upcoming_debit_paise": m.upcoming_debit_paise,
            "next_debit_date": m.next_debit_date,
            "end_at": m.end_at,
            "status": m.status,
            "is_real": m.is_real,
            "healthy": not defects,
            "defects": [
                {"defect": d.defect.value, "repair": d.repair, "detail": d.detail}
                for d in defects
            ],
        })

    unhealthy = [r for r in rows if not r["healthy"]]
    return {
        "scanned": len(rows),
        "defective": len(unhealthy),
        "at_risk_paise": at_risk_paise,
        "defect_counts": defect_counts,
        "mandates": rows,
        "scanned_at": business_now().isoformat(),
        "note": (
            "Detection is arithmetic on each mandate's own fields, not a prediction -- "
            "no persona, no fitted probability. The defect rates in this book are declared, "
            "not measured: what is zero-assumption is that a mandate carrying a defect is "
            "always caught, because the check is an inequality."
        ),
    }


FAILURE_KINDS = {
    "headroom": "sub_HEADROOM1",
    "expiry": "sub_EXPIRY001",
    "afa": "sub_AFA00001",
    "nsf": "sub_NSF00001",
    "revoked": "sub_REVOKED1",
    "rail": "sub_RAILDEG1",
}
"""The failure a demo button asks for, mapped to the mandate constructed to
carry it. Named rather than "pick any broken one" so a person driving the
demo can show a *specific* defect on purpose -- the six are genuinely
different failures with genuinely different repairs, and a demo that always
showed the same one would undersell the detector."""


# (mandate_id, customer, plan, max_paise, debit_paise, end_offset_days,
#  next_debit_offset_days, status, afa, nsf, issuer_rate, attempted)
#
# Constructed to span the detector's range: two clean mandates, and one of
# each defect it can find. The clean ones matter as much as the broken --
# a detector that flags everything is not a detector.
_SEED = [
    ("sub_HLTHY001", "Nandini Dairy Supplies", "Monthly supply plan",
     25_000_00, 21_500_00, 240, 6, "active", True, 0, 0.02, True),
    ("sub_HLTHY002", "Prakash Stationers", "Quarterly retainer",
     60_000_00, 48_000_00, 300, 11, "active", True, 0, None, True),

    ("sub_HEADROOM1", "Vertex Packaging", "Monthly supply plan",
     18_000_00, 21_500_00, 200, 4, "active", True, 0, 0.03, True),
    ("sub_EXPIRY001", "Coastal Freight Co", "Monthly logistics",
     40_000_00, 32_000_00, 3, 9, "active", True, 0, 0.01, True),
    ("sub_AFA00001", "Sterling Chemicals", "Bulk order plan",
     90_000_00, 74_000_00, 260, 7, "active", False, 0, 0.04, True),
    ("sub_NSF00001", "Rapid Print House", "Monthly retainer",
     15_000_00, 12_500_00, 190, 5, "active", True, 3, 0.06, True),
    ("sub_REVOKED1", "Anand Textiles", "Monthly supply plan",
     30_000_00, 27_000_00, 220, 8, "revoked", True, 0, 0.02, False),
    ("sub_RAILDEG1", "Meridian Logistics", "Monthly logistics",
     55_000_00, 44_000_00, 210, 12, "active", True, 0, 0.31, True),
]


def seed_portfolio(portfolio: MandatePortfolio, *, now: datetime | None = None) -> int:
    """Idempotent, and never overwrites a real mandate pulled from the rail."""
    now = now or business_now()
    written = 0
    for (mid, cust, plan, mx, debit, end_off, next_off, status,
         afa, nsf, rate, attempted) in _SEED:
        written += portfolio.add_if_absent(PortfolioMandate(
            mandate_id=mid, customer=cust, plan=plan,
            max_amount_paise=mx, upcoming_debit_paise=debit,
            end_at=(now + timedelta(days=end_off)).isoformat(),
            next_debit_date=(now + timedelta(days=next_off)).isoformat(),
            status=status, afa_scheduled=afa, consecutive_nsf=nsf,
            issuer_failure_rate=rate, cycle_was_attempted=attempted,
            is_real=False,
        ))
    return written
