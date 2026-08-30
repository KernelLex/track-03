"""Money as integer paise, everywhere. Never float. DEVDOC_v6 §9.1.

Format to rupees only at the display boundary (`to_rupees_display`). Every
other layer of the system passes `Money` — a plain `int` at runtime, tagged
at the type level so a reviewer sees a `float` in a Money-typed position on
sight.
"""

from __future__ import annotations

from typing import NewType

Money = NewType("Money", int)
"""Integer paise. Rs 1 = Money(100). Never construct Money from a float literal."""


def paise(rupees: int, extra_paise: int = 0) -> Money:
    """Construct Money from whole rupees (+ optional paise) — the one literal-value constructor."""
    if not isinstance(rupees, int) or not isinstance(extra_paise, int):
        raise TypeError("paise() takes int rupees and int extra_paise — never float")
    return Money(rupees * 100 + extra_paise)


def to_rupees_display(amount: Money) -> str:
    """Format paise as a rupee string for display. Never parse this string back into Money."""
    value = int(amount)
    sign = "-" if value < 0 else ""
    whole, rem = divmod(abs(value), 100)
    return f"{sign}₹{whole:,}.{rem:02d}"


def assert_money(value: object, *, field: str = "value") -> Money:
    """Runtime guard: raise if a float (or non-int) reached a Money-typed boundary.

    Call this at every rail-response parsing boundary, per §9.1's "lint rule
    against float arithmetic" — this is the runtime half of that guard; a
    static linter rule is future work (see LIMITATIONS.md).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be int paise, got {type(value).__name__} ({value!r}) — see DEVDOC_v6 §9.1")
    return Money(value)
