#!/usr/bin/env python3
"""导出指定时间段手动重推批次中的全部候选人信息到 Excel。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from resume_parser import schema as resume_schema
from src.parse_store import ParseRecordStore
from src.push_accounts import build_candidate_account_index, lookup_account_for_candidate
from src.push_response import build_candidate_results, parse_push_response

EXTRA_HEADERS = ["推送批次ID", "批次创建时间", "重推时间", "批次状态", "推送租户", "邮箱账号", "原始文件名"]


def _match_source_file(candidate: dict, cand_index: dict) -> str:
    parse_store = ParseRecordStore(ROOT / "data" / "processed.db")
    try:
        records = parse_store.list_success_with_parsed()
    finally:
        parse_store.close()
    name = str(candidate.get("name") or "").strip()
    phone = str(candidate.get("phone") or "").strip()
    for rec in records:
        parsed = rec.get("parsed_json") or {}
        if not isinstance(parsed, dict):
            continue
        p_name = str(parsed.get("name") or "").strip()
        p_phone = str(parsed.get("phone") or "").strip()
        if name and phone and p_name == name and p_phone == phone:
            return str(rec.get("source_file") or "")
        if name and phone and p_name == name and (not p_phone or not phone):
            return str(rec.get("source_file") or "")
    account = lookup_account_for_candidate(candidate, cand_index) or ""
    if account:
        for rec in records:
            if rec.get("account_name") == account:
                parsed = rec.get("parsed_json") or {}
                if str(parsed.get("name") or "").strip() == name:
                    return str(rec.get("source_file") or "")
    return ""


def _candidate_row(
    candidate: dict,
    *,
    batch_id: int,
    created_at: str,
    pushed_at: str,
    batch_status: str,
    tenant_code: str,
    cand_index: dict,
) -> list:
    source_file = _match_source_file(candidate, cand_index)
    rec = dict(candidate)
    if not rec.get("tenant_code"):
        rec["tenant_code"] = tenant_code
    account = lookup_account_for_candidate(candidate, cand_index) or ""
    base = resume_schema.to_excel_row(rec, source_file, "重推批次")
    # to_excel_row ends with: 最近活跃, 原始文件名, 解析状态, 解析后json
    # Replace source_file/status in base and prepend meta columns
    base[-3] = source_file or base[-3]
    base[-2] = "重推批次"
    return [
        batch_id,
        created_at,
        pushed_at,
        batch_status,
        tenant_code,
        account,
        source_file,
        *base,
    ]


def export_retry_push_excel(
    *,
    pushed_prefixes: tuple[str, ...] = ("2026-06-15T15:58", "2026-06-15T15:59"),
    output_path: Path | None = None,
) -> Path:
    db_path = ROOT / "data" / "processed.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    clauses = " OR ".join("pushed_at LIKE ?" for _ in pushed_prefixes)
    rows = conn.execute(
        f"""
        SELECT id, status, tenant_code, candidate_count, created_at, pushed_at,
               request_payload, response_body, response_status
        FROM push_batches
        WHERE trigger_type = 'retry' AND ({clauses})
        ORDER BY pushed_at, id
        """,
        [f"{p}%" for p in pushed_prefixes],
    ).fetchall()
    conn.close()

    parse_store = ParseRecordStore(db_path)
    try:
        cand_index = build_candidate_account_index(parse_store.list_success_with_parsed())
    finally:
        parse_store.close()

    headers = EXTRA_HEADERS + resume_schema.excel_headers()
    wb = Workbook()
    ws = wb.active
    ws.title = "重推名单"
    ws.append(headers)

    total = 0
    for row in rows:
        payload_raw = row["request_payload"]
        if not payload_raw:
            continue
        payload = json.loads(payload_raw)
        candidates = payload.get("candidates") or []
        created = (row["created_at"] or "")[:19].replace("T", " ")
        pushed = (row["pushed_at"] or "")[:19].replace("T", " ")
        tenant = str(payload.get("tenant_code") or row["tenant_code"] or "")
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            ws.append(
                _candidate_row(
                    cand,
                    batch_id=int(row["id"]),
                    created_at=created,
                    pushed_at=pushed,
                    batch_status=str(row["status"] or ""),
                    tenant_code=tenant,
                    cand_index=cand_index,
                )
            )
            total += 1

    no_wrap = Alignment(wrap_text=False, vertical="top")
    for row_cells in ws.iter_rows(min_row=2):
        for cell in row_cells:
            cell.alignment = no_wrap

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = ROOT / "data" / f"重推名单_20260615_1558_{ts}.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"导出完成: {output_path} ({total} 人, {len(rows)} 批次)")
    return output_path


if __name__ == "__main__":
    export_retry_push_excel()
