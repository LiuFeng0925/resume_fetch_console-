"""纯文本 → 对齐 schema 的 JSON。封装豆包（火山方舟，OpenAI 兼容）调用。"""
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

logger = logging.getLogger(__name__)

# 全局 LLM 请求节流：任意两次请求启动间隔至少该秒数（并发 worker 也受此约束）
LLM_MIN_REQUEST_INTERVAL = 1.5
# 并发 worker 错峰启动：第 2 路延迟该秒数再开始（与节流叠加，进一步降低突发 429）
LLM_STAGGER_DELAY = 1.0
# 429 / RateLimit 专用退避（秒），不消耗 max_retries 配额，最多额外重试 len 次
RATE_LIMIT_BACKOFF_SECONDS = (2, 5, 10)

_llm_throttle_lock = threading.Lock()
_llm_last_request_at: float | None = None

_FIELD_HINT = (
    "标量字段: name, gender(男/女→保留原文,系统侧再归一), birth_date(精确到日,YYYY-MM-DD), "
    "age(整数,若只写年龄), city, political_status, marital_status, phone, email, wechat, "
    "education(最高学历), work_years(整数), work_start_date, current_company, "
    "current_position, current_salary(整数,元), expect_city, expect_salary(原始展示串,如\"20-30k\"), self_description。\n"
    "数组字段(对象数组): "
    "work_experiences[company,industry,position,start_date,end_date,description,skills], "
    "education_history[school,school_type,major,degree,start_date,end_date], "
    "project_experiences[name,role,start_date,end_date,description,responsibility], "
    "internship_experiences[company,position,start_date,end_date,description], "
    "language_abilities[language,reading_writing,listening_speaking], "
    "job_intentions[job_status,expected_positions(数组),expected_industries(数组),"
    "expected_cities(数组),expected_salary_min(整数),expected_salary_max(整数)]。\n"
    "字符串数组字段: skills, certificates。"
)

SYSTEM_PROMPT = (
    "你是简历信息抽取器。把简历纯文本抽取为 JSON。\n"
    "规则：\n"
    "1) 只输出一个 JSON 对象，不要任何解释或 markdown。\n"
    "2) 日期统一 YYYY-MM（birth_date 例外，精确到日 YYYY-MM-DD）；在职/至今的 end_date 用 null。\n"
    "3) 薪资单位为元，取整数。\n"
    "4) 填槽逻辑：所有对象数组中的每条记录，必须包含该数组的全部子字段，找不到的给 null，绝不省略字段名。例如 education_history 的每一条都必须有 school、school_type、major、degree、start_date、end_date，即使值为 null。work_experiences、project_experiences、internship_experiences、language_abilities、job_intentions 同理。\n"
    "5) 标量字段找不到就省略或留空字符串，绝不编造。\n"
    "编造包括但不限于：从其他字段推导（如从年龄算出生日期）、填充默认值（如不知道月份就填01、不知道日就填1号）、\n"
    "猜测（如看到邮箱含zhang就猜姓张、看到公司名含科技就猜行业）、拼凑（如简历只写了城市名就编造完整地址）。\n"
    "只有简历原文明确写出的信息才可以填写。若原文写了「22岁」则 age 可填22，但 birth_date 不可以从中推导。\n"
    "5b) birth_date 特别规则：如果简历只写了年龄（如「22岁」）而未写明出生日期，"
    "则 birth_date 必须留空字符串或省略，绝不要从年龄倒推编造出生日期。"
    "只有简历原文明确写了出生日期（如「2004年1月」「2004-01-15」）时才填写 birth_date。\n"
    "6) 姓名(name)特别规则：文本可能来自图片 OCR，段落顺序会乱。除「姓名：XXX」外，"
    "以下也应识别为 name：简历开头或「基本信息」附近的单独 2-4 个汉字人名；"
    "出现在「工作经历/教育经历」标题与日期之间的孤立人名行（常见于 OCR 把头像旁姓名误排到经历区）；"
    "「XXX的简历」中的 XXX。不要把公司名、学校名、岗位名、城市名当作姓名。"
    "若同时出现多个疑似人名，优先取最像真实姓名的那个（2-3 个汉字常见），不要用邮箱/文件名里的昵称。\n"
    "7) 若简历未明确标注「当前公司」「当前职位」，则取工作经历(work_experiences)中结束时间最新的那条（end_date 为 null 表示在职/至今），将其 company 填入 current_company，position 填入 current_position。\n"
    f"字段定义：\n{_FIELD_HINT}"
)


def build_messages(text: str, truncate: int = 6000) -> list[dict]:
    body = text[:truncate]
    # 将中文引号替换为「」避免模型输出 ASCII " 破坏 JSON 结构
    body = body.replace("\u201c", "\u300c").replace("\u201d", "\u300d")
    body = body.replace("\u2018", "\u300e").replace("\u2019", "\u300f")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"简历文本：\n{body}"},
    ]


def _has_resume_content(data: dict) -> bool:
    """模型返回的对象是否含有效简历字段（空 {} 视为无效）。"""
    if not data:
        return False
    for key in ("name", "phone", "email", "work_experiences", "education_history"):
        val = data.get(key)
        if val:
            return True
    return False


def _is_trivial_garbage_response(content: str) -> bool:
    """极短或明显无效的模型输出（如 [1]、[22]、[]），重试通常无意义。"""
    s = (content or "").strip()
    if len(s) < 30:
        return True
    if re.fullmatch(r"\[[\d,\s]*\]", s):
        return True
    return False


