from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.push_response import parse_push_response


class PushRecordStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS push_batches (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_key         TEXT NOT NULL,
                excel_path        TEXT,
                tenant_code       TEXT,
                tenant_id         TEXT,
                candidate_count   INTEGER NOT NULL DEFAULT 0,
                parse_record_ids  TEXT,
                status            TEXT NOT NULL,
                trigger_type      TEXT NOT NULL DEFAULT 'auto',
                attempt_count     INTEGER NOT NULL DEFAULT 1,
                request_payload   TEXT,
                response_status   INTEGER,
                response_body     TEXT,
                error_message     TEXT,
                created_at        TEXT NOT NULL,
                pushed_at         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_push_batches_status
                ON push_batches(status);
            CREATE INDEX IF NOT EXISTS idx_push_batches_created
                ON push_batches(created_at DESC);
            """
        )
        self._conn.commit()

    def create_batch(
        self,
        *,
        batch_key: str,
        excel_path: str | None,
        tenant_code: str,
        tenant_id: str,
        candidate_count: int,
        parse_record_ids: list[int],
        status: str,
        trigger_type: str,
        request_payload: dict,
    ) -> int:
        now = datetime.now().astimezone().isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO push_batches
            (batch_key, excel_path, tenant_code, tenant_id, candidate_count,
             parse_record_ids, status, trigger_type, attempt_count,
             request_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                batch_key,
                excel_path,
                tenant_code,
                tenant_id,
                candidate_count,
                json.dumps(parse_record_ids, ensure_ascii=False),
                status,
                trigger_type,
                json.dumps(request_payload, ensure_ascii=False),
                now,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def finish_batch(
        self,
        batch_id: int,
        *,
        status: str,
        response_status: int | None,
        response_body: str | None,
        error_message: str | None,
    ) -> None:
        now = datetime.now().astimezone().isoformat()
        self._conn.execute(
            """
            UPDATE push_batches
            SET status = ?, response_status = ?, response_body = ?,
                error_message = ?, pushed_at = ?, attempt_count = attempt_count
            WHERE id = ?
            """,
            (status, response_status, response_body, error_message, now, batch_id),
        )
        self._conn.commit()

    def update_batch_push_meta(
        self,
        batch_id: int,
        *,
        tenant_code: str | None = None,
        tenant_id: str | None = None,
        request_payload: dict | None = None,
    ) -> None:
        fields: list[str] = []
        params: list = []
        if tenant_code is not None:
            fields.append("tenant_code = ?")
            params.append(tenant_code)
        if tenant_id is not None:
            fields.append("tenant_id = ?")
            params.append(tenant_id)
        if request_payload is not None:
            fields.append("request_payload = ?")
            params.append(json.dumps(request_payload, ensure_ascii=False))
        if not fields:
            return
        params.append(batch_id)
        self._conn.execute(
            f"UPDATE push_batches SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        self._conn.commit()

    def list_failed_batch_ids(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT id FROM push_batches WHERE status IN ('failed', 'partial') ORDER BY id"
        ).fetchall()
        return [int(r[0]) for r in rows]

    def mark_retrying(self, batch_id: int) -> None:
        self._conn.execute(
            """
            UPDATE push_batches
            SET status = 'pushing', attempt_count = attempt_count + 1,
                trigger_type = 'retry', error_message = NULL
            WHERE id = ?
            """,
            (batch_id,),
        )
        self._conn.commit()

    def get_batch(self, batch_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM push_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def get_batches(self, batch_ids: list[int]) -> list[dict]:
        if not batch_ids:
            return []
        placeholders = ",".join("?" * len(batch_ids))
        rows = self._conn.execute(
            f"SELECT * FROM push_batches WHERE id IN ({placeholders})",
            batch_ids,
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_batches(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[dict], int]:
        from src.date_filter import build_date_range

        where: list[str] = []
        params: list = []
        if status == "success":
            where.append("status = 'success'")
        elif status == "failed":
            where.append("status = 'failed'")
        elif status == "pushing":
            where.append("status = 'pushing'")
        elif status == "partial":
            where.append("status = 'partial'")
        start, end_excl = build_date_range(date_from, date_to)
        if start:
            where.append(
                "COALESCE(NULLIF(pushed_at, ''), created_at) >= ? "
                "AND COALESCE(NULLIF(pushed_at, ''), created_at) < ?"
            )
            params.extend([start, end_excl])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM push_batches{where_sql}",
            params,
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""
            SELECT id, batch_key, excel_path, tenant_code, tenant_id,
                   candidate_count, status, trigger_type, attempt_count,
                   response_status, response_body, error_message, created_at, pushed_at
            FROM push_batches{where_sql}
            ORDER BY COALESCE(NULLIF(pushed_at, ''), created_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows], int(total)

    def get_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM push_batches").fetchone()[0]
        ok = self._conn.execute(
            "SELECT COUNT(*) FROM push_batches WHERE status = 'success'"
        ).fetchone()[0]
        failed = self._conn.execute(
            "SELECT COUNT(*) FROM push_batches WHERE status = 'failed'"
        ).fetchone()[0]
        partial = self._conn.execute(
            "SELECT COUNT(*) FROM push_batches WHERE status = 'partial'"
        ).fetchone()[0]
        return {
            "total": int(total),
            "success": int(ok),
            "failed": int(failed),
            "partial": int(partial),
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in ("parse_record_ids", "request_payload"):
            raw = data.get(key)
            if raw:
                try:
                    data[key] = json.loads(raw)
                except json.JSONDecodeError:
                    data[key] = None
        body = data.get("response_body")
        if body:
            data["response_summary"] = parse_push_response(
                body, data.get("response_status")
            )
        return data

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        self._conn.close()
