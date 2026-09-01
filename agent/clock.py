"""What "today" means to an Indian business.

`date.today()` is the server's local date. On Render that is UTC, and this
system's debtors are in India. IST is UTC+05:30, so between 00:00 and 05:30
IST the UTC date is still yesterday -- and for those five and a half hours
every relative date the system resolves is one day early.

That is not theoretical. A live run had a debtor write "I can pay 21000
today", and the extractor -- told the UTC date -- resolved "today" to
2026-09-01 while the debtor's own calendar said 2026-09-02. A payment
scheduled a day before the debtor agreed to it is a real problem, not a
cosmetic one: it is a debit on a date they did not name.

**Why a fixed offset rather than a timezone database.** India has a single
timezone and has never observed daylight saving. `Asia/Kolkata` is UTC+05:30
year-round, so a fixed offset is exactly correct here and has no
dependency, no tzdata version to drift, and nothing to go wrong on a
platform whose tzdata is missing (which is common in slim containers).
A system serving multiple countries would need the real thing; this one
does not, and pretending otherwise would be more code for no more
correctness.

`TRUECOMMIT_TIMEZONE_OFFSET_MINUTES` overrides it for a deployment
somewhere else, so the choice is configurable rather than baked in.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
"""UTC+05:30, year-round. See the module docstring for why this is a fixed
offset and not a tzdata lookup."""

_OFFSET_ENV = "TRUECOMMIT_TIMEZONE_OFFSET_MINUTES"


def business_timezone() -> timezone:
    """IST unless a deployment declares otherwise."""
    raw = os.environ.get(_OFFSET_ENV)
    if not raw:
        return IST
    try:
        minutes = int(raw)
    except ValueError:
        return IST
    # A timezone more than a day from UTC is a typo, not a place.
    if not -24 * 60 < minutes < 24 * 60:
        return IST
    return timezone(timedelta(minutes=minutes))


def business_now() -> datetime:
    """Now, in the debtor's timezone."""
    return datetime.now(business_timezone())


def business_today() -> date:
    """Today, as the debtor's calendar has it.

    Every place that resolves a relative date -- "today", "next Friday",
    the early-payment window, a promise horizon -- should use this rather
    than `date.today()`, so the whole system agrees on what day it is.
    """
    return business_now().date()