def parse_response(content: str) -> dict:
    """从模型输出里提取 JSON 对象。"""
    s = content.strip()
    # 1) 尝试提取 markdown 代码块中的 JSON
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    else:
        # 2) 回退：找最外层的 { ... }
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end != -1 and end > start:
            s = s[start:end + 1]

    # 3) 清理常见的 JSON 格式问题
    #    模型有时在字符串值内混入中文引号「"」替换为转义
    #    以及尾部多余逗号
    s = re.sub(r",\s*([}\]])", r"\1", s)

    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"模型返回非法 JSON: {e}") from e
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
        raise ValueError("模型返回 JSON 数组而非对象")
    if not isinstance(data, dict):
        raise ValueError(f"模型返回非对象 JSON: {type(data).__name__}")
    if not _has_resume_content(data):
        raise ValueError("模型返回空简历对象")
    return data


def _is_rate_limit_error(exc: Exception) -> bool:
    """识别 OpenRouter / OpenAI 429 与 RateLimitError。"""
    name = type(exc).__name__
    if name in {"RateLimitError", "RateLimitExceeded"}:
        return True
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "rate-limit" in msg


def _acquire_llm_throttle_slot() -> None:
    """全局锁 + 最小间隔，保证两次 LLM 请求不会紧挨着发出。"""
    global _llm_last_request_at
    with _llm_throttle_lock:
        now = time.monotonic()
        if _llm_last_request_at is not None:
            wait = LLM_MIN_REQUEST_INTERVAL - (now - _llm_last_request_at)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        _llm_last_request_at = now


def _reset_llm_throttle_for_tests() -> None:
    global _llm_last_request_at
    with _llm_throttle_lock:
        _llm_last_request_at = None


def _build_http_timeout(request_timeout: int):
    """拆分 connect/read/write 超时，避免响应体挂死时无限等待。"""
    import httpx

    read_timeout = max(10.0, float(request_timeout))
    return httpx.Timeout(
        connect=min(10.0, read_timeout),
        read=read_timeout,
        write=min(15.0, read_timeout),
        pool=5.0,
    )


def _call_chat_completion(client, *, model: str, messages: list[dict], hard_timeout: float):
    """在线程中调用 API，并由硬超时兜底（防止 httpx 偶发不触发超时）。"""
    create_kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    base_url = str(getattr(client, "base_url", "") or "")
    if "openrouter.ai" in base_url:
        create_kwargs["extra_body"] = {"provider": {"require_parameters": True}}

    def _do_call():
        return client.chat.completions.create(**create_kwargs)

    _acquire_llm_throttle_slot()
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_do_call)
        try:
            return fut.result(timeout=hard_timeout)
        except FuturesTimeoutError as e:
            raise TimeoutError(f"模型请求超过 {int(hard_timeout)}s 未响应") from e


def make_client(cfg):
    """用配置创建豆包(OpenAI 兼容)客户端。"""
    from openai import OpenAI
    return OpenAI(
        api_key=cfg.ark_api_key,
        base_url=cfg.ark_base_url,
        timeout=_build_http_timeout(cfg.request_timeout),
        max_retries=0,  # SDK 层不重试，重试完全交给 extract_fields，避免叠加拖慢
    )


def extract_fields(text: str, model: str, client, *,
                   truncate: int = 6000, max_retries: int = 1,
                   request_timeout: int = 45) -> dict:
    """调用模型抽取字段，失败重试。返回原始 dict（未归一）。"""
    messages = build_messages(text, truncate=truncate)
    hard_timeout = max(10.0, float(request_timeout)) + 5.0
    last_err = None
    rate_limit_backoff_idx = 0
    max_attempts = max_retries + 1
    attempt = 0
    while attempt < max_attempts:
        try:
            resp = _call_chat_completion(
                client, model=model, messages=messages, hard_timeout=hard_timeout,
            )
        except Exception as e:
            last_err = e
            if isinstance(e, TimeoutError):
                logger.info("llm request abandon: %s", e)
                raise last_err
            if _is_rate_limit_error(e) and rate_limit_backoff_idx < len(RATE_LIMIT_BACKOFF_SECONDS):
                delay = RATE_LIMIT_BACKOFF_SECONDS[rate_limit_backoff_idx]
                rate_limit_backoff_idx += 1
                logger.info(
                    "llm rate limit backoff %ss (rl_retry %s/%s): %s",
                    delay,
                    rate_limit_backoff_idx,
                    len(RATE_LIMIT_BACKOFF_SECONDS),
                    e,
                )
                time.sleep(delay)
                continue
            if attempt + 1 < max_attempts:
                logger.info("llm request retry %s/%s: %s", attempt + 1, max_attempts, e)
                attempt += 1
                continue
            break
        content = resp.choices[0].message.content or ""
        try:
            return parse_response(content)
        except ValueError as e:
            last_err = e
            if _is_trivial_garbage_response(content):
                logger.info("llm json abandon: trivial response %r", content[:80])
                raise last_err
            if attempt + 1 < max_attempts:
                logger.info("llm json retry %s/%s: %s", attempt + 1, max_attempts, e)
                attempt += 1
                continue
            break
    if isinstance(last_err, Exception):
        raise last_err
    raise ValueError(f"重试 {max_retries} 次仍失败: {last_err}")
