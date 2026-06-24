from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from resume_parser import schema as resume_schema
from resume_parser.engine import Deps, process_file, scan_folder
from resume_parser.excel_writer import dump_json, write_excel
from resume_parser.llm_client import LLM_STAGGER_DELAY, make_client

from src.config import AccountConfig, ParserConfig, PushConfig, clamp_parser_concurrency
from src.parse_progress import clear_progress, write_progress
from src.parse_store import ParseRecordStore

if TYPE_CHECKING:
    from src.push_store import PushRecordStore

logger = logging.getLogger(__name__)

MAX_PARSE_RESCAN_ROUNDS = 50

class _ParseJobLock:
    """防止多个 parse 进程同时跑，避免抢 API 与进度互相覆盖。"""

    def __init__(self, lock_path: Path):
        self._lock_path = lock_path
        self._fh = None

    def acquire(self) -> bool:
        from src.parse_lock_util import clear_stale_parse_lock

        clear_stale_parse_lock(self._lock_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass
class ParseRunResult:
    total: int = 0
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    excel_path: str | None = None
    error: str | None = None
    results: list[dict] = field(default_factory=list)
    push_summaries: list[dict] = field(default_factory=list)


def resolve_account_from_filename(
    filename: str, accounts: tuple[AccountConfig, ...]
) -> tuple[str, str, str, str, str]:
    """从文件名中的邮箱地址匹配账号，返回 (account_name, username, job_display_id, tenant_id, tenant_code)。"""
    stem = re.sub(r"\.[^.]+$", "", Path(filename).name).lower()
    for acct in accounts:
        email = acct.imap.username.lower()
        if email and email in stem:
            return (
                acct.name,
                acct.imap.username,
                acct.job_display_id,
                acct.tenant_id,
                acct.tenant_code,
            )
    return "", "", "", "", ""


def _move_to_archive(src: Path, archive_dir: Path) -> Path | None:
    """解析完成或已解析跳过后，将附件移至归档目录。"""
    if not src.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / src.name
    if dest.exists():
        stem, ext = src.stem, src.suffix
        n = 2
        while dest.exists():
            dest = archive_dir / f"{stem}({n}){ext}"
            n += 1
    shutil.move(str(src), str(dest))
    logger.info("archived %s -> %s", src.name, dest)
    return dest


def _should_abandon(status: str) -> bool:
    """超时、无效 JSON（含 OCR 兜底后）等：不再 file_attempts 重试，避免堆积卡住队列。"""
    if not str(status).startswith("失败"):
        return False
    if "TimeoutError" in status or "模型请求超过" in status:
        return True
    if "ValueError" in status or "模型返回" in status:
        return True
    return False


def _process_file_with_retries(
    path: Path,
    *,
    deps: Deps,
    current_year: int,
    truncate: int,
    max_attempts: int,
) -> dict:
    """单次任务内重试；超时/无效 JSON 等立即放弃；全部失败后标记为永久失败。"""
    last_res: dict | None = None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        res = process_file(path, deps=deps, current_year=current_year, truncate=truncate)
        last_res = res
        if res.get("status", "").startswith("成功"):
            return res
        status = str(res.get("status") or "")
        if _should_abandon(status):
            logger.info("parse abandon for %s: %s", path.name, status)
            break
        if attempt < attempts:
            logger.info(
                "parse retry %s/%s for %s: %s",
                attempt,
                attempts,
                path.name,
                status,
            )
            time.sleep(1)
    assert last_res is not None
    status = last_res.get("status", "失败")
    if not status.startswith("成功") and "已放弃" not in status:
        last_res = {**last_res, "status": f"{status}（已放弃）"}
    return last_res


def _parse_file_worker(
    path: Path,
    *,
    deps: Deps,
    current_year: int,
    truncate: int,
    max_attempts: int,
    accounts: tuple[AccountConfig, ...],
) -> dict:
    """在线程中解析单文件（仅 LLM/抽文本，不写 DB/Excel）。"""
    file_path = str(path.resolve())
    file_mtime = path.stat().st_mtime
    account_name, _, job_display_id, tenant_id, tenant_code = resolve_account_from_filename(
        path.name, accounts
    )
    res = _process_file_with_retries(
        path,
        deps=deps,
        current_year=current_year,
        truncate=truncate,
        max_attempts=max_attempts,
    )
    return {
        "path": path,
        "file_path": file_path,
        "file_mtime": file_mtime,
        "account_name": account_name,
        "job_display_id": job_display_id,
        "tenant_id": tenant_id,
        "tenant_code": tenant_code,
        "res": res,
    }


def _process_pending_files(
    pending_files: list[Path],
    *,
    parser_cfg: ParserConfig,
    deps: Deps,
    current_year: int,
    accounts: tuple[AccountConfig, ...],
    archive_dir: Path,
    output_dir: Path,
    store: ParseRecordStore,
    push_cfg: PushConfig | None,
    push_store_factory: Callable[[], PushRecordStore] | None,
    result: ParseRunResult,
    rows: list[list],
    pending_records: list[dict],
    processed: int,
    write_progress_state: Callable[..., None],
    maybe_flush_chunk: Callable[[], None],
) -> tuple[list[list], list[dict], int]:
    """串行或并发解析 pending_files；DB/Excel/推送仅在当前线程执行。"""

    def _apply_outcome(outcome: dict) -> None:
        nonlocal processed, rows, pending_records
        path = outcome["path"]
        res = outcome["res"]
        record = res.get("record")
        job_display_id = outcome["job_display_id"]
        tenant_id = outcome["tenant_id"]
        tenant_code = outcome["tenant_code"]

        if record is not None:
            record["job_display_id"] = job_display_id
            record["tenant_id"] = tenant_id
            record["tenant_code"] = tenant_code
            result.ok += 1
            dump_json(record, output_dir, res["source_file"])
            rows.append(resume_schema.to_excel_row(record, res["source_file"], res["status"]))
            _move_to_archive(path, archive_dir)
        else:
            result.failed += 1
            empty = resume_schema.normalize_record({}, current_year=current_year)
            empty["job_display_id"] = job_display_id
            empty["tenant_id"] = tenant_id
            empty["tenant_code"] = tenant_code
            rows.append(resume_schema.to_excel_row(empty, res["source_file"], res["status"]))
            _move_to_archive(path, archive_dir)

        item = {
            "source_file": res["source_file"],
            "file_path": outcome["file_path"],
            "file_mtime": outcome["file_mtime"],
            "account_name": outcome["account_name"],
            "job_display_id": job_display_id,
            "tenant_id": tenant_id,
            "tenant_code": tenant_code,
            "status": res["status"],
            "parsed_json": record,
            "used_ocr": bool(res.get("used_ocr")),
        }
        pending_records.append(item)
        _upsert_item(
            store,
            item,
            excel_path=None,
            reset_push_pending=str(res["status"]).startswith("成功"),
        )
        processed += 1
        _sync_active_progress()
        maybe_flush_chunk()
        result.results.append({
            "source_file": res["source_file"],
            "status": res["status"],
            "job_display_id": job_display_id,
            "tenant_id": tenant_id,
            "tenant_code": tenant_code,
            "account_name": outcome["account_name"],
        })

    concurrency = clamp_parser_concurrency(parser_cfg.concurrency)
    worker_kwargs = {
        "deps": deps,
        "current_year": current_year,
        "truncate": parser_cfg.text_truncate,
        "max_attempts": parser_cfg.file_attempts,
        "accounts": accounts,
    }

    active_lock = threading.Lock()
    active_files: list[str] = []

    def _sync_active_progress() -> None:
        with active_lock:
            snapshot = list(active_files)
        write_progress_state(active_files=snapshot)

    def _worker_tracked(path: Path, *, stagger_delay: float = 0) -> dict:
        if stagger_delay > 0:
            time.sleep(stagger_delay)
        with active_lock:
            active_files.append(path.name)
        _sync_active_progress()
        try:
            return _parse_file_worker(path, **worker_kwargs)
        finally:
            with active_lock:
                if path.name in active_files:
                    active_files.remove(path.name)
            _sync_active_progress()

    if concurrency <= 1:
        for path in pending_files:
            _apply_outcome(_worker_tracked(path))
        return rows, pending_records, processed

    logger.info("parse concurrency: workers=%s files=%s", concurrency, len(pending_files))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _worker_tracked,
                path,
                stagger_delay=(idx % concurrency) * LLM_STAGGER_DELAY,
            ): path
            for idx, path in enumerate(pending_files)
        }
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                _apply_outcome(fut.result())
            except Exception as exc:
                logger.exception("parse worker failed for %s", path.name)
                result.failed += 1
                processed += 1
                _sync_active_progress()
                result.results.append({
                    "source_file": path.name,
                    "status": f"失败 · {type(exc).__name__}: {exc}",
                    "job_display_id": "",
                    "tenant_id": "",
                    "tenant_code": "",
                    "account_name": "",
                })

    return rows, pending_records, processed


