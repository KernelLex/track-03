"""A keyword classifier for debtor replies -- the thing the LLM has to beat.

Without this, "the extractor gets N% on the golden set" is unfalsifiable:
N% of what? A 29-way problem with an uneven prior can be gamed by always
answering the majority class, and a reader has no way to tell whether the
model is doing work or whether the task is easy.

So this is written as a *good-faith* baseline, not a strawman. It gets the
patterns a careful engineer would actually reach for first: the vocabulary
of Indian B2B collections, Hinglish included, ordered so that the more
specific signal wins when two fire. If the LLM cannot beat this, the LLM is
not earning its cost or its latency, and the honest move is to ship this
instead.

**One constraint on what may go in here, because the first version broke
it.** Every pattern below must be vocabulary a collections domain expert
would list *before* seeing the golden set. No phrase may be copied out of a
specific item. The first draft ignored that and scored 94% -- because I had
written the replies and then written regexes against my own phrasing, right
down to putting `card is old` in INSTRUMENT_EXPIRED while item g045's note
said in as many words that a keyword baseline should miss it. That is not a
baseline, it is an answer key with extra steps.

The rewritten version keeps a real advantage the extractor does not get: I
have still read the set, and the class vocabulary is chosen knowing which
classes appear in it. The bias therefore runs *toward* the baseline, which
means any margin the extractor shows over it is a lower bound, not a
flattering one. That is the correct direction for a number I am publishing
about my own system.

Its remaining structural weakness is the real argument for the model: it
matches surface forms. "my card expired" it gets; "the card you have on
file is old" says the same thing with no shared keyword, and it cannot.
"""

from __future__ import annotations

import re

from agent.diagnose.extract import DiagnosisClass, Family

# Ordered most-specific first: the first rule that fires wins. Ordering is
# doing real work here -- "already paid ... please reconcile" must beat the
# generic payment vocabulary, and an approval blocker must beat stalling.
RULES: list[tuple[Family, DiagnosisClass, str]] = [
    # Family D -- disputes. Placed first: a substantive dispute outranks the
    # refusal vocabulary it often contains ("we are not paying for this lot").
    (Family.D, DiagnosisClass.NOT_OUR_DEBT, r"wrong number|not our (debt|dues|bill)|never dealt|no such account|sister concern|different entity"),
    (Family.D, DiagnosisClass.CONTRACT, r"as per (the )?(clause|contract|agreement|terms)|per our agreement|contract.{0,20}terminat|payment terms|credit period"),
    (Family.D, DiagnosisClass.QUANTITY_QUALITY, r"damaged|defect|short supply|shortage|short.?shipped|missing|rejected|quality|not as per spec"),
    (Family.D, DiagnosisClass.AMOUNT, r"rate|price|overcharg|excess|amount is wrong|wrong amount|discount|billed (extra|more)"),
    # Family B -- administrative blockers.
    (Family.B, DiagnosisClass.ALREADY_PAID_UNRECONCILED, r"already paid|already settled|have paid|was paid|payment (was )?made|utr|neft|rtgs|reconcile"),
    (Family.B, DiagnosisClass.GST_DEFECT, r"gst|gstin|igst|cgst|sgst|hsn|tax invoice"),
    (Family.B, DiagnosisClass.PO_MISMATCH, r"\bpo\b|purchase order|work order|\bwo\b"),
    (Family.B, DiagnosisClass.BANK_DETAIL_MISMATCH, r"account number|account no|bank detail|ifsc|beneficiary|payee"),
    (Family.B, DiagnosisClass.APPROVAL_BOTTLENECK, r"approv|sign.?off|authoris|authoriz|sanction|pending with"),
    (Family.B, DiagnosisClass.DOCUMENT_MISSING, r"challan|delivery note|proof of delivery|\bpod\b|\blr\b|e.?way bill|supporting document|signed copy|annexure"),
    (Family.B, DiagnosisClass.INVOICE_NOT_RECEIVED, r"invoice|\bbill\b|not received|never got|resend|re.?send|portal|our system"),
    # Family A -- instrument faults self-reported by the debtor.
    (Family.A, DiagnosisClass.MANDATE_INVALID, r"mandate|auto.?debit|standing instruction|\bsi\b|\benach\b|revoke|cancel"),
    (Family.A, DiagnosisClass.AUTH_FAILURE, r"\botp\b|authenticat|3d ?secure|\bpin\b|password|verification"),
    (Family.A, DiagnosisClass.LIMIT_EXCEEDED, r"limit"),
    (Family.A, DiagnosisClass.INSTRUMENT_EXPIRED, r"expir|valid till|renew"),
    (Family.A, DiagnosisClass.INSUFFICIENT_FUNDS, r"insufficient|bounce|dishonour|dishonor|\bnsf\b|balance"),
    # Family C -- liquidity and willingness. Last: the vaguest vocabulary.
    (Family.C, DiagnosisClass.REFUSAL, r"not pay|won'?t pay|will not be paid|refuse|stop (messaging|contacting|calling)|do not contact"),
    (Family.C, DiagnosisClass.PROMISE_STATED, r"will (pay|transfer|release|process|clear)|by (month|week) end|tomorrow|next week|in \d+ days|on the \d+"),
    (Family.C, DiagnosisClass.CASHFLOW_SHORTFALL, r"cash ?flow|liquidity|funds|tight|shortage of|need (some )?time|thoda|collection"),
    (Family.C, DiagnosisClass.STALLING, r"check with|get back|look into|will see|noted|on leave|will update|revert"),
]

COMPILED = [(family, class_, re.compile(pattern, re.IGNORECASE)) for family, class_, pattern in RULES]

FALLBACK = (Family.C, DiagnosisClass.STALLING)
"""What to answer when nothing matches. STALLING is the majority-ish class
among replies that carry no specific signal, so this is the baseline's best
guess rather than a deliberate throwaway."""


def classify(text: str) -> tuple[Family, DiagnosisClass]:
    for family, class_, pattern in COMPILED:
        if pattern.search(text):
            return family, class_
    return FALLBACK
