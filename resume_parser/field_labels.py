"""候选人 JSON 字段的中文展示标签，供 Web 详情页结构化渲染。"""

from __future__ import annotations

from resume_parser.schema import ARRAY_FIELDS, SCALAR_FIELDS, STRING_LIST_FIELDS, _GENDER_DISPLAY

SCALAR_LABELS = {key: label for key, label in SCALAR_FIELDS}

ARRAY_SECTION_LABELS = {key: label for key, label, _ in ARRAY_FIELDS}

ARRAY_ITEM_LABELS: dict[str, dict[str, str]] = {
    "work_experiences": {
        "company": "公司",
        "industry": "行业",
        "position": "职位",
        "start_date": "开始时间",
        "end_date": "结束时间",
        "description": "工作描述",
        "skills": "技能",
    },
    "education_history": {
        "school": "学校",
        "school_type": "学校类型",
        "major": "专业",
        "degree": "学历",
        "start_date": "开始时间",
        "end_date": "结束时间",
    },
    "project_experiences": {
        "name": "项目名称",
        "role": "角色",
        "start_date": "开始时间",
        "end_date": "结束时间",
        "description": "项目描述",
        "responsibility": "职责",
    },
    "internship_experiences": {
        "company": "公司",
        "position": "职位",
        "start_date": "开始时间",
        "end_date": "结束时间",
        "description": "实习描述",
    },
    "language_abilities": {
        "language": "语言",
        "reading_writing": "读写",
        "listening_speaking": "听说",
    },
    "job_intentions": {
        "job_status": "求职状态",
        "expected_positions": "期望职位",
        "expected_industries": "期望行业",
        "expected_cities": "期望城市",
        "expected_salary_min": "期望薪资下限",
        "expected_salary_max": "期望薪资上限",
    },
}

STRING_LIST_LABELS = {key: label for key, label in STRING_LIST_FIELDS}

META_LABELS = {
    "_source_file": "原始文件名",
    "job_display_id": "岗位编号",
    "tenant_id": "租户ID",
    "tenant_code": "租户名",
    "channel_nickname": "渠道昵称",
}


def _fmt_scalar(key: str, value) -> str:
    if value in (None, ""):
        return ""
    if key == "gender":
        return _GENDER_DISPLAY.get(value, str(value))
    if isinstance(value, list):
        return "、".join(str(x) for x in value if x not in (None, ""))
    return str(value)


def build_detail_sections(record: dict) -> list[dict]:
    """把解析 JSON 转为前端可渲染的分组结构。"""
    sections: list[dict] = []

    basic_fields = []
    for key, label in SCALAR_FIELDS:
        if key in {"channel_nickname"}:
            continue
        val = record.get(key)
        if key == "channel_nickname":
            val = record.get("channel_nickname") or ""
        basic_fields.append({"key": key, "label": label, "value": _fmt_scalar(key, val)})
    sections.append({"title": "基本信息", "type": "fields", "fields": basic_fields})

    for key, section_label, sub_order in ARRAY_FIELDS:
        items = record.get(key) or []
        if not items:
            continue
        labels = ARRAY_ITEM_LABELS.get(key, {})
        rendered_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            fields = []
            for sub in sub_order:
                val = item.get(sub)
                if sub == "end_date" and val is None and item.get("start_date"):
                    val = "至今"
                fields.append({
                    "key": sub,
                    "label": labels.get(sub, sub),
                    "value": _fmt_scalar(sub, val),
                })
            rendered_items.append(fields)
        sections.append({
            "title": section_label,
            "type": "array",
            "key": key,
            "items": rendered_items,
        })

    for key, label in STRING_LIST_FIELDS:
        items = record.get(key) or []
        if not items:
            continue
        sections.append({
            "title": label,
            "type": "list",
            "key": key,
            "value": "、".join(str(x) for x in items if x not in (None, "")),
        })

    meta_fields = []
    for key, label in META_LABELS.items():
        val = record.get(key)
        if val not in (None, ""):
            meta_fields.append({"key": key, "label": label, "value": str(val)})
    if meta_fields:
        sections.append({"title": "元数据", "type": "fields", "fields": meta_fields})

    return sections