def _upsert_item(
    store: ParseRecordStore,
    item: dict,
    *,
    excel_path: str | None,
    reset_push_pending: bool = False,
) -> int:
    record_id = store.upsert_record(
        source_file=item["source_file"],
        file_path=item["file_path"],
        file_mtime=item["file_mtime"],
        account_name=item.get("account_name") or "",
        job_display_id=item.get("job_display_id") or "",
        tenant_id=item.get("tenant_id") or "",
        tenant_code=item.get("tenant_code") or "",
        status=item["status"],
        parsed_json=item.get("parsed_json"),
        excel_path=excel_path,
        used_ocr=bool(item.get("used_ocr")),
        reset_push_pending=reset_push_pending,
    )
    item["parse_record_id"] = record_id
    return record_id


def _record_to_row_item(rec: dict) -> tuple[list, dict] | None:
    parsed = rec.get("parsed_json")
    if not isinstance(parsed, dict):
        return None
    source_file = str(rec.get("source_file") or "")
    status = str(rec.get("status") or "")
    normalized = resume_schema.normalize_record(parsed, current_year=datetime.now().year)
    if rec.get("job_display_id") or rec.get("job_id"):
        normalized["job_display_id"] = rec.get("job_display_id") or rec.get("job_id") or ""
    if rec.get("tenant_id"):
        normalized["tenant_id"] = rec.get("tenant_id")
    if rec.get("tenant_code"):
        normalized["tenant_code"] = rec.get("tenant_code")
    normalized = resume_schema.apply_name_fallback(normalized, source_file)
    row = resume_schema.to_excel_row(normalized, source_file, status)
    item = {
        "source_file": source_file,
        "file_path": rec.get("file_path") or "",
        "file_mtime": float(rec.get("file_mtime") or 0),
        "account_name": rec.get("account_name") or "",
        "job_display_id": rec.get("job_display_id") or rec.get("job_id") or "",
        "tenant_id": rec.get("tenant_id") or "",
        "tenant_code": rec.get("tenant_code") or "",
        "status": status,
        "parsed_json": parsed,
        "used_ocr": bool(rec.get("used_ocr")),
        "parse_record_id": rec.get("id"),
    }
    return row, item


