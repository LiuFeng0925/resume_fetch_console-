"""简历解析 Web API 与后台调度逻辑。"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import jsonify, request

from resume_parser import schema as resume_schema
from src.config import (
    read_account_job_display_id,
    write_account_job_display_id,
    MAX_PARSER_CONCURRENCY,
    clamp_parser_concurrency,
    load_config,
)
from src.parse_retry import prepare_failed_reparses
from src.parse_lock_util import clear_stale_parse_lock, is_parse_lock_active
from resume_parser.field_labels import build_detail_sections
from src.parse_progress import read_progress
from src.parse_store import ParseRecordStore

log = logging.getLogger("parser")

PARSER_SCHEDULER_PATH: Path | None = None
BASE_DIR: Path | None = None
CONFIG_PATH: Path | None = None
DB_PATH: Path | None = None
MAIN_SCRIPT: Path | None = None
PARSE_PYTHON_BIN: Path | None = None

_parser_run_state = {
    "status": "idle",
    "started_at": None,
    "message": "等待执行",
    "last_result": None,
    "queue_size": 0,
}
_parser_run_lock = threading.Lock()
_parse_queue: list[str] = []
_parse_queue_lock = threading.Lock()
_parse_worker_lock = threading.Lock()
_parse_worker_started = False
_parse_coalesce_pending = False

_parser_scheduler_state = {
    "enabled": False,
    "last_run_at": None,
    "next_run_at": None,
}
_parser_scheduler_lock = threading.Lock()


def init_parser_web(
    *,
    base_dir: Path,
    config_path: Path,
    db_path: Path,
    main_script: Path,
    python_bin: Path | str,
) -> None:
    global PARSER_SCHEDULER_PATH, BASE_DIR, CONFIG_PATH, DB_PATH, MAIN_SCRIPT, PARSE_PYTHON_BIN
    BASE_DIR = base_dir
    CONFIG_PATH = config_path
    DB_PATH = db_path
    MAIN_SCRIPT = main_script
    PARSER_SCHEDULER_PATH = base_dir / "data" / "parser_scheduler.json"
    legacy_venv = base_dir.parent / "简历解析" / ".venv" / "bin" / "python3"
    PARSE_PYTHON_BIN = Path(legacy_venv) if legacy_venv.exists() else Path(python_bin)
    _start_parse_worker()


def _load_parser_scheduler_config() -> dict:
    try:
        if PARSER_SCHEDULER_PATH and PARSER_SCHEDULER_PATH.exists():
            data = json.loads(PARSER_SCHEDULER_PATH.read_text(encoding="utf-8"))
            data.setdefault("minute", 0)
            data.setdefault("interval_minutes", 60)
            data.setdefault("enabled", False)
            return data
    except Exception as e:
        log.warning("读取解析调度配置失败: %s", e)
    return {"enabled": False, "interval_minutes": 60, "minute": 0}


def _calc_next_run_time(interval_minutes: int, minute_offset: int) -> datetime:
    now = datetime.now().astimezone()
    valid_minutes = []
    m = minute_offset % 60
    while m < 60:
        valid_minutes.append(m)
        m += interval_minutes
        if m >= 60:
            break
    for vm in valid_minutes:
        candidate = now.replace(minute=vm, second=0, microsecond=0)
        if candidate > now:
            return candidate
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return next_hour.replace(minute=valid_minutes[0] if valid_minutes else 0)


def _is_time_to_run(now: datetime, interval_minutes: int, minute_offset: int) -> bool:
    valid_minutes = []
    m = minute_offset % 60
    while m < 60:
        valid_minutes.append(m)
        m += interval_minutes
        if m >= 60:
            break
    return now.minute in valid_minutes and now.second < 30


def _pending_parse_count() -> int:
    with _parse_queue_lock:
        pending = len(_parse_queue)
    with _parser_run_lock:
        if _parse_coalesce_pending:
            pending += 1
    return pending


def _execute_parse_once() -> None:
    with _parser_run_lock:
        _parser_run_state["status"] = "running"
        _parser_run_state["started_at"] = datetime.now().astimezone().isoformat()
        _parser_run_state["message"] = "正在解析简历..."

    lock_skip = "已有解析任务在运行"
    try:
        cmd = [str(PARSE_PYTHON_BIN), str(MAIN_SCRIPT), "--config", str(CONFIG_PATH), "parse"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600, cwd=str(BASE_DIR),
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "未知错误").strip()
            if lock_skip in err:
                clear_stale_parse_lock(DB_PATH.parent / "parse.lock")
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=3600, cwd=str(BASE_DIR),
                )
                if proc.returncode != 0:
                    err = (proc.stderr or proc.stdout or "未知错误").strip()
            if proc.returncode != 0 and lock_skip in err:
                with _parser_run_lock:
                    _parser_run_state["status"] = "idle"
                    _parser_run_state["message"] = (
                        "另一解析进程正在运行，重试文件将在其完成后自动处理"
                    )
                return
        with _parser_run_lock:
            if proc.returncode == 0:
                _parser_run_state["status"] = "idle"
                _parser_run_state["message"] = proc.stdout.strip().split("\n")[-1] or "解析完成"
                _parser_run_state["last_result"] = proc.stdout.strip()
            else:
                _parser_run_state["status"] = "error"
                err = (proc.stderr or proc.stdout or "未知错误").strip()
                _parser_run_state["message"] = f"执行出错: {err[:300]}"
    except subprocess.TimeoutExpired:
        with _parser_run_lock:
            _parser_run_state["status"] = "error"
            _parser_run_state["message"] = "执行超时（60分钟）"
    except Exception as e:
        log.exception("parse run failed")
        with _parser_run_lock:
            _parser_run_state["status"] = "error"
            _parser_run_state["message"] = f"解析异常: {e}"
    finally:
        with _parser_run_lock:
            _parser_run_state["started_at"] = None
            if _parser_run_state["status"] == "running":
                _parser_run_state["status"] = "idle"


def _parse_worker_loop() -> None:
    """串行消费解析队列：上一轮完成后自动执行排队任务（合并重复触发）。"""
    global _parse_coalesce_pending
    while True:
        with _parse_queue_lock:
            if not _parse_queue:
                with _parser_run_lock:
                    if _parser_run_state["status"] == "running":
                        _parser_run_state["status"] = "idle"
                    _parser_run_state["queue_size"] = 0
                time.sleep(0.5)
                continue
            _parse_queue.pop(0)
            remaining = len(_parse_queue)
        with _parser_run_lock:
            follow_up = remaining + (1 if _parse_coalesce_pending else 0)
            _parser_run_state["queue_size"] = follow_up
            if follow_up > 0:
                _parser_run_state["message"] = (
                    f"正在解析，后续还有 {follow_up} 个合并任务"
                )
        log.info("parse worker start, remaining in queue: %s", remaining)
        _execute_parse_once()
        with _parse_queue_lock:
            if _parse_coalesce_pending and not _parse_queue:
                _parse_coalesce_pending = False
                _parse_queue.append("coalesced")
                log.info("parse worker: running coalesced follow-up job")


def _start_parse_worker() -> None:
    global _parse_worker_started
    with _parse_worker_lock:
        if _parse_worker_started:
            return
        _parse_worker_started = True
        threading.Thread(target=_parse_worker_loop, daemon=True).start()
        log.info("解析队列工作线程已启动")


def enqueue_parse(source: str = "manual") -> int:
    """将解析任务加入队列；若已在跑或已有排队，则合并为一次后续任务。"""
    global _parse_coalesce_pending
    _start_parse_worker()
    with _parse_queue_lock:
        with _parser_run_lock:
            running = _parser_run_state["status"] == "running"
        if running or _parse_queue:
            _parse_coalesce_pending = True
        else:
            _parse_queue.append(source)
        pending = len(_parse_queue) + (1 if _parse_coalesce_pending else 0)
    with _parser_run_lock:
        _parser_run_state["queue_size"] = max(0, pending - (1 if running else 0))
        if _parse_coalesce_pending and running:
            _parser_run_state["message"] = (
                f"任务已合并，当前批次完成后继续（待执行 {pending}）"
            )
        elif pending == 1 and not running:
            _parser_run_state["message"] = "即将开始解析..."
        elif running:
            _parser_run_state["message"] = f"任务已排队，待执行 {pending} 个"
    return pending


def parser_scheduler_loop() -> None:
    log.info("简历解析调度线程已启动")
    last_triggered_minute = -1
    while True:
        try:
            cfg = _load_parser_scheduler_config()
            enabled = cfg.get("enabled", False)
            with _parser_scheduler_lock:
                _parser_scheduler_state["enabled"] = enabled

            if not enabled:
                with _parser_scheduler_lock:
                    _parser_scheduler_state["next_run_at"] = None
                time.sleep(10)
                continue

            interval = max(5, cfg.get("interval_minutes", 60))
            minute_offset = cfg.get("minute", 0) % 60
            now = datetime.now().astimezone()
            next_run = _calc_next_run_time(interval, minute_offset)
            with _parser_scheduler_lock:
                _parser_scheduler_state["next_run_at"] = next_run.isoformat()

            should_run = (
                _is_time_to_run(now, interval, minute_offset)
                and now.minute != last_triggered_minute
            )
            if should_run:
                last_triggered_minute = now.minute
                log.info("解析调度触发: interval=%s minute=%s", interval, minute_offset)
                enqueue_parse("scheduler")
                with _parser_scheduler_lock:
                    _parser_scheduler_state["last_run_at"] = datetime.now().astimezone().isoformat()
                next_run = _calc_next_run_time(interval, minute_offset)
                with _parser_scheduler_lock:
                    _parser_scheduler_state["next_run_at"] = next_run.isoformat()
            time.sleep(5)
        except Exception as e:
            log.error("解析调度线程异常: %s", e)
            time.sleep(30)


def get_parser_status_payload() -> dict:
    with _parser_run_lock:
        state = dict(_parser_run_state)
    with _parser_scheduler_lock:
        state["scheduler"] = dict(_parser_scheduler_state)
    store = ParseRecordStore(DB_PATH)
    try:
        state["stats"] = store.get_stats()
    finally:
        store.close()
    if state.get("status") == "running":
        progress = read_progress(DB_PATH)
        if progress:
            state["progress"] = progress
            pending = progress.get("pending", 0)
            parsed_now = progress.get("ok", 0) + progress.get("failed", 0)
            chunk_size = progress.get("chunk_size") or 0
            chunk_in = progress.get("chunk_in_progress") or 0
            chunks_done = progress.get("chunks_done") or 0
            state["message"] = f"本次解析 {parsed_now}/{pending}"
            if chunk_size:
                state["message"] += f" · 第 {chunks_done + 1} 组 {chunk_in}/{chunk_size}"
            if state.get("queue_size", 0) > 0:
                state["message"] += f" · 待执行 {state['queue_size']}"
            active = progress.get("active_files") or []
            if not active:
                current = progress.get("current_file")
                if current:
                    active = [current]
            if active:
                state["active_files"] = active
    return state


def register_parser_routes(app, *, load_yaml, save_yaml) -> None:
    @app.route("/api/jobs", methods=["GET"])
    def list_jobs():
        data = load_yaml()
        jobs = []
        for acct in data.get("accounts", []):
            jobs.append({
                "name": acct.get("name", ""),
                "username": acct.get("imap", {}).get("username", ""),
                "job_display_id": read_account_job_display_id(acct),
                "tenant_id": acct.get("tenant_id", ""),
                "tenant_code": acct.get("tenant_code", ""),
            })
        return jsonify(jobs)

    @app.route("/api/jobs", methods=["PUT"])
    def update_jobs():
        body = request.json or {}
        items = body.get("jobs")
        if not isinstance(items, list):
            return jsonify({"error": "jobs 必须为数组"}), 400
        data = load_yaml()
        job_map = {j.get("name"): j for j in items if j.get("name")}
        for acct in data.get("accounts", []):
            if acct["name"] in job_map:
                j = job_map[acct["name"]]
                write_account_job_display_id(acct, str(j.get("job_display_id") or j.get("job_id") or ""))
                acct["tenant_id"] = str(j.get("tenant_id") or "").strip()
                acct["tenant_code"] = str(j.get("tenant_code") or "").strip()
        save_yaml(data)
        return jsonify({"ok": True})

    @app.route("/api/jobs", methods=["POST"])
    def add_job():
        body = request.json or {}
        name = str(body.get("name") or "").strip()
        if not name:
            return jsonify({"error": "请选择邮箱账号"}), 400
        data = load_yaml()
        acct = next((a for a in data.get("accounts", []) if a.get("name") == name), None)
        if not acct:
            return jsonify({"error": f"账号「{name}」不存在，请先在收取邮箱配置中添加"}), 404
        write_account_job_display_id(
            acct,
            str(body.get("job_display_id") or body.get("job_id") or ""),
        )
        acct["tenant_id"] = str(body.get("tenant_id") or "").strip()
        acct["tenant_code"] = str(body.get("tenant_code") or "").strip()
        save_yaml(data)
        return jsonify({"ok": True})

    @app.route("/api/jobs/<name>", methods=["DELETE"])
    def delete_job(name: str):
        data = load_yaml()
        acct = next((a for a in data.get("accounts", []) if a.get("name") == name), None)
        if not acct:
            return jsonify({"error": f"账号「{name}」不存在"}), 404
        write_account_job_display_id(acct, "")
        acct["tenant_id"] = ""
        acct["tenant_code"] = ""
        save_yaml(data)
        return jsonify({"ok": True})

    @app.route("/api/parser/config", methods=["GET"])
    def get_parser_config():
        data = load_yaml()
        parser = data.get("parser") or {}
        return jsonify({
            "input_path": parser.get("input_path", ""),
            "output_path": parser.get("output_path", ""),
            "archive_path": parser.get("archive_path", "/Users/admin/Desktop/resume-parsed"),
            "recursive": bool(parser.get("recursive", False)),
            "ark_base_url": parser.get("ark_base_url", ""),
            "model": parser.get("model", ""),
            "concurrency": clamp_parser_concurrency(parser.get("concurrency"), default=1),
            "request_timeout": int(parser.get("request_timeout", 60)),
            "max_retries": int(parser.get("max_retries", 1)),
            "file_attempts": int(parser.get("file_attempts", 3)),
            "text_truncate": int(parser.get("text_truncate", 6000)),
            "chunk_size": int(parser.get("chunk_size", 10)),
            "has_api_key": bool(parser.get("ark_api_key") or __import__("os").environ.get("ARK_API_KEY")),
            "max_concurrency": MAX_PARSER_CONCURRENCY,
        })

    @app.route("/api/parser/config", methods=["PUT"])
    def update_parser_config():
        body = request.json or {}
        if "concurrency" in body:
            try:
                concurrency = int(body["concurrency"])
            except (TypeError, ValueError):
                return jsonify({"error": "并发数必须是整数"}), 400
            if concurrency < 1:
                return jsonify({"error": "并发数至少为 1"}), 400
            if concurrency > MAX_PARSER_CONCURRENCY:
                return jsonify({
                    "error": (
                        f"并发数最多为 {MAX_PARSER_CONCURRENCY}。"
                        "并发过高容易触发模型接口限流（429），并增加解析失败风险。"
                    ),
                }), 400
        data = load_yaml()
        parser = dict(data.get("parser") or {})
        for key in (
            "input_path", "output_path", "archive_path", "recursive", "ark_base_url", "model",
            "concurrency", "request_timeout", "max_retries", "file_attempts", "text_truncate",
            "chunk_size",
        ):
            if key in body:
                parser[key] = body[key]
        if body.get("ark_api_key"):
            parser["ark_api_key"] = body["ark_api_key"]
        data["parser"] = parser
        save_yaml(data)
        return jsonify({"ok": True})

    @app.route("/api/parser/status", methods=["GET"])
    def parser_status():
        return jsonify(get_parser_status_payload())

    @app.route("/api/parser/run", methods=["POST"])
    def trigger_parse():
        queue_len = enqueue_parse("manual")
        if queue_len > 1:
            return jsonify({
                "ok": True,
                "queued": True,
                "queue_size": queue_len - 1,
                "message": f"已加入队列，前方还有 {queue_len - 1} 个任务",
            })
        return jsonify({"ok": True, "message": "已触发简历解析"})

    @app.route("/api/parser/scheduler", methods=["GET"])
    def get_parser_scheduler():
        if PARSER_SCHEDULER_PATH and PARSER_SCHEDULER_PATH.exists():
            data = json.loads(PARSER_SCHEDULER_PATH.read_text(encoding="utf-8"))
            data.setdefault("minute", 0)
            return jsonify(data)
        return jsonify({"enabled": False, "interval_minutes": 60, "minute": 0})

    @app.route("/api/parser/scheduler", methods=["PUT"])
    def update_parser_scheduler():
        body = request.json or {}
        PARSER_SCHEDULER_PATH.parent.mkdir(parents=True, exist_ok=True)
        PARSER_SCHEDULER_PATH.write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return jsonify({"ok": True})

    @app.route("/api/parser/records", methods=["GET"])
    def list_parser_records():
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 20))
        status = request.args.get("status")
        keyword = request.args.get("keyword", "").strip()
        candidate_name = request.args.get("candidate_name", "").strip()
        push_status = request.args.get("push_status", "").strip()
        date_from = request.args.get("date_from", "").strip() or None
        date_to = request.args.get("date_to", "").strip() or None
        job_display_id = (
            request.args.get("job_display_id", "").strip()
            or request.args.get("job_id", "").strip()
        )
        store = ParseRecordStore(DB_PATH)
        try:
            records, total = store.list_records(
                page=page,
                per_page=per_page,
                status=status or None,
                keyword=keyword or None,
                candidate_name=candidate_name or None,
                push_status=push_status or None,
                job_display_id=job_display_id or None,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            store.close()
        return jsonify({
            "records": records,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total else 0,
        })

    @app.route("/api/parser/records/retry", methods=["POST"])
    def retry_parser_records():
        body = request.json or {}
        raw_ids = body.get("record_ids") or []
        record_ids: list[int] = []
        for x in raw_ids:
            try:
                record_ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not record_ids:
            return jsonify({"ok": False, "error": "请选择至少一条记录"}), 400

        cfg = load_config(CONFIG_PATH)
        store = ParseRecordStore(DB_PATH)
        try:
            result = prepare_failed_reparses(
                store,
                record_ids,
                input_dir=Path(cfg.parser.input_path),
                archive_dir=Path(cfg.parser.archive_path),
            )
        finally:
            store.close()

        if result["prepared_count"] <= 0:
            return jsonify({
                **result,
                "ok": False,
                "error": "没有可重新解析的记录",
            }), 400

        lock_path = DB_PATH.parent / "parse.lock"
        parse_running = is_parse_lock_active(lock_path, db_path=DB_PATH)
        if parse_running:
            queue_len = 0
            msg = (
                f"已将 {result['prepared_count']} 份放回待解析目录，"
                "当前解析任务完成后将自动继续"
            )
        else:
            clear_stale_parse_lock(lock_path)
            queue_len = enqueue_parse("manual-retry")
            msg = f"已提交 {result['prepared_count']} 份失败简历重新解析"
            if queue_len > 1:
                msg += f"（队列中待执行 {queue_len - 1} 个任务）"
        if result.get("skipped"):
            msg += f"，跳过 {len(result['skipped'])} 条"
        if result.get("errors"):
            msg += f"，{len(result['errors'])} 条失败"
        return jsonify({
            **result,
            "ok": True,
            "message": msg,
            "queue_size": queue_len,
            "parse_running": parse_running,
        })

    @app.route("/api/parser/records/<int:record_id>", methods=["GET"])
    def get_parser_record_detail(record_id: int):
        store = ParseRecordStore(DB_PATH)
        try:
            record = store.get_record(record_id)
        finally:
            store.close()
        if not record:
            return jsonify({"error": "记录不存在"}), 404
        parsed = record.get("parsed_json") or {}
        if parsed:
            if not parsed.get("job_display_id") and record.get("job_display_id"):
                parsed = {**parsed, "job_display_id": record["job_display_id"]}
            parsed = resume_schema.migrate_job_id_to_display(parsed)
            if not parsed.get("tenant_id") and record.get("tenant_id"):
                parsed = {**parsed, "tenant_id": record["tenant_id"]}
            if not parsed.get("tenant_code") and record.get("tenant_code"):
                parsed = {**parsed, "tenant_code": record["tenant_code"]}
        return jsonify({
            "record": record,
            "sections": build_detail_sections(parsed) if parsed else [],
        })
