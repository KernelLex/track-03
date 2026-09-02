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

It has one structural weakness, which is the real argument for the model:
it matches surface forms. "my card expired" it gets; "the card you have on
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
    (Family.D, DiagnosisClass.NOT_OUR_DEBT, r"wrong number|don'?t know this|do not know this|not us\b|sister concern|never dealt"),
    (Family.D, DiagnosisClass.CONTRACT, r"as per clause|per our agreement|contract was terminated|terms of the contract|not billable"),
    (Family.D, DiagnosisClass.QUANTITY_QUALITY, r"damaged|short supply|shortage|missing from|only \d+ units|units but|defective|quality"),
    (Family.D, DiagnosisClass.AMOUNT, r"rate agreed|agreed on a|amount is wrong|overcharg|billed \d+|discount|delivery charge"),
    # Family B -- administrative blockers.
    (Family.B, DiagnosisClass.ALREADY_PAID_UNRECONCILED, r"already paid|already settled|have paid|was paid|utr\b|reconcile"),
    (Family.B, DiagnosisClass.GST_DEFECT, r"gst|gstin|igst|cgst|sgst|tax invoice"),
    (Family.B, DiagnosisClass.PO_MISMATCH, r"\bpo\b|purchase order|po number|po-"),
    (Family.B, DiagnosisClass.BANK_DETAIL_MISMATCH, r"account number|bank details|ifsc|beneficiary"),
    (Family.B, DiagnosisClass.APPROVAL_BOTTLENECK, r"approval|approve|pending with|sign off|sign-off|director"),
    (Family.B, DiagnosisClass.DOCUMENT_MISSING, r"challan|delivery note|proof of delivery|\bpod\b|supporting document|signed copy"),
    (Family.B, DiagnosisClass.INVOICE_NOT_RECEIVED, r"never got|not received|resend|send it to|not in our system|upload it again|not showing"),
    # Family A -- instrument faults self-reported by the debtor.
    (Family.A, DiagnosisClass.MANDATE_INVALID, r"cancelled.*mandate|mandate.*cancel|revoked|stopped the auto"),
    (Family.A, DiagnosisClass.AUTH_FAILURE, r"\botp\b|authenticat|3d secure|password"),
    (Family.A, DiagnosisClass.LIMIT_EXCEEDED, r"per day limit|daily limit|limit is|above that limit|transaction limit"),
    (Family.A, DiagnosisClass.INSTRUMENT_EXPIRED, r"expired|expiry|card is old"),
    (Family.A, DiagnosisClass.INSUFFICIENT_FUNDS, r"insufficient|bounced|balance nahi|not enough|didn'?t have enough|did not have enough|low balance"),
    # Family C -- liquidity and willingness. Last: the vaguest vocabulary.
    (Family.C, DiagnosisClass.REFUSAL, r"not paying|will not be paid|won'?t be paid|stop messaging|do not contact|refuse"),
    (Family.C, DiagnosisClass.PROMISE_STATED, r"will transfer|will pay|will be released|will release|by month end|tomorrow|in \d+ days|on the \d+"),
    (Family.C, DiagnosisClass.CASHFLOW_SHORTFALL, r"cash ?flow|funds are tight|tight|give some time|thoda time|account is empty|collection is (very )?bad|not paid us"),
    (Family.C, DiagnosisClass.STALLING, r"check with|get back to you|look into it|will see|noted|on leave|will update"),
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