def _flush_chunk(
    *,
    rows: list[list],
    pending_records: list[dict],
    chunk_seq: int,
    job_timestamp: str,
    output_dir: Path,
    store: ParseRecordStore,
    push_cfg: PushConfig | None,
    push_store_factory: Callable[[], PushRecordStore] | None,
    result: ParseRunResult,
) -> str | None:
    """写入 Excel、更新 DB，并推送本组记录。"""
    if not rows:
        return None
    timestamp = f"{job_timestamp}_{chunk_seq:03d}"
    excel_path = write_excel(rows, output_dir, timestamp)
    excel_str = str(excel_path)
    for item in pending_records:
        _upsert_item(store, item, excel_path=excel_str)
    logger.info(
        "parse chunk flushed: seq=%s count=%s excel=%s",
        chunk_seq,
        len(pending_records),
        excel_str,
    )
    if push_cfg and push_cfg.enabled and push_store_factory:
        push_items = [
            item for item in pending_records
            if str(item.get("status") or "").startswith("成功")
        ]
        if push_items:
            from src.push_orchestrator import push_parse_batch

            push_store = push_store_factory()
            try:
                summaries = push_parse_batch(
                    push_store,
                    push_cfg,
                    excel_path=excel_str,
                    items=push_items,
                    trigger_type="auto",
                    output_dir=output_dir,
                    parse_store=store,
                )
                result.push_summaries.extend(summaries)
            finally:
                push_store.close()
    if not result.excel_path:
        result.excel_path = excel_str
    return excel_str


