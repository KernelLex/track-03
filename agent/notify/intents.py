"""Reading the handful of things a debtor says that are commands, not prose.

"2", "my invoices", "dispute this" -- these are menu selections. They do not
need a language model, and routing them through one would be worse on every
axis that matters: a model call costs money and about four seconds, and a
misclassified "2" sends the conversation somewhere the debtor did not ask
to go.

So this is deliberately deterministic and deliberately narrow. It matches
short, unambiguous messages and returns None for everything else, which
falls through to the real extractor. **Ambiguity resolves to None**, always:
a message that is arguably a command and arguably prose is prose, because
the extractor can read nuance and this cannot.

**Law 8 still holds.** The debtor's text is data here too. Matching a
keyword decides which of *this system's* code paths runs; it never lets the
message assert a fact, change what is owed, or reach an instruction
channel. "Mark invoice 3 as paid" matches nothing here and is classified by
the extractor as the claim it is (`ALREADY_PAID_UNRECONCILED`), which is
checked against the rail rather than believed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COMMAND_CHARS = 60
"""Commands are short. A long message that happens to contain "dispute" is
prose about a dispute, and belongs to the extractor -- this cap is what
stops a keyword anywhere in a paragraph from hijacking the routing."""


@dataclass(frozen=True, slots=True)
class Intent:
    kind: str
    """'list' | 'select' | 'schedule' | 'dispute' | 'problem' | 'help'"""
    invoice_ref: str | None = None
    """An invoice id ("INV-2201") or a 1-based list position ("2"), exactly
    as the debtor wrote it -- resolving which invoice that means needs their
    list, which this module deliberately does not have."""


_LIST_PATTERNS = (
    r"\b(my|which|what|any)\s+(invoice|invoices|bills?|dues?)\b",
    r"\binvoice\s*list\b",
    r"\b(list|show|see)\s+(my\s+)?(invoice|invoices|bills?|dues?)\b",
    r"\bwhat\s+do\s+i\s+owe\b",
    r"\bhow\s+much\s+do\s+i\s+owe\b",
    r"\b(outstanding|pending)\s+(invoice|invoices|amount|balance|dues?)\b",
    r"^\s*(invoices?|dues?|balance|status|statement)\s*[?.!]*\s*$",
)

_HELP_PATTERNS = (r"^\s*(help|menu|options|what can you do)\s*[?.!]*\s*$",)

_SCHEDULE_PATTERNS = (
    r"^\s*(schedule|set\s*up|setup|plan|instal?ments?|emandate|e-?mandate|autopay)\b",
    r"\b(schedule|set\s*up)\s+(a\s+)?(payment|plan|instal?ments?)\b",
)

_DISPUTE_PATTERNS = (
    r"^\s*(dispute|raise\s+a?\s*dispute|contest|disputed)\b",
    r"\b(raise|open|file)\s+a\s+dispute\b",
    r"\bi\s+(dispute|contest)\s+(this|it)\b",
)

_PROBLEM_PATTERNS = (
    r"^\s*(problem|issue|help me|i have a problem|something.s wrong)\b",
    r"\bi\s+have\s+(a\s+)?(problem|issue)\b",
    r"\b(talk|speak)\s+to\s+(a\s+)?(person|human|someone|agent)\b",
)

_INVOICE_ID = re.compile(r"\b((?:INV|SUB)[-\s]?\d{3,})\b", re.I)
_BARE_NUMBER = re.compile(r"^\s*#?\s*([1-9]\d?)\s*[.)]?\s*$")


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def detect_intent(text: str) -> Intent | None:
    """A command, or None if this is prose for the extractor to read.

    Order matters: an explicit action beats a bare selection, because
    "dispute INV-2201" names both and the verb is what they want done.
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()
    if len(stripped) > MAX_COMMAND_CHARS:
        # Long enough to be a real message. Even if it contains "dispute",
        # the extractor should read it -- it can tell a dispute from a
        # sentence mentioning one, and this cannot.
        return None

    invoice_match = _INVOICE_ID.search(stripped)
    invoice_ref = None
    if invoice_match:
        invoice_ref = re.sub(r"[\s]", "", invoice_match.group(1)).upper()
        if "-" not in invoice_ref:
            invoice_ref = re.sub(r"^(INV|SUB)", r"\1-", invoice_ref)

    if _matches(stripped, _DISPUTE_PATTERNS):
        return Intent("dispute", invoice_ref)
    if _matches(stripped, _PROBLEM_PATTERNS):
        return Intent("problem", invoice_ref)
    if _matches(stripped, _SCHEDULE_PATTERNS):
        return Intent("schedule", invoice_ref)
    if _matches(stripped, _LIST_PATTERNS):
        return Intent("list")
    if _matches(stripped, _HELP_PATTERNS):
        return Intent("help")

    # A bare selection: "2", "#2", "INV-2201". Only when that is the entire
    # message -- a number inside a sentence is an amount or a date, and
    # reading it as a menu choice is exactly the misfire this guards.
    bare = _BARE_NUMBER.match(stripped)
    if bare:
        return Intent("select", bare.group(1))
    if invoice_ref and _INVOICE_ID.sub("", stripped).strip(" .?!#:") == "":
        return Intent("select", invoice_ref)

    return None
