"""候选人字段契约：字段定义、归一、校验、拍平为 Excel 单元格。
字段名对齐 docs/reference/candidate-data-model.md。纯函数，无 IO。"""

import json
import re

# 标量字段：(字段标识, Excel 中文列名)
# channel_nickname 在姓名后；top_edu_school / top_edu_major 在最高学历后
SCALAR_FIELDS = [
    ("name", "姓名"), ("channel_nickname", "渠道昵称"), ("job_display_id", "岗位编号"),
    ("tenant_id", "租户ID"), ("tenant_code", "租户名"),
    ("gender", "性别"), ("birth_date", "出生日期"),
    ("birth_year", "出生年份"), ("city", "当前城市"),
    ("political_status", "政治面貌"), ("marital_status", "婚姻状况"),
    ("phone", "手机"), ("email", "邮箱"), ("wechat", "微信"),
    ("education", "最高学历"),
    ("top_edu_school", "最高学历院校"), ("top_edu_major", "最高学历专业"),
    ("edu_note", "学历差异备注"),
    ("work_years", "工作年限"),
    ("work_start_date", "参加工作时间"), ("current_company", "当前公司"),
    ("current_position", "当前职位"), ("current_salary", "目前薪资"),
    ("expect_city", "期望工作地"), ("expect_salary", "期望薪资"),
    ("self_description", "自我描述"),
]

# 数组字段：(字段标识, Excel 中文列名, 单段拼接用的子字段顺序)
ARRAY_FIELDS = [
    ("work_experiences", "工作经历",
     ["company", "industry", "position", "start_date", "end_date", "description"]),
    ("education_history", "教育经历",
     ["school", "school_type", "major", "degree", "start_date", "end_date"]),
    ("project_experiences", "项目经验",
     ["name", "role", "start_date", "end_date", "description", "responsibility"]),
    ("internship_experiences", "实习经历",
     ["company", "position", "start_date", "end_date", "description"]),
    ("language_abilities", "语言能力",
     ["language", "reading_writing", "listening_speaking"]),
    ("job_intentions", "求职意向",
     ["job_status", "expected_positions", "expected_industries",
      "expected_cities", "expected_salary_min", "expected_salary_max"]),
]

# 纯字符串数组字段
STRING_LIST_FIELDS = [("skills", "技能"), ("certificates", "证书")]

SEGMENT_SEP = "\n"      # Excel 单元格内段与段之间换行
FIELD_SEP = " | "       # 段内字段之间用竖线

_GENDER_MAP = {
    "男": "male", "male": "male", "m": "male",
    "女": "female", "female": "female", "f": "female",
}

_GENDER_DISPLAY = {"male": "男", "female": "女", "unknown": "未知"}


def normalize_gender(value) -> str:
    if not value:
        return "unknown"
    return _GENDER_MAP.get(str(value).strip().lower(), "unknown")


def derive_birth_year(data: dict, current_year: int):
    if data.get("birth_year") is not None:
        return data["birth_year"]
    age = data.get("age")
    if isinstance(age, int) and age > 0:
        return current_year - age
    # 有出生日期但没有年龄时，从出生日期中提取年份
    birth_date = data.get("birth_date")
    if isinstance(birth_date, str) and len(birth_date) >= 4:
        try:
            return int(birth_date[:4])
        except ValueError:
            pass
    return None


