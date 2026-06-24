from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from src.config import load_config
from src.parse_store import ParseRecordStore
from src.push_store import PushRecordStore

_META_KEY = "_candidate_meta"


def build_candidate_account_index(records: list[dict]) -> dict[tuple[str, ...], str]:
    index: dict[tuple[str, ...], str] = {}
    for rec in records:
        account = str(rec.get("account_name") or "").strip()
        parsed = rec.get("parsed_json") or {}
        if not account or not isinstance(parsed, dict):
            continue
        name = str(parsed.get("name") or "").strip()
        phone = str(parsed.get("phone") or "").strip()
        email = str(parsed.get("email") or "").strip()
        if name and phone:
            index[("name_phone", name, phone)] = account
        if name and email:
            index[("name_email", name, email)] = account
        if name:
            index.setdefault(("name", name), account)
        if phone:
            index.setdefault(("phone", phone), account)
        if email:
            index.setdefault(("email", email), account)
    return index


def lookup_account_for_candidate(
    candidate: dict,
    cand_index: dict[tuple[str, ...], str],
) -> str | None:
    name = str(candidate.get("name") or "").strip()
    phone = str(candidate.get("phone") or "").strip()
    email = str(candidate.get("email") or "").strip()
    if name and phone:
        account = cand_index.get(("name_phone", name, phone))
        if account:
            return account
    if name and email:
        account = cand_index.get(("name_email", name, email))
        if account:
            return account
    if name:
        account = cand_index.get(("name", name))
        if account:
            return account
    if phone:
        account = cand_index.get(("phone", phone))
        if account:
            return account
    if email:
        return cand_index.get(("email", email))
    return None


def _resolve_account_from_filename(filename: str, accounts) -> str:
    stem = re.sub(r"\.[^.]+$", "", Path(filename).name).lower()
    for acct in accounts:
        email = acct.imap.username.lower()
        if email and email in stem:
            return acct.name
    return ""


def strip_push_meta(payload: dict) -> dict:
    """发送接口前去掉仅用于展示的元数据。"""
    cleaned = copy.deepcopy(payload)
    cleaned.pop(_META_KEY, None)
    return cleaned


