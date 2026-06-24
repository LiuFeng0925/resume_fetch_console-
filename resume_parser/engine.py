"""编排：扫描文件夹 → 并发跑流水线 → 收集结果 → 写出。对外暴露 run_batch。"""
from pathlib import Path
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from .extractor import SUPPORTED_EXT, is_pdf
from . import schema as _schema
from . import excel_writer

logger = logging.getLogger(__name__)


def scan_folder(folder, recursive: bool = False) -> list[Path]:
    folder = Path(folder)
    it = folder.rglob("*") if recursive else folder.glob("*")
    return [p for p in it if p.is_file() and p.suffix.lower() in SUPPORTED_EXT]

MIN_TEXT_FOR_LLM = 10


def _should_ocr_fallback(exc: Exception) -> bool:
    """文字层解析失败（无效 JSON 等）时尝试 OCR；API 超时不在此列。"""
    return isinstance(exc, ValueError)


def _parse_text(path: Path, text: str, *, deps, current_year: int, truncate: int) -> dict:
    raw = deps.extract_fields(text, truncate=truncate)
    record = _schema.normalize_record(raw, current_year=current_year)
    record = _schema.apply_name_fallback(record, path.name)
    return record


class Deps:
    """真实依赖容器：默认绑定 extractor/llm_client。测试时可替换。"""
    def __init__(self, client, model, max_retries, request_timeout=45):
        from . import extractor, llm_client
        self._extractor = extractor
        self._llm = llm_client
        self._client = client
        self._model = model
        self._max_retries = max_retries
        self._request_timeout = request_timeout
    def extract_text(self, path):
        return self._extractor.extract_text(path)
    def extract_text_via_ocr(self, path):
        return self._extractor.extract_text_via_ocr(path)
    def extract_fields(self, text, *, truncate):
        return self._llm.extract_fields(
            text, model=self._model, client=self._client,
            truncate=truncate, max_retries=self._max_retries,
            request_timeout=self._request_timeout)


def process_file(path, *, deps, current_year, truncate) -> dict:
    """跑完单份简历，返回 {source_file, status, record, used_ocr}。绝不抛异常。"""
    path = Path(path)
    base = {"source_file": path.name, "record": None, "used_ocr": False}
    try:
        text, used_ocr = deps.extract_text(path)
        base["used_ocr"] = used_ocr
        if len(text.strip()) < MIN_TEXT_FOR_LLM:
            return {**base, "status": "失败 · OCR无文字"}
        try:
            record = _parse_text(path, text, deps=deps, current_year=current_year, truncate=truncate)
            return {**base, "status": "成功", "record": record}
        except Exception as e:
            if used_ocr or not is_pdf(path) or not _should_ocr_fallback(e):
                return {**base, "status": f"失败 · {type(e).__name__}: {e}"}
            logger.info(
                "text layer parse failed for %s (%s), fallback to OCR",
                path.name,
                e,
            )
            ocr_text, _ = deps.extract_text_via_ocr(path)
            if len(ocr_text.strip()) < MIN_TEXT_FOR_LLM:
                return {
                    **base,
                    "status": f"失败 · 文字层解析失败后 OCR 仍无文字（原错: {type(e).__name__}: {e}）",
                }
            try:
                record = _parse_text(
                    path, ocr_text, deps=deps, current_year=current_year, truncate=truncate,
                )
                return {**base, "status": "成功 · OCR兜底", "record": record, "used_ocr": True}
            except Exception as ocr_err:
                return {
                    **base,
                    "used_ocr": True,
                    "status": (
                        f"失败 · 文字层 {type(e).__name__}: {e}；"
                        f"OCR后 {type(ocr_err).__name__}: {ocr_err}"
                    ),
                }
    except Exception as e:  # 单文件失败不影响整体
        return {**base, "status": f"失败 · {type(e).__name__}: {e}"}


def run_batch(input_dir, output_dir, *, deps, concurrency, current_year,
              truncate, timestamp, progress_cb, recursive=False) -> dict:
    files = scan_folder(input_dir, recursive=recursive)
    total = len(files)
    if total == 0:
        return {"total": 0, "ok": 0, "failed": 0, "excel_path": None, "results": []}

    results = []
    if concurrency <= 1:
        # 串行逐个处理，每个请求间隔 2 秒，避免触发 API 限流
        for i, f in enumerate(files):
            res = process_file(f, deps=deps, current_year=current_year, truncate=truncate)
            results.append(res)
            progress_cb(len(results), total, res)
            if i < len(files) - 1:
                time.sleep(2)
    else:
        # 并发处理：分批提交，每批 concurrency 个，批次间间隔 1 秒
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            batch_size = concurrency
            for i in range(0, len(files), batch_size):
                batch = files[i:i + batch_size]
                futures = {
                    pool.submit(process_file, f, deps=deps,
                                current_year=current_year, truncate=truncate): f
                    for f in batch
                }
                for fut in as_completed(futures):
                    res = fut.result()
                    results.append(res)
                    progress_cb(len(results), total, res)
                # 批次之间间隔 1 秒
                if i + batch_size < len(files):
                    time.sleep(1)

    rows = []
    ok = failed = 0
    for res in results:
        if res["record"] is not None:
            ok += 1
            excel_writer.dump_json(res["record"], output_dir, res["source_file"])
            rows.append(_schema.to_excel_row(res["record"], res["source_file"], res["status"]))
        else:
            failed += 1
            empty = _schema.normalize_record({}, current_year=current_year)
            rows.append(_schema.to_excel_row(empty, res["source_file"], res["status"]))

    excel_path = excel_writer.write_excel(rows, output_dir, timestamp)
    return {"total": total, "ok": ok, "failed": failed,
            "excel_path": excel_path, "results": results}


def extract_only_file(path, *, extract_text=None) -> dict:
    """仅取文：返回 {source_file, status, text, used_ocr}。绝不抛异常。"""
    if extract_text is None:
        from . import extractor
        extract_text = extractor.extract_text
    base = {"source_file": Path(path).name, "text": "", "used_ocr": False}
    try:
        text, used_ocr = extract_text(path)
        base["used_ocr"] = used_ocr
        text = text.strip()
        if not text:
            return {**base, "status": "失败 · 无文字"}
        return {**base, "text": text, "status": "成功"}
    except Exception as e:
        return {**base, "status": f"失败 · {type(e).__name__}: {e}"}


def run_extract_only(input_dir, output_dir, *, concurrency, recursive,
                     progress_cb, extract_text=None) -> dict:
    """仅取文批处理：扫描 → 并发取文 → 每份非空文本写 文本/<stem>.txt。不调豆包。"""
    files = scan_folder(input_dir, recursive=recursive)
    total = len(files)
    if total == 0:
        return {"total": 0, "ok": 0, "failed": 0, "text_dir": None, "results": []}
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(extract_only_file, f, extract_text=extract_text): f
                   for f in files}
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            progress_cb(len(results), total, res)
    ok = failed = 0
    text_dir = None
    for res in results:
        if res["text"]:
            ok += 1
            p = excel_writer.dump_text(res["text"], output_dir, res["source_file"])
            text_dir = p.parent
        else:
            failed += 1
    return {"total": total, "ok": ok, "failed": failed,
            "text_dir": text_dir, "results": results}
