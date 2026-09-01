"""A small, opt-in, local record of past Path B extractions
(reply_text + the ExtractionResult it produced) — the thing
agent.auditor.extractor_drift needs to sample from, and the reason
DEVDOC_v6's own extractor-drift job was "not implemented" until now: it
needs real extractions to re-run, and until this existed there was
nowhere durable holding any.

Deliberately NOT under docs/evidence/ and not git-committed — a local
SQLite file, gitignored the same way events.db/ledger.db already are.
LEDGER.md's own "deliberately not stored" list names exactly this content
("full inbound message bodies beyond the retention window") as something
this project already commits to not exporting; this log exists for the
Auditor's own local use, not as evidence to publish.

agent.diagnose.llm_extract.extract_from_reply() does not write here
automatically — a caller passes an ExtractionLog in (mirroring how
spend_ledger already works as an optional, explicit dependency), so every
existing test and caller that doesn't care about drift sampling is
unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.db import connect
from agent.diagnose.extract import DiagnosisClass, ExtractionResult, Family

DEFAULT_LOG_PATH = Path("extraction_log.db")


@dataclass(frozen=True, slots=True)
class LoggedExtraction:
    id: int
    reply_text: str
    family: Family
    class_: DiagnosisClass
    confidence: float
    model: str
    purpose: str
    recorded_at: str


class ExtractionLog:
    def __init__(self, db_path: str | Path = DEFAULT_LOG_PATH):
        self.db_path = str(db_path)
        self._conn = connect(self.db_path)
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ExtractionLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reply_text TEXT NOT NULL,
                family TEXT NOT NULL,
                class TEXT NOT NULL,
                confidence REAL NOT NULL,
                model TEXT NOT NULL,
                purpose TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            )
            """
        )
        self._conn.commit()

    def record(self, *, reply_text: str, result: ExtractionResult, model: str, purpose: str) -> int:
        """Only ever called on a successfully validated ExtractionResult —
        a call that failed schema validation is logged elsewhere by reason
        and count, never by content (matching LEDGER.md's own convention
        for rejected model outputs)."""
        cursor = self._conn.execute(
            "INSERT INTO extractions (reply_text, family, class, confidence, model, purpose) VALUES (?, ?, ?, ?, ?, ?)",
            (reply_text, result.family.value, result.class_.value, result.confidence, model, purpose),
        )
        self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def all_entries(self) -> list[LoggedExtraction]:
        rows = self._conn.execute(
            "SELECT id, reply_text, family, class, confidence, model, purpose, recorded_at "
            "FROM extractions ORDER BY id ASC"
        ).fetchall()
        return [
            LoggedExtraction(
                id=row[0], reply_text=row[1], family=Family(row[2]), class_=DiagnosisClass(row[3]),
                confidence=row[4], model=row[5], purpose=row[6], recorded_at=row[7],
            )
            for row in rows
        ]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