def _fmt_value(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(str(x) for x in v if x not in (None, ""))
    return str(v)


def flatten_array(field_key: str, items: list) -> str:
    """把对象数组拍平成一个单元格字符串，多段用换行分隔，段内字段用竖线分隔。"""
    sub_order = next((s for k, _, s in ARRAY_FIELDS if k == field_key), [])
    segments = []
    for item in items or []:
        if not isinstance(item, dict):
            text = _fmt_value(item)
            if text:
                segments.append(text.replace("\n", " ").replace("\r", " "))
            continue
        parts = []
        for sub in sub_order:
            val = item.get(sub)
            if sub == "end_date" and val is None and "start_date" in item:
                val = "至今"
            parts.append(_fmt_value(val))
        while parts and parts[-1] == "":
            parts.pop()
        while parts and parts[0] == "":
            parts.pop(0)
        seg = FIELD_SEP.join(parts)
        seg = seg.replace("\n", " ").replace("\r", " ")
        segments.append(seg)
    return SEGMENT_SEP.join(s for s in segments if s)


def flatten_string_list(items: list) -> str:
    return "、".join(str(x) for x in (items or []) if x not in (None, ""))


def _to_int_or_none(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _coerce_llm_root(raw) -> dict:
    """模型偶发返回单元素数组而非对象，做兼容。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], dict):
        return raw[0]
    return {}


def _coerce_array_value(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _coerce_scalar_field(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return _fmt_value(value)
    return value


def _coerce_expect_city(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if x not in (None, "")]
        return parts[0] if parts else ""
    return str(value).strip() if value else ""


def _normalize_job_intentions(intentions: list, expect_city: str) -> list:
    """对齐 spec 5.5 强契约：expect_city 并入 expected_cities(排最前)、薪资转 int/null。"""
    intentions = _coerce_array_value(intentions)
    expect_city = _coerce_expect_city(expect_city)
    result = []
    for it in intentions or []:
        if not isinstance(it, dict):
            continue  # 跳过非字典元素（模型偶尔返回字符串）
        item = dict(it)
        cities = item.get("expected_cities") or []
        if not isinstance(cities, list):
            cities = [cities]
        if expect_city and expect_city not in cities:
            cities = [expect_city] + cities
        item["expected_cities"] = cities
        item["expected_salary_min"] = _to_int_or_none(item.get("expected_salary_min"))
        item["expected_salary_max"] = _to_int_or_none(item.get("expected_salary_max"))
        result.append(item)
    if not result and expect_city:
        result = [{"expected_cities": [expect_city],
                   "expected_salary_min": None, "expected_salary_max": None}]
    return result


def derive_education(data: dict) -> str:
    """从 education_history 中取学历等级最高的那条记录的 degree。
    若 education_history 为空，则用模型直接解析的 education 字段兜底。
    与 top_edu_school / top_edu_major 同源，保证三者一致。"""
    item = _find_top_edu_item(data)
    if item:
        for key in ("degree", "school_type"):
            val = item.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    # 教育经历为空，用模型解析的 education 兜底
    edu = data.get("education")
    if edu is not None and str(edu).strip():
        return str(edu).strip()
    return ""


def derive_edu_note(data: dict) -> str:
    """当模型解析的 education 与教育经历最高学历不一致时，记录差异备注。
    返回空字符串表示无差异。"""
    model_edu = data.get("education")
    if model_edu is None or not str(model_edu).strip():
        return ""  # 模型没解析出学历，无需比较
    model_edu = str(model_edu).strip()
    history = data.get("education_history") or []
    if not history:
        return ""  # 教育经历为空，用模型值兜底，无需标注
    item = _find_top_edu_item(data)
    if not item:
        return ""
    hist_edu = ""
    for key in ("degree", "school_type"):
        val = item.get(key)
        if val is not None and str(val).strip():
            hist_edu = str(val).strip()
            break
    if not hist_edu:
        return ""
    # 比较两者等级
    model_rank = _edu_rank(model_edu)
    hist_rank = _edu_rank(hist_edu)
    if model_rank != hist_rank:
        return f"模型解析为{model_edu}，教育经历最高为{hist_edu}"
    return ""


# ---------- 学历等级，用于找"最高"学历 ----------
_EDU_RANK = {
    "博士": 7, "硕士": 6, "研究生": 6,
    "本科": 5, "大本": 5, "学士": 5,
    "大专": 4, "专科": 4, "高职": 4,
    "高中": 3, "中专": 3, "技校": 3,
    "初中": 2, "小学": 1,
}

def _edu_rank(degree_str: str) -> int:
    """把学历字符串映射到等级，用于比较高低。未知返回 0。"""
    if not degree_str:
        return 0
    s = str(degree_str).strip()
    for k, v in _EDU_RANK.items():
        if k in s:
            return v
    return 0


def _find_top_edu_item(data: dict) -> dict | None:
    """从 education_history 找最高学历那条记录（degree 等级最高）。"""
    history = data.get("education_history") or []
    if not history:
        return None
    best = None
    best_rank = -1
    for item in history:
        if not isinstance(item, dict):
            continue
        degree = item.get("degree") or item.get("school_type") or ""
        rank = _edu_rank(degree)
        if rank > best_rank:
            best_rank = rank
            best = item
    return best


def derive_top_edu_school(data: dict) -> str:
    """取最高学历对应学校名称，无则返回空字符串。"""
    item = _find_top_edu_item(data)
    if item:
        school = item.get("school")
        if school and str(school).strip():
            return str(school).strip()
    return ""


def derive_top_edu_major(data: dict) -> str:
    """取最高学历对应专业，无则返回空字符串。"""
    item = _find_top_edu_item(data)
    if item:
        major = item.get("major")
        if major and str(major).strip():
            return str(major).strip()
    return ""


def migrate_job_id_to_display(data: dict) -> dict:
    """将历史 job_id 字段迁移为 job_display_id（接口与 Excel 使用后者）。"""
    out = dict(data)
    legacy = out.get("job_id")
    if legacy not in (None, "") and not out.get("job_display_id"):
        out["job_display_id"] = legacy
    out.pop("job_id", None)
    return out


def job_display_id_value(data: dict) -> str:
    return str(data.get("job_display_id") or data.get("job_id") or "").strip()


def parse_channel_nickname(filename: str) -> str:
    """从文件名中提取渠道昵称。
    规则：取 '】' 之后、第一个 '_' 之前的部分（去掉扩展名之前的后缀如 _N年）。
    示例：'xxx_【职位_泰安_5-8K】泡泡泡泡_3年.pdf' → '泡泡泡泡'
    若无法匹配则返回空字符串。"""
    # 去掉路径只保留文件名
    fname = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    # 去掉扩展名
    fname = re.sub(r'\.[^.]+$', '', fname)
    # 找最后一个 '】' 之后的内容
    idx = fname.rfind('】')
    if idx == -1:
        return ""
    after = fname[idx + 1:]
    # after 形如 '泡泡泡泡_3年' 或 'Nawab khan_一年以内'
    # 取第一个 '_' 之前的内容
    parts = after.split('_', 1)
    nickname = parts[0].strip()
    return nickname


def apply_name_fallback(rec: dict, source_file: str) -> dict:
    """姓名为空时，用文件名中的渠道昵称兜底。"""
    name = str(rec.get("name") or "").strip()
    if name:
        return rec
    nickname = parse_channel_nickname(source_file)
    if not nickname:
        return rec
    updated = dict(rec)
    updated["name"] = nickname
    if not str(updated.get("channel_nickname") or "").strip():
        updated["channel_nickname"] = nickname
    return updated


def normalize_record(raw: dict, current_year: int) -> dict:
    """把大模型返回的原始 dict 归一为完整记录：补齐所有字段、做归一。
    输出同时满足 Excel 拍平与 spec 5.5 的 JSON 导入契约。
    注意：channel_nickname / top_edu_school / top_edu_major 不在 raw 里，
    由 to_excel_row 在写行时从 source_file / education_history 派生，此处仅占位。"""
    raw = _coerce_llm_root(raw)
    rec = {}
    # channel_nickname、top_edu_school、top_edu_major、edu_note 由 to_excel_row 派生，跳过
    _DERIVED = {"gender", "birth_year", "education",
                "channel_nickname", "top_edu_school", "top_edu_major", "edu_note",
                "job_display_id", "tenant_id", "tenant_code"}
    for key, _ in SCALAR_FIELDS:
        if key in _DERIVED:
            continue
        rec[key] = _coerce_scalar_field(raw.get(key))
    rec["gender"] = normalize_gender(raw.get("gender"))
    by = derive_birth_year(raw, current_year)
    rec["birth_year"] = by if by is not None else ""
    # 注意顺序：先算 edu_note（基于 raw 中模型原始 education），再覆盖 education
    rec["edu_note"] = derive_edu_note(raw)
    rec["education"] = derive_education(raw)
    # 这两个字段存到 rec 方便 JSON 序列化，但以 education_history 为准
    rec["top_edu_school"] = derive_top_edu_school(raw)
    rec["top_edu_major"] = derive_top_edu_major(raw)
    # channel_nickname 只能在文件名已知时填，这里先留空
    rec["channel_nickname"] = ""
    rec["job_display_id"] = (
        raw.get("job_display_id")
        if raw.get("job_display_id") is not None
        else (raw.get("job_id") if raw.get("job_id") is not None else "")
    )
    rec["tenant_id"] = raw.get("tenant_id") if raw.get("tenant_id") is not None else ""
    rec["tenant_code"] = raw.get("tenant_code") if raw.get("tenant_code") is not None else ""
    for key, _, _ in ARRAY_FIELDS:
        rec[key] = _coerce_array_value(raw.get(key))
    for key, _ in STRING_LIST_FIELDS:
        val = raw.get(key)
        if isinstance(val, list):
            rec[key] = val
        elif val in (None, ""):
            rec[key] = []
        else:
            rec[key] = [val]
    rec["job_intentions"] = _normalize_job_intentions(
        rec.get("job_intentions"), rec.get("expect_city"))
    return rec


def parse_active_time(filename: str) -> str:
    """从文件名中解析时间戳，转为 yyyy-MM-dd HH:mm:ss 格式。
    支持格式：20260602_194731（可在任意位置）→ 2026-06-02 19:47:31
    无法解析时返回空字符串。"""
    m = re.search(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", filename)
    if not m:
        return ""
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{s}"


def excel_headers() -> list[str]:
    headers = [zh for _, zh in SCALAR_FIELDS]
    headers += [zh for _, zh, _ in ARRAY_FIELDS]
    headers += [zh for _, zh in STRING_LIST_FIELDS]
    headers += ["最近活跃时间", "原始文件名", "解析状态", "解析后json"]
    return headers


def to_excel_row(rec: dict, source_file: str, status: str) -> list:
    row = []
    for k, _ in SCALAR_FIELDS:
        v = rec.get(k)
        if k == "gender":
            v = _GENDER_DISPLAY.get(v, v)
        elif k == "channel_nickname":
            # 从文件名实时派生（不依赖 rec 里的占位空字符串）
            v = parse_channel_nickname(source_file)
        elif k == "top_edu_school":
            v = derive_top_edu_school(rec)
        elif k == "top_edu_major":
            v = derive_top_edu_major(rec)
        elif k == "edu_note":
            # 直接用 rec 中已计算好的值（不能重新 derive，因为 rec.education 已被覆盖）
            v = rec.get("edu_note", "")
        row.append(_fmt_value(v))
    row += [flatten_array(k, rec.get(k, [])) for k, _, _ in ARRAY_FIELDS]
    row += [flatten_string_list(rec.get(k, [])) for k, _ in STRING_LIST_FIELDS]
    row += [parse_active_time(source_file), source_file, status]
    payload = {**rec, "_source_file": source_file,
               "channel_nickname": parse_channel_nickname(source_file)}
    row.append(json.dumps(payload, ensure_ascii=False, indent=2))
    return row