def _recover_unpushed_chunks(
    *,
    store: ParseRecordStore,
    output_dir: Path,
    chunk_size: int,
    job_timestamp: str,
    push_cfg: PushConfig | None,
    push_store_factory: Callable[[], PushRecordStore] | None,
    result: ParseRunResult,
) -> int:
    """补推历史遗留：已成功解析但尚未写入 Excel 的记录（按组分块）。"""
    recovered = 0
    chunk_seq = 0
    while True:
        records = store.list_unpushed_success(limit=chunk_size)
        if not records:
            break
        rows: list[list] = []
        items: list[dict] = []
        for rec in records:
            pair = _record_to_row_item(rec)
            if not pair:
                continue
            row, item = pair
            rows.append(row)
            items.append(item)
        if not rows:
            break
        chunk_seq += 1
        _flush_chunk(
            rows=rows,
            pending_records=items,
            chunk_seq=chunk_seq,
            job_timestamp=f"{job_timestamp}_recover",
            output_dir=output_dir,
            store=store,
            push_cfg=push_cfg,
            push_store_factory=push_store_factory,
            result=result,
        )
        recovered += len(items)
    if recovered:
        logger.info("recovered unpushed parse records: count=%s chunks=%s", recovered, chunk_seq)
    return recovered


def _collect_pending_files(
    input_dir: Path,
    archive_dir: Path,
    store: ParseRecordStore,
    output_dir: Path,
    parser_cfg: ParserConfig,
    result: ParseRunResult,
) -> list[Path]:
    pending: list[Path] = []
    for path in scan_folder(input_dir, recursive=parser_cfg.recursive):
        file_path = str(path.resolve())
        file_mtime = path.stat().st_mtime
        if store.should_skip(file_path, file_mtime, path.name, output_dir):
            result.skipped += 1
            _move_to_archive(path, archive_dir)
            continue
        pending.append(path)
    return pending


def run_parse_job(
    parser_cfg: ParserConfig,
    accounts: tuple[AccountConfig, ...],
    store: ParseRecordStore,
    *,
    push_cfg: PushConfig | None = None,
    push_store_factory: Callable[[], PushRecordStore] | None = None,
) -> ParseRunResult:
    result = ParseRunResult()
    if not parser_cfg.model:
        result.error = "parser.model 未配置"
        return result
    if not parser_cfg.ark_api_key:
        result.error = "parser.ark_api_key 未配置（或设置环境变量 ARK_API_KEY）"
        return result

    lock_path = store.db_path.parent / "parse.lock"
    job_lock = _ParseJobLock(lock_path)
    if not job_lock.acquire():
        result.error = "已有解析任务在运行，本次跳过"
        logger.warning("%s", result.error)
        return result

    try:
        return _run_parse_job_locked(
            parser_cfg,
            accounts,
            store,
            push_cfg=push_cfg,
            push_store_factory=push_store_factory,
        )
    finally:
        job_lock.release()


