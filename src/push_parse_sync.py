from __future__ import annotations

import logging
from datetime import datetime

from src.parse_store import ParseRecordStore
from src.push_response import build_candidate_results, parse_push_response

logger = logging.getLogger(__name__)


def _parse_record_ids(items: list[dict]) -> list[int]:
    ids: list[int] = []
    for item in items:
        raw = item.get("parse_record_id") or item.get("id")
        if raw in (None, ""):
            continue
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def sync_push_results_to_parse_records(
    parse_store: ParseRecordStore,
    *,
    batch_id: int,
    aligned_items: list[dict],
    request_payload: dict,
    response_body: str | None,
    response_status: int | None,
    pushed_at: str | None = None,
) -> int:
    """推送完成后，按 candidates index 与 aligned_items 对齐，反写解析记录推送状态。"""
    if not aligned_items:
        return 0
    ts = pushed_at or datetime.now().astimezone().isoformat()
    summary = parse_push_response(response_body, response_status)
    rows = build_candidate_results(request_payload, summary)
    updated = 0
    for i, row in enumerate(rows):
        record_id = None
        if i < len(aligned_items):
            raw = aligned_items[i].get("parse_record_id") or aligned_items[i].get("id")
            if raw not in (None, ""):
                try:
                    record_id = int(raw)
                except (TypeError, ValueError):
                    record_id = None
        if not record_id:
            continue
        status = str(row.get("status") or "unknown")
        if status == "unknown" and summary.get("kind") == "import_result":
            status = "success"
        parse_store.update_push_result(
            record_id,
            push_status=status,
            push_error=str(row.get("reason") or ""),
            push_batch_id=batch_id,
            pushed_at=ts,
        )
        updated += 1
    logger.info(
        "parse records push sync: batch_id=%s updated=%s",
        batch_id,
        updated,
    )
    return updated


def aligned_items_from_batch(
    parse_store: ParseRecordStore,
    *,
    parse_record_ids: list[int],
    request_payload: dict | None,
    excel_path: str | None,
) -> list[dict]:
    """重推时从批次保存的 parse_record_ids 还原 aligned_items。"""
    payload = request_payload or {}
    candidates = payload.get("candidates") or []
    if parse_record_ids and len(parse_record_ids) == len(candidates):
        items: list[dict] = []
        for rid in parse_record_ids:
            rec = parse_store.get_record(int(rid))
            if not rec:
                items.append({"parse_record_id": int(rid)})
            else:
                items.append({
                    "parse_record_id": rec["id"],
                    "id": rec["id"],
                    "source_file": rec.get("source_file") or "",
                    "file_path": rec.get("file_path") or "",
                })
        return items
    if not excel_path:
        return []
    rows = parse_store.list_by_excel_path(excel_path)
    if len(rows) == len(candidates):
        return [
            {
                "parse_record_id": r["id"],
                "id": r["id"],
                "source_file": r.get("source_file") or "",
                "file_path": r.get("file_path") or "",
            }
            for r in rows
        ]
    return []


def mark_items_push_pushing(parse_store: ParseRecordStore, items: list[dict]) -> None:
    ids = _parse_record_ids(items)
    if ids:
        parse_store.mark_push_pushing(ids)
