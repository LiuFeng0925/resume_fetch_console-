from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class ParseRecordStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS parse_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file   TEXT NOT NULL,
                file_path     TEXT NOT NULL,
                file_mtime    REAL NOT NULL,
                account_name  TEXT,
                job_id        TEXT,
                tenant_id     TEXT,
                tenant_code   TEXT,
                status        TEXT NOT NULL,
                parsed_json   TEXT,
                excel_path    TEXT,
                used_ocr      INTEGER NOT NULL DEFAULT 0,
                parsed_at     TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_parse_file_path
                ON parse_records(file_path);
            """
        )
        # 兼容旧表：如果 tenant_id 列不存在则补充
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(parse_records)").fetchall()]
        if "tenant_id" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN tenant_id TEXT DEFAULT ''")
        if "tenant_code" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN tenant_code TEXT DEFAULT ''")
        if "push_status" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN push_status TEXT DEFAULT ''")
        if "pushed_at" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN pushed_at TEXT DEFAULT ''")
        if "push_error" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN push_error TEXT DEFAULT ''")
        if "push_batch_id" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN push_batch_id INTEGER")
        if "candidate_name" not in cols:
            self._conn.execute("ALTER TABLE parse_records ADD COLUMN candidate_name TEXT DEFAULT ''")
            self._conn.execute(
                """
                UPDATE parse_records
                SET candidate_name = COALESCE(json_extract(parsed_json, '$.name'), '')
                WHERE parsed_json IS NOT NULL AND parsed_json != ''
                """
            )
        self._conn.commit()

    @staticmethod
    def _extract_candidate_name(parsed_json: dict | None) -> str:
        if not parsed_json:
            return ""
        return str(parsed_json.get("name") or "").strip()

    def _find_json_archive(self, output_dir: Path, source_file: str) -> Path | None:
        json_dir = output_dir / "json"
        if not json_dir.is_dir():
            return None
        stem = Path(source_file).stem
        exact = json_dir / f"{stem}.json"
        if exact.exists():
            return exact
        for candidate in json_dir.glob(f"{stem}(*).json"):
            if candidate.exists():
                return candidate
        return None

    def should_skip(
        self,
        file_path: str,
        file_mtime: float,
        source_file: str,
        output_dir: Path | None = None,
    ) -> bool:
        """已处理过的文件跳过：DB 记录或历史 JSON 留档均视为已解析。"""
        row = self._conn.execute(
            "SELECT file_mtime FROM parse_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row and float(row["file_mtime"]) == file_mtime:
            return True

        row = self._conn.execute(
            "SELECT id FROM parse_records WHERE source_file = ?",
            (source_file,),
        ).fetchone()
        if row:
            self._conn.execute(
                """
                UPDATE parse_records
                SET file_path = ?, file_mtime = ?
                WHERE source_file = ?
                """,
                (file_path, file_mtime, source_file),
            )
            self._conn.commit()
            return True

        if output_dir:
            json_path = self._find_json_archive(output_dir, source_file)
            if json_path:
                parsed_json = None
                try:
                    parsed_json = json.loads(json_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
                self.upsert_record(
                    source_file=source_file,
                    file_path=file_path,
                    file_mtime=file_mtime,
                    account_name="",
                    job_display_id=str(
                        (parsed_json or {}).get("job_display_id")
                        or (parsed_json or {}).get("job_id")
                        or ""
                    ),
                    tenant_id=str((parsed_json or {}).get("tenant_id") or ""),
                    tenant_code=str((parsed_json or {}).get("tenant_code") or ""),
                    status="成功",
                    parsed_json=parsed_json,
                    excel_path=None,
                    used_ocr=False,
                )
                return True
        return False

    def upsert_record(
        self,
        *,
        source_file: str,
        file_path: str,
        file_mtime: float,
        account_name: str,
        job_display_id: str,
        tenant_id: str = "",
        tenant_code: str = "",
        status: str,
        parsed_json: dict | None,
        excel_path: str | None,
        used_ocr: bool,
        reset_push_pending: bool = False,
    ) -> int:
        parsed_at = datetime.now().astimezone().isoformat()
        payload = json.dumps(parsed_json, ensure_ascii=False) if parsed_json else None
        candidate_name = self._extract_candidate_name(parsed_json)
        push_pending = reset_push_pending and str(status).startswith("成功")
        self._conn.execute(
            """
            INSERT INTO parse_records
            (source_file, file_path, file_mtime, account_name, job_id, tenant_id, tenant_code, status,
             parsed_json, excel_path, used_ocr, parsed_at, push_status, push_error, push_batch_id, pushed_at,
             candidate_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, '', ?)
            ON CONFLICT(file_path) DO UPDATE SET
                source_file = excluded.source_file,
                file_mtime = excluded.file_mtime,
                account_name = excluded.account_name,
                job_id = excluded.job_id,
                tenant_id = excluded.tenant_id,
                tenant_code = excluded.tenant_code,
                status = excluded.status,
                parsed_json = excluded.parsed_json,
                excel_path = excluded.excel_path,
                used_ocr = excluded.used_ocr,
                parsed_at = excluded.parsed_at,
                candidate_name = excluded.candidate_name
            """,
            (
                source_file,
                file_path,
                file_mtime,
                account_name,
                job_display_id,
                tenant_id,
                tenant_code,
                status,
                payload,
                excel_path,
                1 if used_ocr else 0,
                parsed_at,
                "pending" if push_pending else "",
                candidate_name,
            ),
        )
        if push_pending:
            self._conn.execute(
                """
                UPDATE parse_records
                SET push_status = 'pending', push_error = '', push_batch_id = NULL, pushed_at = ''
                WHERE file_path = ?
                """,
                (file_path,),
            )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM parse_records WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        return int(row["id"]) if row else 0

    def mark_push_pushing(self, record_ids: list[int]) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" * len(record_ids))
        self._conn.execute(
            f"""
            UPDATE parse_records
            SET push_status = 'pushing', push_error = ''
            WHERE id IN ({placeholders})
            """,
            record_ids,
        )
        self._conn.commit()

    def update_push_result(
        self,
        record_id: int,
        *,
        push_status: str,
        push_error: str = "",
        push_batch_id: int | None = None,
        pushed_at: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE parse_records
            SET push_status = ?, push_error = ?, push_batch_id = ?, pushed_at = ?
            WHERE id = ?
            """,
            (push_status, push_error or "", push_batch_id, pushed_at, record_id),
        )
        self._conn.commit()

    def list_by_excel_path(self, excel_path: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT * FROM parse_records
            WHERE excel_path = ? AND status LIKE '成功%'
            ORDER BY id ASC
            """,
            (excel_path,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_record(self, record_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM parse_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def delete_record(self, record_id: int) -> bool:
        cur = self._conn.execute(
            "DELETE FROM parse_records WHERE id = ?",
            (record_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def list_records(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        keyword: str | None = None,
        candidate_name: str | None = None,
        push_status: str | None = None,
        job_display_id: str | None = None,
        job_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> tuple[list[dict], int]:
        from src.date_filter import append_date_where

        where: list[str] = []
        params: list = []
        if status == "ok":
            where.append("status LIKE '成功%'")
        elif status == "fail":
            where.append("status NOT LIKE '成功%'")
        filter_job = job_display_id or job_id
        if filter_job:
            where.append("job_id = ?")
            params.append(filter_job)
        if keyword:
            where.append("(source_file LIKE ? OR account_name LIKE ? OR job_id LIKE ?)")
            params.extend([f"%{keyword}%"] * 3)
        if candidate_name:
            where.append("candidate_name LIKE ?")
            params.append(f"%{candidate_name}%")
        if push_status == "none":
            where.append("(push_status IS NULL OR push_status = '')")
        elif push_status:
            where.append("push_status = ?")
            params.append(push_status)
        append_date_where(where, params, "parsed_at", date_from, date_to)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM parse_records{where_sql}",
            params,
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""
            SELECT id, source_file, file_path, account_name, job_id, status,
                   excel_path, used_ocr, parsed_at, push_status, pushed_at,
                   push_error, push_batch_id, candidate_name, parsed_json
            FROM parse_records{where_sql}
            ORDER BY parsed_at DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return [self._row_to_dict(r) for r in rows], int(total)

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        if "job_id" in data:
            data["job_display_id"] = data.get("job_id") or ""
        parsed_json = data.get("parsed_json")
        if parsed_json:
            try:
                data["parsed_json"] = json.loads(parsed_json)
            except json.JSONDecodeError:
                data["parsed_json"] = None
        data["used_ocr"] = bool(data.get("used_ocr"))
        return data

    @property
    def db_path(self) -> Path:
        return self._db_path

    def list_success_with_parsed(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT account_name, tenant_code, tenant_id, parsed_json
            FROM parse_records
            WHERE status LIKE '成功%' AND parsed_json IS NOT NULL AND parsed_json != ''
            """
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM parse_records").fetchone()[0]
        ok_cnt = self._conn.execute(
            "SELECT COUNT(*) FROM parse_records WHERE status LIKE '成功%'"
        ).fetchone()[0]
        return {"total": int(total), "ok": int(ok_cnt), "failed": int(total) - int(ok_cnt)}

    def list_unpushed_success(self, *, limit: int = 10) -> list[dict]:
        """已成功解析但尚未写入 Excel / 推送的记录（用于任务恢复）。"""
        rows = self._conn.execute(
            """
            SELECT * FROM parse_records
            WHERE status LIKE '成功%'
              AND parsed_json IS NOT NULL AND parsed_json != ''
              AND (excel_path IS NULL OR excel_path = '')
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