def _run_parse_job_locked(
    parser_cfg: ParserConfig,
    accounts: tuple[AccountConfig, ...],
    store: ParseRecordStore,
    *,
    push_cfg: PushConfig | None = None,
    push_store_factory: Callable[[], PushRecordStore] | None = None,
) -> ParseRunResult:
    result = ParseRunResult()
    input_dir = Path(parser_cfg.input_path)
    output_dir = Path(parser_cfg.output_path)
    archive_dir = Path(parser_cfg.archive_path)
    if not input_dir.exists():
        result.error = f"解析输入目录不存在: {input_dir}"
        return result

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    chunk_size = max(1, parser_cfg.chunk_size)
    recovered = _recover_unpushed_chunks(
        store=store,
        output_dir=output_dir,
        chunk_size=chunk_size,
        job_timestamp=timestamp,
        push_cfg=push_cfg,
        push_store_factory=push_store_factory,
        result=result,
    )

    pending_files = _collect_pending_files(
        input_dir, archive_dir, store, output_dir, parser_cfg, result,
    )
    if not pending_files:
        clear_progress(store.db_path)
        if recovered:
            logger.info("parse job: recovered %s unpushed records, no files in input", recovered)
        elif result.skipped:
            logger.info("parse job: nothing to parse, archived %s skipped files", result.skipped)
        return result

    processed = 0
    pending_total = len(pending_files)
    result.total = pending_total
    write_progress(store.db_path, {
        "pending": pending_total,
        "processed": 0,
        "ok": 0,
        "failed": 0,
        "current_file": "",
        "active_files": [],
        "chunk_size": chunk_size,
        "chunks_done": 0,
        "chunk_in_progress": 0,
    })

    client = make_client(parser_cfg)
    deps = Deps(
        client,
        parser_cfg.model,
        parser_cfg.max_retries,
        parser_cfg.request_timeout,
    )
    current_year = datetime.now().year

    rows: list[list] = []
    pending_records: list[dict] = []
    chunk_seq = 0
    rescan_round = 0

    def _write_progress_state(*, active_files: list[str] | None = None) -> None:
        files = list(active_files) if active_files is not None else []
        write_progress(store.db_path, {
            "pending": pending_total,
            "processed": processed,
            "ok": result.ok,
            "failed": result.failed,
            "current_file": files[0] if files else "",
            "active_files": files,
            "chunk_size": chunk_size,
            "chunks_done": chunk_seq,
            "chunk_in_progress": len(pending_records),
        })

    def _maybe_flush_chunk() -> None:
        nonlocal chunk_seq, rows, pending_records
        if len(pending_records) < chunk_size:
            return
        chunk_seq += 1
        _flush_chunk(
            rows=rows,
            pending_records=pending_records,
            chunk_seq=chunk_seq,
            job_timestamp=timestamp,
            output_dir=output_dir,
            store=store,
            push_cfg=push_cfg,
            push_store_factory=push_store_factory,
            result=result,
        )
        rows = []
        pending_records = []

    while rescan_round < MAX_PARSE_RESCAN_ROUNDS:
        if rescan_round > 0:
            pending_files = _collect_pending_files(
                input_dir, archive_dir, store, output_dir, parser_cfg, result,
            )
            if not pending_files:
                break
            pending_total += len(pending_files)
            result.total += len(pending_files)
            logger.info(
                "parse rescan round %s: %s new files (total pending=%s)",
                rescan_round + 1,
                len(pending_files),
                pending_total,
            )
            _write_progress_state()

        rescan_round += 1

        rows, pending_records, processed = _process_pending_files(
            pending_files,
            parser_cfg=parser_cfg,
            deps=deps,
            current_year=current_year,
            accounts=accounts,
            archive_dir=archive_dir,
            output_dir=output_dir,
            store=store,
            push_cfg=push_cfg,
            push_store_factory=push_store_factory,
            result=result,
            rows=rows,
            pending_records=pending_records,
            processed=processed,
            write_progress_state=_write_progress_state,
            maybe_flush_chunk=_maybe_flush_chunk,
        )

        if pending_records:
            chunk_seq += 1
            _flush_chunk(
                rows=rows,
                pending_records=pending_records,
                chunk_seq=chunk_seq,
                job_timestamp=timestamp,
                output_dir=output_dir,
                store=store,
                push_cfg=push_cfg,
                push_store_factory=push_store_factory,
                result=result,
            )
            rows = []
            pending_records = []

    if chunk_seq or result.excel_path:
        logger.info(
            "parse job done: batch=%s ok=%s failed=%s archived_skip=%s chunks=%s last_excel=%s",
            result.total,
            result.ok,
            result.failed,
            result.skipped,
            chunk_seq,
            result.excel_path,
        )
    clear_progress(store.db_path)
    return result
