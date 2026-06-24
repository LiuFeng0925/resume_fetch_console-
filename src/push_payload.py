from __future__ import annotations

import copy
import json
from pathlib import Path

from resume_parser import schema as resume_schema

# 与 resume_parser.schema 对齐，并补充接口工作经历中的 skills 子字段
_SCALAR_INT_KEYS = frozenset({"birth_year", "work_years"})
_JOB_INTENT_INT_KEYS = frozenset({"expected_salary_min", "expected_salary_max"})

_ARRAY_SUBKEYS: dict[str, tuple[str, ...]] = {
    key: tuple(subs)
    for key, _, subs in resume_schema.ARRAY_FIELDS
}
# 接口文档中 work_experiences 项含 skills，schema 拍平未列但解析 JSON 可能有
_ARRAY_SUBKEYS["work_experiences"] = _ARRAY_SUBKEYS["work_experiences"] + ("skills",)

_ITEM_META_KEYS = ("tenant_id", "tenant_code", "account_name")


def _find_json_archive(output_dir: Path, source_file: str) -> Path | None:
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


def _load_parsed_from_archive(output_dir: Path | None, source_file: str) -> dict | None:
    if not output_dir or not source_file:
        return None
    path = _find_json_archive(output_dir, source_file)
    if not path:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _merge_item_meta(parsed: dict, item: dict) -> dict:
    """把解析记录行上的分配字段合并进 JSON，与 Excel 导出保持一致。"""
    data = resume_schema.migrate_job_id_to_display(copy.deepcopy(parsed))
    job_val = resume_schema.job_display_id_value({**data, **item})
    if job_val and not data.get("job_display_id"):
        data["job_display_id"] = job_val
    for key in _ITEM_META_KEYS:
        item_val = item.get(key)
        if item_val in (None, ""):
            continue
        if not data.get(key):
            data[key] = item_val
    return data


def _resolve_parsed_item(item: dict, *, output_dir: Path | None = None) -> dict | None:
    archived = _load_parsed_from_archive(output_dir, str(item.get("source_file") or ""))
    parsed = archived or item.get("parsed_json")
    if not isinstance(parsed, dict):
        return None
    return _merge_item_meta(parsed, item)


def _scalar_value(key: str, value):
    """标量字段与 Excel JSON 对齐：空字符串保留为 \"\"，不转成 null。"""
    if key in _SCALAR_INT_KEYS:
        if value in (None, ""):
            return "" if value == "" else None
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return value


def _pick_dict(item: dict, keys: tuple[str, ...]) -> dict:
    return {k: item.get(k) for k in keys}


def candidate_from_parsed(parsed: dict, *, source_file: str) -> dict:
    """将解析 JSON 转为接口 candidates 单项（字段与 Excel JSON 对齐）。"""
    data = resume_schema.migrate_job_id_to_display(copy.deepcopy(parsed))
    nickname = data.get("channel_nickname") or resume_schema.parse_channel_nickname(source_file)
    if nickname:
        data["channel_nickname"] = nickname
    data = resume_schema.apply_name_fallback(data, source_file)
    if not data.get("top_edu_school"):
        data["top_edu_school"] = resume_schema.derive_top_edu_school(data)
    if not data.get("top_edu_major"):
        data["top_edu_major"] = resume_schema.derive_top_edu_major(data)
    if not data.get("edu_note"):
        data["edu_note"] = resume_schema.derive_edu_note(data)

    candidate: dict = {}
    for key, _ in resume_schema.SCALAR_FIELDS:
        candidate[key] = _scalar_value(key, data.get(key))

    for key, _, _ in resume_schema.ARRAY_FIELDS:
        subkeys = _ARRAY_SUBKEYS[key]
        if key == "job_intentions":
            rows = []
            for item in data.get(key) or []:
                if not isinstance(item, dict):
                    continue
                row = _pick_dict(item, subkeys)
                for int_key in _JOB_INTENT_INT_KEYS:
                    if int_key in row:
                        row[int_key] = _scalar_value(int_key, row[int_key])
                rows.append(row)
            candidate[key] = rows
        else:
            candidate[key] = [
                _pick_dict(item, subkeys)
                for item in (data.get(key) or [])
                if isinstance(item, dict)
            ]

    for key, _ in resume_schema.STRING_LIST_FIELDS:
        candidate[key] = [
            s for s in (data.get(key) or []) if s not in (None, "")
        ]
    return candidate


def build_import_payload(
    *,
    tenant_code: str,
    items: list[dict],
    output_dir: Path | None = None,
) -> tuple[dict, list[dict]]:
    """构建推送请求体，并返回与 candidates 一一对应的来源 items。"""
    candidates: list[dict] = []
    aligned_items: list[dict] = []
    for item in items:
        parsed = _resolve_parsed_item(item, output_dir=output_dir)
        if not parsed:
            continue
        candidates.append(
            candidate_from_parsed(parsed, source_file=item.get("source_file", ""))
        )
        aligned_items.append(item)
    return {"tenant_code": tenant_code, "candidates": candidates}, aligned_items
