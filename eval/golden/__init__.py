"""A pre-registered golden set for Path B extraction accuracy.

`replies.jsonl` holds 50 labelled debtor replies; `baseline.py` is the
keyword classifier the model has to beat; `score.py` runs both and writes
`docs/evidence/EXTRACTION_ACCURACY.md`.

The labels are committed before the extractor is ever run against them, and
`score.py` refuses to score if that ordering cannot be established from git.
"""
