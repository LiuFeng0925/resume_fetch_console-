from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from src.config import PushConfig, load_config
from src.parse_store import ParseRecordStore
from src.push_client import post_candidates
from src.push_accounts import (
    attach_candidate_meta,
    build_candidate_account_index,
    build_candidate_meta,
    lookup_account_for_candidate,
)
from src.push_payload import build_import_payload
from src.push_parse_sync import (
    aligned_items_from_batch,
    mark_items_push_pushing,
    sync_push_results_to_parse_records,
)
from src.push_response import derive_batch_status, parse_push_response
from src.push_store import PushRecordStore

logger = logging.getLogger(__name__)


def _group_items_by_tenant(items: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        parsed = item.get("parsed_json")
        status = str(item.get("status") or "")
        if not parsed or not status.startswith("成功"):
            continue
        tenant_code = str(item.get("tenant_code") or "").strip()
        tenant_id = str(item.get("tenant_id") or "").strip()
        if not tenant_code:
            logger.warning(
                "skip push for %s: tenant_code empty",
                item.get("source_file"),
            )
            continue
        groups[(tenant_code, tenant_id)].append(item)
    return groups


def push_parse_batch(
    store: PushRecordStore,
    cfg: PushConfig,
    *,
    excel_path: str | None,
    items: list[dict],
    trigger_type: str = "auto",
    output_dir: Path | None = None,
    parse_store: ParseRecordStore | None = None,
) -> list[dict]:
    """解析批次完成后按租户分组推送，返回各批次结果摘要。"""
    if not cfg.enabled:
        return []
    groups = _group_items_by_tenant(items)
    if not groups:
        logger.info("push skipped: no successful candidates with tenant_code")
        return []

    summaries: list[dict] = []
    for (tenant_code, tenant_id), group_items in groups.items():
        payload, aligned_items = build_import_payload(
            tenant_code=tenant_code,
            items=group_items,
            output_dir=output_dir,
        )
        if not payload["candidates"]:
            continue
        payload = attach_candidate_meta(
            payload,
            build_candidate_meta(payload, items=aligned_items),
        )
        parse_record_ids: list[int] = []
        for x in aligned_items:
            raw = x.get("parse_record_id")
            if raw in (None, ""):
                continue
            try:
                parse_record_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        if parse_store:
            mark_items_push_pushing(parse_store, aligned_items)
        batch_key = f"{excel_path or 'manual'}::{tenant_code}::{len(payload['candidates'])}"
        batch_id = store.create_batch(
            batch_key=batch_key,
            excel_path=excel_path,
            tenant_code=tenant_code,
            tenant_id=tenant_id,
            candidate_count=len(payload["candidates"]),
            parse_record_ids=parse_record_ids,
            status="pushing",
            trigger_type=trigger_type,
            request_payload=payload,
        )
        result = post_candidates(cfg, payload)
        resp_summary = parse_push_response(result.body, result.status_code)
        batch_status = derive_batch_status(
            http_ok=result.ok,
            response_status=result.status_code,
            summary=resp_summary,
        )
        error_message = result.error_message
        if not error_message and batch_status in ("failed", "partial"):
            error_message = resp_summary.get("detail") or None
        store.finish_batch(
            batch_id,
            status=batch_status,
            response_status=result.status_code,
            response_body=result.body,
            error_message=error_message,
        )
        if parse_store:
            sync_push_results_to_parse_records(
                parse_store,
                batch_id=batch_id,
                aligned_items=aligned_items,
                request_payload=payload,
                response_body=result.body,
                response_status=result.status_code,
            )
        summary = {
            "batch_id": batch_id,
            "tenant_code": tenant_code,
            "candidate_count": len(payload["candidates"]),
            "status": batch_status,
            "error": error_message,
            "response_summary": resp_summary,
        }
        summaries.append(summary)
        logger.info(
            "push batch id=%s tenant=%s count=%s status=%s",
            batch_id,
            tenant_code,
            len(payload["candidates"]),
            summary["status"],
        )
    return summaries


def _build_account_tenant_map(config_path: Path) -> dict[str, tuple[str, str]]:
    cfg = load_config(config_path)
    mapping: dict[str, tuple[str, str]] = {}
    for acct in cfg.accounts:
        tenant_code = (acct.tenant_code or "").strip()
        if tenant_code:
            mapping[acct.name] = (tenant_code, (acct.tenant_id or "").strip())
    return mapping


def _default_tenant_from_config(
    account_tenant_map: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    """若所有账号配置为同一租户，无法逐人匹配时回退到该租户。"""
    pairs = list(account_tenant_map.values())
    if not pairs:
        return None
    codes = {p[0] for p in pairs}
    if len(codes) == 1:
        return pairs[0]
    return None


def refresh_payload_tenants(
    payload: dict,
    *,
    account_tenant_map: dict[str, tuple[str, str]],
    cand_index: dict[tuple[str, ...], str],
) -> list[dict]:
    """按当前分配规则刷新请求体中的 tenant_code，并按新租户重新分组。"""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    config_default = _default_tenant_from_config(account_tenant_map)
    fallback_tc = config_default[0] if config_default else str(payload.get("tenant_code") or "").strip()
    fallback_tid = config_default[1] if config_default else ""
    for cand in payload.get("candidates") or []:
        account = lookup_account_for_candidate(cand, cand_index)
        if account and account in account_tenant_map:
            tenant_code, tenant_id = account_tenant_map[account]
        else:
            tenant_code, tenant_id = fallback_tc, fallback_tid
        groups[(tenant_code, tenant_id)].append(cand)
    payloads: list[dict] = []
    for (tenant_code, tenant_id), candidates in groups.items():
        if not tenant_code or not candidates:
            continue
        payloads.append({"tenant_code": tenant_code, "candidates": candidates})
    return payloads or [payload]


def retry_push_batches(
    store: PushRecordStore,
    cfg: PushConfig,
    batch_ids: list[int],
    *,
    refresh_tenant: bool = True,
    config_path: Path | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """重推失败/部分成功批次；默认按最新分配规则刷新 tenant_code。"""
    if not cfg.enabled:
        raise ValueError("推送未启用")

    account_tenant_map: dict[str, tuple[str, str]] = {}
    cand_index: dict[tuple[str, ...], str] = {}
    if refresh_tenant and config_path and db_path:
        account_tenant_map = _build_account_tenant_map(config_path)
        parse_store = ParseRecordStore(db_path)
        try:
            cand_index = build_candidate_account_index(parse_store.list_success_with_parsed())
        finally:
            parse_store.close()

    batches = store.get_batches(batch_ids)
    results: list[dict] = []
    for batch in batches:
        if batch.get("status") not in ("failed", "partial"):
            results.append({
                "batch_id": batch["id"],
                "status": "skipped",
                "error": "仅失败或部分成功记录可重推",
            })
            continue
        payload = batch.get("request_payload")
        if not payload or not payload.get("candidates"):
            results.append({
                "batch_id": batch["id"],
                "status": "failed",
                "error": "缺少可重推的数据",
            })
            continue

        payloads_to_send = (
            refresh_payload_tenants(
                payload,
                account_tenant_map=account_tenant_map,
                cand_index=cand_index,
            )
            if refresh_tenant and account_tenant_map
            else [payload]
        )
        store.mark_retrying(batch["id"])

        last_result = None
        last_summary: dict = {}
        last_status = "failed"
        last_error = None
        last_http_status = None
        last_body = ""
        refreshed_payload = payloads_to_send[0]

        for send_payload in payloads_to_send:
            result = post_candidates(cfg, send_payload)
            resp_summary = parse_push_response(result.body, result.status_code)
            batch_status = derive_batch_status(
                http_ok=result.ok,
                response_status=result.status_code,
                summary=resp_summary,
            )
            error_message = result.error_message
            if not error_message and batch_status in ("failed", "partial"):
                error_message = resp_summary.get("detail") or None

            last_result = result
            last_summary = resp_summary
            last_status = batch_status
            last_error = error_message
            last_http_status = result.status_code
            last_body = result.body
            refreshed_payload = send_payload

            if batch_status != "success":
                break

        tenant_code = str(refreshed_payload.get("tenant_code") or batch.get("tenant_code") or "")
        tenant_id = ""
        if refresh_tenant and account_tenant_map:
            for account, pair in account_tenant_map.items():
                if pair[0] == tenant_code:
                    tenant_id = pair[1]
                    break

        store.update_batch_push_meta(
            batch["id"],
            tenant_code=tenant_code,
            tenant_id=tenant_id,
            request_payload=refreshed_payload,
        )
        store.finish_batch(
            batch["id"],
            status=last_status,
            response_status=last_http_status,
            response_body=last_body,
            error_message=last_error,
        )
        if db_path:
            retry_parse_store = ParseRecordStore(db_path)
            try:
                aligned = aligned_items_from_batch(
                    retry_parse_store,
                    parse_record_ids=batch.get("parse_record_ids") or [],
                    request_payload=refreshed_payload,
                    excel_path=batch.get("excel_path"),
                )
                mark_items_push_pushing(retry_parse_store, aligned)
                sync_push_results_to_parse_records(
                    retry_parse_store,
                    batch_id=batch["id"],
                    aligned_items=aligned,
                    request_payload=refreshed_payload,
                    response_body=last_body,
                    response_status=last_http_status,
                )
            finally:
                retry_parse_store.close()
        results.append({
            "batch_id": batch["id"],
            "status": last_status,
            "error": last_error,
            "response_summary": last_summary,
            "tenant_code": tenant_code,
        })
    return results