def _parse_records_for_excel(store: ParseRecordStore, excel_path: str | None) -> list[dict]:
    if not excel_path:
        return []
    rows = store._conn.execute(
        """
        SELECT account_name, source_file, parsed_json
        FROM parse_records
        WHERE excel_path = ? AND status LIKE '成功%'
        """,
        (excel_path,),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        parsed = item.get("parsed_json")
        if parsed:
            try:
                item["parsed_json"] = json.loads(parsed)
            except json.JSONDecodeError:
                item["parsed_json"] = None
        out.append(item)
    return out


def _lookup_from_parse_rows(candidate: dict, rows: list[dict]) -> str:
    name = str(candidate.get("name") or "").strip()
    phone = str(candidate.get("phone") or "").strip()
    email = str(candidate.get("email") or "").strip()
    best = ""
    for row in rows:
        account = str(row.get("account_name") or "").strip()
        parsed = row.get("parsed_json") or {}
        if not account or not isinstance(parsed, dict):
            continue
        pn = str(parsed.get("name") or "").strip()
        pp = str(parsed.get("phone") or "").strip()
        pe = str(parsed.get("email") or "").strip()
        if name and phone and pn == name and pp == phone:
            return account
        if name and email and pn == name and pe == email:
            return account
        if name and pn == name and not best:
            best = account
    return best


def resolve_candidate_account(
    candidate: dict,
    *,
    excel_path: str | None,
    excel_rows: list[dict],
    global_index: dict[tuple[str, ...], str],
    config_path: Path | None,
) -> str:
    account = _lookup_from_parse_rows(candidate, excel_rows)
    if account:
        return account
    account = lookup_account_for_candidate(candidate, global_index) or ""
    if account:
        return account
    if config_path:
        cfg = load_config(config_path)
        for row in excel_rows:
            parsed = row.get("parsed_json") or {}
            if not isinstance(parsed, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            phone = str(candidate.get("phone") or "").strip()
            pn = str(parsed.get("name") or "").strip()
            pp = str(parsed.get("phone") or "").strip()
            if name and phone and pn == name and pp == phone:
                source_file = str(row.get("source_file") or "")
                if source_file:
                    account_name = _resolve_account_from_filename(
                        source_file, cfg.accounts
                    )
                    if account_name:
                        return account_name
    return ""


def build_candidate_meta(
    payload: dict,
    *,
    excel_path: str | None = None,
    excel_rows: list[dict] | None = None,
    global_index: dict[tuple[str, ...], str] | None = None,
    config_path: Path | None = None,
    items: list[dict] | None = None,
) -> list[dict]:
    """为请求体中的候选人生成邮箱名称元数据。"""
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        return []
    meta: list[dict] = []
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            cand = {}
        account_name = ""
        source_file = ""
        if items and i < len(items):
            item = items[i]
            account_name = str(item.get("account_name") or "").strip()
            source_file = str(item.get("source_file") or "").strip()
            parsed = item.get("parsed_json") or {}
            if isinstance(parsed, dict):
                item_name = str(parsed.get("name") or "").strip()
                item_phone = str(parsed.get("phone") or "").strip()
                cand_name = str(cand.get("name") or "").strip()
                cand_phone = str(cand.get("phone") or "").strip()
                if (
                    item_name
                    and item_phone
                    and cand_name
                    and cand_phone
                    and (item_name != cand_name or item_phone != cand_phone)
                ):
                    account_name = ""
                    source_file = ""
        if not account_name:
            account_name = resolve_candidate_account(
                cand,
                excel_path=excel_path,
                excel_rows=excel_rows or [],
                global_index=global_index or {},
                config_path=config_path,
            )
        if not source_file and excel_rows:
            name = str(cand.get("name") or "").strip()
            phone = str(cand.get("phone") or "").strip()
            for row in excel_rows:
                parsed = row.get("parsed_json") or {}
                if not isinstance(parsed, dict):
                    continue
                if (
                    str(parsed.get("name") or "").strip() == name
                    and str(parsed.get("phone") or "").strip() == phone
                ):
                    source_file = str(row.get("source_file") or "")
                    break
        meta.append({
            "index": i,
            "account_name": account_name,
            "source_file": source_file,
        })
    return meta


def attach_candidate_meta(payload: dict, meta: list[dict]) -> dict:
    enriched = copy.deepcopy(payload)
    enriched[_META_KEY] = meta
    return enriched


def meta_account_by_index(payload: dict | None) -> dict[int, str]:
    if not isinstance(payload, dict):
        return {}
    raw = payload.get(_META_KEY) or []
    if not isinstance(raw, list):
        return {}
    out: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        out[idx] = str(item.get("account_name") or "").strip()
    return out


def backfill_push_account_meta(
    *,
    db_path: Path,
    config_path: Path | None = None,
) -> dict:
    """为历史推送批次补充 _candidate_meta 中的邮箱名称。"""
    push_store = PushRecordStore(db_path)
    parse_store = ParseRecordStore(db_path)
    try:
        global_index = build_candidate_account_index(
            parse_store.list_success_with_parsed()
        )
        rows = push_store._conn.execute(
            "SELECT id, excel_path, request_payload FROM push_batches ORDER BY id"
        ).fetchall()
        updated = 0
        filled = 0
        total_candidates = 0
        for row in rows:
            batch_id = int(row["id"])
            excel_path = row["excel_path"]
            raw_payload = row["request_payload"]
            if not raw_payload:
                continue
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or not payload.get("candidates"):
                continue
            excel_rows = _parse_records_for_excel(parse_store, excel_path)
            meta = build_candidate_meta(
                payload,
                excel_path=excel_path,
                excel_rows=excel_rows,
                global_index=global_index,
                config_path=config_path,
            )
            total_candidates += len(meta)
            filled += sum(1 for m in meta if m.get("account_name"))
            existing = meta_account_by_index(payload)
            changed = existing != {m["index"]: m["account_name"] for m in meta}
            if changed or _META_KEY not in payload:
                payload = attach_candidate_meta(payload, meta)
                push_store.update_batch_push_meta(batch_id, request_payload=payload)
                updated += 1
        return {
            "batches": len(rows),
            "updated_batches": updated,
            "candidates": total_candidates,
            "filled_accounts": filled,
        }
    finally:
        parse_store.close()
        push_store.close()
