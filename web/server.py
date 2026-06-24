"""邮箱简历抓取 — Web 管理后台 API 服务"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import read_account_job_display_id, write_account_job_display_id
from src.imap_suggest import suggest_imap
from src.parser_web import (
    init_parser_web,
    parser_scheduler_loop,
    register_parser_routes,
)
from src.push_web import init_push_web, register_push_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("server")

# ── 路径常量 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "data" / "processed.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"
PYTHON_BIN = sys.executable
MAIN_SCRIPT = BASE_DIR / "main.py"
BACKFILL_SCRIPT = BASE_DIR / "scripts" / "backfill.py"

# ── 全局状态 ──────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")
CORS(app)

# 运行状态：idle / running / error
_run_state = {
    "status": "idle",           # idle | running | error
    "account": None,            # 正在运行的账号名
    "started_at": None,         # ISO 时间
    "message": "等待执行",       # 状态描述
    "last_result": None,        # 上次运行结果
}
_run_lock = threading.Lock()

# 定时调度状态
_scheduler_state = {
    "enabled": False,
    "last_run_at": None,        # 上次自动执行时间
    "next_run_at": None,        # 下次预计执行时间
    "last_accounts": [],         # 上次执行的账号列表
}
_scheduler_lock = threading.Lock()


# ══════════════════════════════════════════════════════════
#  定时调度器
# ══════════════════════════════════════════════════════════
def _load_scheduler_config() -> dict:
    """读取 scheduler.json 配置"""
    try:
        if SCHEDULER_PATH.exists():
            data = json.loads(SCHEDULER_PATH.read_text(encoding="utf-8"))
            data.setdefault("minute", 0)
            data.setdefault("interval_minutes", 30)
            data.setdefault("enabled", False)
            data.setdefault("accounts", [])
            return data
    except Exception as e:
        log.warning(f"读取调度配置失败: {e}")
    return {"enabled": False, "interval_minutes": 30, "minute": 0, "accounts": []}


def _calc_next_run_time(interval_minutes: int, minute_offset: int) -> datetime:
    """计算下一次执行时间。

    规则：在每小时内，合法的执行分钟为 minute_offset, minute_offset+interval, ...
    找到 >= now 的下一个合法时间点。
    """
    now = datetime.now().astimezone()
    # 当前小时内所有合法分钟
    valid_minutes = []
    m = minute_offset % 60
    while m < 60:
        valid_minutes.append(m)
        m += interval_minutes
        if m >= 60:
            break
    # 检查当前小时是否有可用的
    for vm in valid_minutes:
        candidate = now.replace(minute=vm, second=0, microsecond=0)
        if candidate > now:
            return candidate
    # 当前小时没有了，取下一个小时的第一个
    next_hour = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=1))
    return next_hour.replace(minute=valid_minutes[0] if valid_minutes else 0)


def _is_time_to_run(now: datetime, interval_minutes: int, minute_offset: int) -> bool:
    """判断当前时间是否到达执行点。"""
    valid_minutes = []
    m = minute_offset % 60
    while m < 60:
        valid_minutes.append(m)
        m += interval_minutes
        if m >= 60:
            break
    # 当前分钟匹配 + 秒数 < 30（防止同一分钟重复触发）
    return now.minute in valid_minutes and now.second < 30


def _scheduler_loop() -> None:
    """后台调度线程主循环"""
    log.info("调度线程已启动")
    last_triggered_minute = -1  # 防止同一分钟重复触发

    while True:
        try:
            cfg = _load_scheduler_config()
            enabled = cfg.get("enabled", False)

            with _scheduler_lock:
                _scheduler_state["enabled"] = enabled

            if not enabled:
                with _scheduler_lock:
                    _scheduler_state["next_run_at"] = None
                time.sleep(10)
                continue

            interval = max(5, cfg.get("interval_minutes", 30))
            minute_offset = cfg.get("minute", 0) % 60
            accounts = cfg.get("accounts", [])

            now = datetime.now().astimezone()

            # 计算并更新下次执行时间
            next_run = _calc_next_run_time(interval, minute_offset)
            with _scheduler_lock:
                _scheduler_state["next_run_at"] = next_run.isoformat()

            # 判断是否到时间
            should_run = (
                _is_time_to_run(now, interval, minute_offset)
                and now.minute != last_triggered_minute
            )

            if should_run:
                # 检查是否有任务在跑
                with _run_lock:
                    if _run_state["status"] == "running":
                        log.info("调度触发但已有任务在运行，跳过")
                        time.sleep(30)
                        continue

                last_triggered_minute = now.minute
                log.info(f"调度触发: interval={interval}, minute={minute_offset}, accounts={accounts}")

                # 按账号逐个执行（不用全部一起跑，避免超时）
                if accounts:
                    for acct in accounts:
                        with _scheduler_lock:
                            _scheduler_state["last_accounts"] = accounts
                        _run_fetch(acct)
                else:
                    # 没指定账号就跑全部
                    _run_fetch(None)

                with _scheduler_lock:
                    _scheduler_state["last_run_at"] = datetime.now().astimezone().isoformat()

                # 执行完再算下次时间
                next_run = _calc_next_run_time(interval, minute_offset)
                with _scheduler_lock:
                    _scheduler_state["next_run_at"] = next_run.isoformat()

                log.info(f"下次执行时间: {next_run.strftime('%H:%M:%S')}")

            time.sleep(5)  # 5 秒检查一次

        except Exception as e:
            log.error(f"调度线程异常: {e}")
            time.sleep(30)


# ══════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════
def _load_yaml() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_yaml(data: dict) -> None:
    CONFIG_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _default_download_path(data: dict) -> str:
    """新账号默认下载目录：优先 parser.input_path，其次已有账号的 download.path。"""
    parser = data.get("parser") or {}
    input_path = str(parser.get("input_path") or "").strip()
    if input_path:
        return input_path
    for acct in data.get("accounts", []):
        path = str((acct.get("download") or {}).get("path") or "").strip()
        if path:
            return path
    return "/Users/admin/Desktop/resume"


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _run_fetch(account_name: str | None) -> None:
    """在后台线程执行抓取"""
    with _run_lock:
        if _run_state["status"] == "running":
            return
        _run_state["status"] = "running"
        _run_state["account"] = account_name
        _run_state["started_at"] = datetime.now().astimezone().isoformat()
        _run_state["message"] = f"正在收取 {account_name or '全部账号'}..."

    try:
        cmd = [PYTHON_BIN, str(MAIN_SCRIPT), "run"]
        if account_name:
            cmd += ["--account", account_name]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            cwd=str(BASE_DIR),
        )
        with _run_lock:
            if result.returncode == 0:
                _run_state["status"] = "idle"
                _run_state["message"] = "收取完成"
                _run_state["last_result"] = result.stdout.strip()
            else:
                _run_state["status"] = "error"
                _run_state["message"] = f"执行出错: {result.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        with _run_lock:
            _run_state["status"] = "error"
            _run_state["message"] = "执行超时（10分钟）"
    except Exception as e:
        with _run_lock:
            _run_state["status"] = "error"
            _run_state["message"] = f"执行异常: {e}"
    finally:
        with _run_lock:
            _run_state["account"] = None
            _run_state["started_at"] = None


# ══════════════════════════════════════════════════════════
#  前端页面
# ══════════════════════════════════════════════════════════
@app.route("/")
def index():
    resp = send_from_directory(str(STATIC_DIR), "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ══════════════════════════════════════════════════════════
#  API — 邮箱配置
# ══════════════════════════════════════════════════════════
@app.route("/api/accounts/imap-suggest", methods=["GET"])
def imap_suggest():
    """根据邮箱地址推断 IMAP 服务器配置。"""
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"error": "缺少 email 参数"}), 400

    data = _load_yaml()
    existing = []
    for acct in data.get("accounts", []):
        imap = acct.get("imap") or {}
        existing.append({
            "username": imap.get("username", ""),
            "imap_host": imap.get("host", ""),
            "imap_port": imap.get("port", 993),
            "imap_ssl": imap.get("ssl", True),
        })

    result = suggest_imap(email, existing)
    if not result:
        return jsonify({"ok": False, "error": "邮箱格式无效"})
    return jsonify({"ok": True, **result})


@app.route("/api/accounts/default-download-path", methods=["GET"])
def default_download_path():
    """返回新账号默认附件下载目录。"""
    data = _load_yaml()
    return jsonify({"path": _default_download_path(data)})


@app.route("/api/accounts", methods=["GET"])
def list_accounts():
    """获取所有邮箱账号配置"""
    data = _load_yaml()
    accounts = []
    for acct in data.get("accounts", []):
        accounts.append({
            "name": acct.get("name", ""),
            "imap_host": acct.get("imap", {}).get("host", ""),
            "imap_port": acct.get("imap", {}).get("port", 993),
            "imap_ssl": acct.get("imap", {}).get("ssl", True),
            "username": acct.get("imap", {}).get("username", ""),
            "password": acct.get("imap", {}).get("password", ""),
            "mailbox": acct.get("mailbox", "INBOX"),
            "download_path": acct.get("download", {}).get("path", ""),
            "job_display_id": read_account_job_display_id(acct),
            "tenant_id": acct.get("tenant_id", ""),
            "tenant_code": acct.get("tenant_code", ""),
        })
    # 水位线
    conn = _db_conn()
    try:
        rows = conn.execute("SELECT account_name, since_uid FROM watermarks").fetchall()
        wm = {r["account_name"]: r["since_uid"] for r in rows}
    finally:
        conn.close()
    for a in accounts:
        a["watermark_uid"] = wm.get(a["name"], 0)
    return jsonify(accounts)


@app.route("/api/accounts", methods=["POST"])
def add_account():
    """添加邮箱账号"""
    body = request.json or {}
    required = ["name", "imap_host", "username", "password"]
    for k in required:
        if not body.get(k):
            return jsonify({"error": f"缺少必填字段: {k}"}), 400

    data = _load_yaml()
    names = [a["name"] for a in data.get("accounts", [])]
    if body["name"] in names:
        return jsonify({"error": f"账号名已存在: {body['name']}"}), 400

    download_path = str(body.get("download_path") or "").strip() or _default_download_path(data)

    new_acct = {
        "name": body["name"],
        "imap": {
            "host": body["imap_host"],
            "port": int(body.get("imap_port", 993)),
            "ssl": bool(body.get("imap_ssl", True)),
            "username": body["username"],
            "password": body["password"],
        },
        "mailbox": body.get("mailbox", "INBOX"),
        "download": {"path": download_path},
        "tenant_id": str(body.get("tenant_id") or "").strip(),
        "tenant_code": str(body.get("tenant_code") or "").strip(),
    }
    write_account_job_display_id(
        new_acct,
        str(body.get("job_display_id") or body.get("job_id") or ""),
    )
    data.setdefault("accounts", []).append(new_acct)
    _save_yaml(data)
    return jsonify({"ok": True, "name": body["name"]})


@app.route("/api/accounts/<name>", methods=["PUT"])
def update_account(name: str):
    """更新邮箱账号配置"""
    body = request.json or {}
    data = _load_yaml()
    for i, acct in enumerate(data.get("accounts", [])):
        if acct["name"] == name:
            if "imap_host" in body:
                acct["imap"]["host"] = body["imap_host"]
            if "imap_port" in body:
                acct["imap"]["port"] = int(body["imap_port"])
            if "imap_ssl" in body:
                acct["imap"]["ssl"] = bool(body["imap_ssl"])
            if "username" in body:
                acct["imap"]["username"] = body["username"]
            if "password" in body and body["password"]:
                acct["imap"]["password"] = body["password"]
            if "mailbox" in body:
                acct["mailbox"] = body["mailbox"]
            if "download_path" in body and str(body["download_path"] or "").strip():
                acct.setdefault("download", {})["path"] = body["download_path"]
            elif not str((acct.get("download") or {}).get("path") or "").strip():
                acct.setdefault("download", {})["path"] = _default_download_path(data)
            if "job_display_id" in body or "job_id" in body:
                write_account_job_display_id(
                    acct,
                    str(body.get("job_display_id") or body.get("job_id") or ""),
                )
            if "tenant_id" in body:
                acct["tenant_id"] = str(body["tenant_id"] or "").strip()
            if "tenant_code" in body:
                acct["tenant_code"] = str(body["tenant_code"] or "").strip()
            if "name" in body and body["name"] != name:
                acct["name"] = body["name"]
            data["accounts"][i] = acct
            _save_yaml(data)
            return jsonify({"ok": True})
    return jsonify({"error": f"账号不存在: {name}"}), 404


@app.route("/api/accounts/<name>", methods=["DELETE"])
def delete_account(name: str):
    """删除邮箱账号"""
    data = _load_yaml()
    before = len(data.get("accounts", []))
    data["accounts"] = [a for a in data.get("accounts", []) if a["name"] != name]
    if len(data["accounts"]) == before:
        return jsonify({"error": f"账号不存在: {name}"}), 404
    _save_yaml(data)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════
#  API — 主题 & 附件规则
# ══════════════════════════════════════════════════════════
@app.route("/api/rules", methods=["GET"])
def get_rules():
    data = _load_yaml()
    return jsonify({
        "subject_patterns": data.get("subject_patterns", []),
        "subject_exclude_patterns": data.get("subject_exclude_patterns", []),
        "attachment_extensions": data.get("attachment_extensions", []),
    })


@app.route("/api/rules", methods=["PUT"])
def update_rules():
    body = request.json or {}
    data = _load_yaml()
    if "subject_patterns" in body:
        data["subject_patterns"] = body["subject_patterns"]
    if "subject_exclude_patterns" in body:
        data["subject_exclude_patterns"] = body["subject_exclude_patterns"]
    if "attachment_extensions" in body:
        data["attachment_extensions"] = body["attachment_extensions"]
    _save_yaml(data)
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════
#  API — 运行状态 & 手动触发
# ══════════════════════════════════════════════════════════
@app.route("/api/status", methods=["GET"])
def get_status():
    with _run_lock:
        state = dict(_run_state)
    with _scheduler_lock:
        sched = dict(_scheduler_state)
    # 补充各账号统计
    conn = _db_conn()
    try:
        rows = conn.execute(
            "SELECT account_name, COUNT(*) as cnt, "
            "SUM(CASE WHEN matched=1 THEN 1 ELSE 0 END) as matched_cnt "
            "FROM processed_emails GROUP BY account_name"
        ).fetchall()
        stats = {r["account_name"]: {"total": r["cnt"], "matched": r["matched_cnt"]} for r in rows}
        wm_rows = conn.execute("SELECT account_name, since_uid FROM watermarks").fetchall()
        watermarks = {r["account_name"]: r["since_uid"] for r in wm_rows}
    finally:
        conn.close()
    state["account_stats"] = stats
    state["watermarks"] = watermarks
    state["scheduler"] = sched
    return jsonify(state)


@app.route("/api/sync", methods=["POST"])
def trigger_sync():
    """手动触发收取"""
    body = request.json or {}
    account = body.get("account")  # None = 全部
    with _run_lock:
        if _run_state["status"] == "running":
            return jsonify({"error": "已有收取任务在运行中"}), 409
    t = threading.Thread(target=_run_fetch, args=(account,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "已触发收取"})


# ══════════════════════════════════════════════════════════
#  API — 收取记录
# ══════════════════════════════════════════════════════════
@app.route("/api/records", methods=["GET"])
def list_records():
    """分页查询收取记录"""
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    account = request.args.get("account")
    matched_only = request.args.get("matched") == "1"
    unmatched_only = request.args.get("matched") == "0"
    keyword = request.args.get("keyword", "").strip()
    date_from = request.args.get("date_from", "").strip() or None
    date_to = request.args.get("date_to", "").strip() or None

    where_clauses = []
    params: list = []
    if account:
        where_clauses.append("account_name = ?")
        params.append(account)
    if matched_only:
        where_clauses.append("matched = 1")
    elif unmatched_only:
        where_clauses.append("matched = 0")
    if keyword:
        where_clauses.append("(subject LIKE ? OR saved_files LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    from src.date_filter import append_date_where
    append_date_where(where_clauses, params, "processed_at", date_from, date_to)

    where = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    conn = _db_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM processed_emails{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT account_name, message_id, subject, mail_date, "
            f"processed_at, saved_files, matched "
            f"FROM processed_emails{where} "
            f"ORDER BY processed_at DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        records = []
        for r in rows:
            files = []
            try:
                files = json.loads(r["saved_files"]) if r["saved_files"] else []
            except (json.JSONDecodeError, TypeError):
                files = []
            records.append({
                "account_name": r["account_name"],
                "message_id": r["message_id"],
                "subject": r["subject"],
                "mail_date": r["mail_date"],
                "processed_at": r["processed_at"],
                "saved_files": files,
                "matched": bool(r["matched"]),
            })
    finally:
        conn.close()
    return jsonify({
        "records": records,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
    })


# ══════════════════════════════════════════════════════════
#  API — 定时任务（读写本地 scheduler.json）
# ══════════════════════════════════════════════════════════
SCHEDULER_PATH = BASE_DIR / "data" / "scheduler.json"


@app.route("/api/scheduler", methods=["GET"])
def get_scheduler():
    if SCHEDULER_PATH.exists():
        data = json.loads(SCHEDULER_PATH.read_text(encoding="utf-8"))
        # 兼容旧数据：补充 minute 默认值
        if "minute" not in data:
            data["minute"] = 0
        return jsonify(data)
    return jsonify({"enabled": False, "interval_minutes": 30, "minute": 0, "accounts": []})


@app.route("/api/scheduler", methods=["PUT"])
def update_scheduler():
    body = request.json or {}
    SCHEDULER_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULER_PATH.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


init_parser_web(
    base_dir=BASE_DIR,
    config_path=CONFIG_PATH,
    db_path=DB_PATH,
    main_script=MAIN_SCRIPT,
    python_bin=PYTHON_BIN,
)
register_parser_routes(app, load_yaml=_load_yaml, save_yaml=_save_yaml)
init_push_web(config_path=CONFIG_PATH, db_path=DB_PATH)
register_push_routes(app, load_yaml=_load_yaml, save_yaml=_save_yaml)


# ══════════════════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"📡 邮箱简历管理后台启动中...")
    print(f"   配置文件: {CONFIG_PATH}")
    print(f"   数据库:   {DB_PATH}")
    print(f"   访问地址: http://localhost:5100")
    sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    sched_thread.start()
    parser_sched_thread = threading.Thread(target=parser_scheduler_loop, daemon=True)
    parser_sched_thread.start()
    print(f"   邮箱收取调度线程已启动")
    print(f"   简历解析调度线程已启动")
    app.run(host="0.0.0.0", port=5100, debug=False)
