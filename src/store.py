from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class ProcessedMailStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                account_name TEXT NOT NULL,
                message_id   TEXT NOT NULL,
                subject      TEXT,
                mail_date    TEXT,
                processed_at TEXT NOT NULL,
                saved_files  TEXT NOT NULL,
                matched      INTEGER NOT NULL,
                PRIMARY KEY (account_name, message_id)
            );
            CREATE TABLE IF NOT EXISTS watermarks (
                account_name TEXT PRIMARY KEY,
                since_uid    INTEGER NOT NULL
            );
            """
        )
        self._conn.commit()

    def is_processed(self, account_name: str, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE account_name = ? AND message_id = ?",
            (account_name, message_id),
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        account_name: str,
        message_id: str,
        subject: str,
        mail_date: datetime,
        saved_files: list[str],
        matched: bool,
    ) -> None:
        processed_at = datetime.now().astimezone().isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_emails
            (account_name, message_id, subject, mail_date, processed_at, saved_files, matched)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_name,
                message_id,
                subject,
                mail_date.astimezone().isoformat(),
                processed_at,
                json.dumps(saved_files, ensure_ascii=False),
                1 if matched else 0,
            ),
        )
        self._conn.commit()

    def get_watermark_uid(self, account_name: str) -> int | None:
        row = self._conn.execute(
            "SELECT since_uid FROM watermarks WHERE account_name = ?",
            (account_name,),
        ).fetchone()
        return int(row["since_uid"]) if row else None

    def set_watermark_uid(self, account_name: str, since_uid: int) -> None:
        self._conn.execute(
            """
            INSERT INTO watermarks (account_name, since_uid) VALUES (?, ?)
            ON CONFLICT(account_name) DO UPDATE SET since_uid = excluded.since_uid
            """,
            (account_name, since_uid),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
