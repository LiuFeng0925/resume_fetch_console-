from __future__ import annotations

import json


def parse_push_response(
    response_body: str | None,
    response_status: int | None = None,
) -> dict:
    """解析候选人导入接口响应为统一结构。"""
    base = {
        "kind": "unknown",
        "http_status": response_status,
    }
    if not response_body or not str(response_body).strip():
        return base

    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        return {**base, "kind": "raw", "raw": str(response_body)[:500]}

    if not isinstance(data, dict):
        return {**base, "kind": "raw", "raw": str(data)[:500]}

    if "total" in data and ("created" in data or "failed" in data):
        failures = data.get("failures") or []
        skipped_items = data.get("skipped_items") or []
        return {
            "kind": "import_result",
            "http_status": response_status,
            "total": _as_int(data.get("total")),
            "created": _as_int(data.get("created")),
            "updated": _as_int(data.get("updated")),
            "skipped": _as_int(data.get("skipped")),
            "failed": _as_int(data.get("failed")),
            "failures": failures if isinstance(failures, list) else [],
            "skipped_items": skipped_items if isinstance(skipped_items, list) else [],
        }

    errors = data.get("errors")
    if errors is not None or data.get("error_code") or data.get("detail"):
        err_list = errors if isinstance(errors, list) else []
        return {
            "kind": "error",
            "http_status": response_status or _as_int(data.get("status")),
            "detail": data.get("detail") or data.get("title") or "",
            "error_code": data.get("error_code") or "",
            "title": data.get("title") or "",
            "request_id": data.get("request_id") or "",
            "errors": err_list,
            "error_count": len(err_list),
        }

    return {**base, "kind": "raw", "raw": data}


def derive_batch_status(
    *,
    http_ok: bool,
    response_status: int | None,
    summary: dict,
) -> str:
    """根据 HTTP 与响应体决定推送批次状态。"""
    if summary.get("kind") == "import_result":
        failed = summary.get("failed") or 0
        succeeded = (summary.get("created") or 0) + (summary.get("updated") or 0)
        if failed > 0 and succeeded > 0:
            return "partial"
        if failed > 0:
            return "failed"
        return "success"
    if http_ok and response_status in (200, 201, 204):
        return "success"
    return "failed"


def format_summary_text(summary: dict) -> str:
    """列表页简短摘要。"""
    if summary.get("kind") == "import_result":
        parts = [
            f"合计{summary.get('total', '—')}",
            f"新建{summary.get('created', 0)}",
            f"更新{summary.get('updated', 0)}",
        ]
        failed = summary.get("failed") or 0
        skipped = summary.get("skipped") or 0
        if failed:
            parts.append(f"失败{failed}")
        if skipped:
            parts.append(f"跳过{skipped}")
        failures = summary.get("failures") or []
        if failures:
            parts.append(f"失败明细{len(failures)}条")
        return " · ".join(parts)
    if summary.get("kind") == "error":
        detail = summary.get("detail") or "请求失败"
        count = summary.get("error_count") or 0
        if count:
            return f"{detail}（{count}项校验错误）"
        return str(detail)
    if summary.get("kind") == "raw":
        raw = summary.get("raw")
        return str(raw)[:120] if raw else "—"
    return "—"


def _candidate_index_from_loc(loc) -> int | None:
    if not isinstance(loc, list):
        return None
    for i, part in enumerate(loc):
        if part == "candidates" and i + 1 < len(loc):
            try:
                return int(loc[i + 1])
            except (TypeError, ValueError):
                return None
    return None


def _failure_index(item: dict) -> int | None:
    if not isinstance(item, dict):
        return None
    for key in ("index", "row", "candidate_index", "position"):
        if key in item and item[key] is not None:
            try:
                return int(item[key])
            except (TypeError, ValueError):
                pass
    loc = item.get("loc")
    idx = _candidate_index_from_loc(loc)
    if idx is not None:
        return idx
    return None


def _failure_reason(item: dict) -> str:
    if not isinstance(item, dict):
        return str(item)
    for key in ("reason", "message", "msg", "error", "detail", "title"):
        val = item.get(key)
        if val not in (None, ""):
            return str(val)
    return json.dumps(item, ensure_ascii=False)


def build_candidate_results(
    request_payload: dict | None,
    response_summary: dict | None,
    *,
    cand_index: dict[tuple[str, ...], str] | None = None,
    lookup_account=None,
    meta_accounts: dict[int, str] | None = None,
) -> list[dict]:
    """把请求里的候选人与接口响应合并为逐人结果列表。"""
    candidates = []
    if isinstance(request_payload, dict):
        raw = request_payload.get("candidates") or []
        if isinstance(raw, list):
            candidates = raw

    summary = response_summary or {}
    kind = summary.get("kind")
    error_by_index: dict[int, list[str]] = {}
    failure_by_index: dict[int, str] = {}
    skipped_by_index: dict[int, str] = {}

    if kind == "error":
        for err in summary.get("errors") or []:
            if not isinstance(err, dict):
                continue
            idx = _candidate_index_from_loc(err.get("loc"))
            if idx is None:
                continue
            error_by_index.setdefault(idx, []).append(str(err.get("msg") or err))
        batch_reason = summary.get("detail") or "请求失败"
    else:
        batch_reason = ""

    if kind == "import_result":
        for item in summary.get("failures") or []:
            idx = _failure_index(item)
            if idx is not None:
                failure_by_index[idx] = _failure_reason(item)
        for item in summary.get("skipped_items") or []:
            idx = _failure_index(item)
            if idx is not None:
                skipped_by_index[idx] = _failure_reason(item)

    batch_failed = kind == "error"
    import_failed = kind == "import_result" and (summary.get("failed") or 0) > 0
    import_ok = kind == "import_result" and (summary.get("failed") or 0) == 0

    results: list[dict] = []
    for i, cand in enumerate(candidates):
        if not isinstance(cand, dict):
            cand = {}
        account_name = ""
        name = str(cand.get("name") or "").strip()
        phone = str(cand.get("phone") or "").strip()
        strong_lookup = ""
        if cand_index and lookup_account and name and phone:
            if ("name_phone", name, phone) in cand_index:
                strong_lookup = lookup_account(cand, cand_index) or ""
        if strong_lookup:
            account_name = strong_lookup
        elif meta_accounts and i in meta_accounts and meta_accounts[i]:
            account_name = meta_accounts[i]
        elif cand_index and lookup_account:
            account_name = lookup_account(cand, cand_index) or ""
        row = {
            "row": i + 1,
            "index": i,
            "name": cand.get("name") or "",
            "account_name": account_name,
            "phone": cand.get("phone") or "",
            "email": cand.get("email") or "",
            "channel_nickname": cand.get("channel_nickname") or "",
            "status": "unknown",
            "status_label": "未知",
            "reason": "",
        }
        if i in error_by_index:
            row["status"] = "failed"
            row["status_label"] = "校验失败"
            row["reason"] = "；".join(error_by_index[i])
        elif i in failure_by_index:
            row["status"] = "failed"
            row["status_label"] = "入库失败"
            row["reason"] = failure_by_index[i]
        elif i in skipped_by_index:
            row["status"] = "skipped"
            row["status_label"] = "跳过"
            row["reason"] = skipped_by_index[i]
        elif import_ok:
            row["status"] = "success"
            row["status_label"] = "成功"
        elif import_failed:
            row["status"] = "success"
            row["status_label"] = "成功"
        elif batch_failed:
            row["status"] = "failed"
            row["status_label"] = "未入库"
            row["reason"] = batch_reason or "整批请求失败"
        results.append(row)

    return results


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
