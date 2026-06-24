"""候选人推送 Web API。"""
from __future__ import annotations

from pathlib import Path

from flask import jsonify, request

from src.config import load_config, ConfigError
from src.push_client import _normalize_bearer_token
from src.push_response import build_candidate_results
from src.push_store import PushRecordStore
from src.parse_store import ParseRecordStore
from src.push_accounts import (
    backfill_push_account_meta,
    build_candidate_account_index,
    lookup_account_for_candidate,
    meta_account_by_index,
)
from src.push_orchestrator import retry_push_batches

CONFIG_PATH: Path | None = None
DB_PATH: Path | None = None


def init_push_web(*, config_path: Path, db_path: Path) -> None:
    global CONFIG_PATH, DB_PATH
    CONFIG_PATH = config_path
    DB_PATH = db_path


def _load_push_config():
    if not CONFIG_PATH:
        return None
    try:
        cfg = load_config(CONFIG_PATH)
        return cfg.push
    except ConfigError:
        return None


def register_push_routes(app, *, load_yaml, save_yaml) -> None:
    @app.route("/api/push/config", methods=["GET"])
    def get_push_config():
        data = load_yaml()
        push = data.get("push") or {}
        return jsonify({
            "enabled": bool(push.get("enabled", False)),
            "api_url": push.get("api_url", ""),
            "bearer_token": _normalize_bearer_token(push.get("bearer_token", "")),
            "timeout": int(push.get("timeout", 60)),
            "host_header": push.get("host_header", ""),
            "verify_ssl": bool(push.get("verify_ssl", True)),
        })

    @app.route("/api/push/config", methods=["PUT"])
    def update_push_config():
        body = request.json or {}
        data = load_yaml()
        push = dict(data.get("push") or {})
        for key in ("enabled", "api_url", "timeout", "host_header"):
            if key in body:
                push[key] = body[key]
        if "verify_ssl" in body:
            push["verify_ssl"] = bool(body["verify_ssl"])
        if "bearer_token" in body:
            push["bearer_token"] = _normalize_bearer_token(str(body["bearer_token"] or ""))
        data["push"] = push
        save_yaml(data)
        return jsonify({"ok": True})

    @app.route("/api/push/records", methods=["GET"])
    def list_push_records():
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 20))
        status = request.args.get("status", "").strip() or None
        date_from = request.args.get("date_from", "").strip() or None
        date_to = request.args.get("date_to", "").strip() or None
        store = PushRecordStore(DB_PATH)
        try:
            records, total = store.list_batches(
                page=page,
                per_page=per_page,
                status=status,
                date_from=date_from,
                date_to=date_to,
            )
            stats = store.get_stats()
        finally:
            store.close()
        return jsonify({
            "records": records,
            "stats": stats,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page if total else 0,
        })

    @app.route("/api/push/records/<int:batch_id>", methods=["GET"])
    def get_push_record(batch_id: int):
        store = PushRecordStore(DB_PATH)
        try:
            batch = store.get_batch(batch_id)
        finally:
            store.close()
        if not batch:
            return jsonify({"error": "记录不存在"}), 404
        cand_index: dict[tuple[str, ...], str] = {}
        if DB_PATH:
            parse_store = ParseRecordStore(DB_PATH)
            try:
                cand_index = build_candidate_account_index(
                    parse_store.list_success_with_parsed()
                )
            finally:
                parse_store.close()
        candidate_results = build_candidate_results(
            batch.get("request_payload"),
            batch.get("response_summary"),
            cand_index=cand_index,
            lookup_account=lookup_account_for_candidate,
            meta_accounts=meta_account_by_index(batch.get("request_payload")),
        )
        return jsonify({"record": batch, "candidate_results": candidate_results})

    @app.route("/api/push/backfill-account-meta", methods=["POST"])
    def backfill_push_account_meta_api():
        if not DB_PATH:
            return jsonify({"error": "数据库未配置"}), 500
        result = backfill_push_account_meta(
            db_path=DB_PATH,
            config_path=CONFIG_PATH,
        )
        return jsonify({"ok": True, **result})

    @app.route("/api/push/retry", methods=["POST"])
    def retry_push():
        body = request.json or {}
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"error": "ids 必须为非空数组"}), 400
        refresh_tenant = body.get("refresh_tenant", True)
        push_cfg = _load_push_config()
        if not push_cfg or not push_cfg.enabled:
            return jsonify({"error": "推送未启用"}), 400
        store = PushRecordStore(DB_PATH)
        try:
            results = retry_push_batches(
                store,
                push_cfg,
                [int(i) for i in ids],
                refresh_tenant=bool(refresh_tenant),
                config_path=CONFIG_PATH,
                db_path=DB_PATH,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            store.close()
        ok_cnt = sum(1 for r in results if r.get("status") == "success")
        refreshed = "（已使用最新租户名）" if refresh_tenant else ""
        return jsonify({
            "ok": True,
            "results": results,
            "message": f"重推完成{refreshed}：成功 {ok_cnt}/{len(results)}",
        })

    @app.route("/api/push/retry-all-failed", methods=["POST"])
    def retry_all_failed_push():
        body = request.json or {}
        refresh_tenant = body.get("refresh_tenant", True)
        push_cfg = _load_push_config()
        if not push_cfg or not push_cfg.enabled:
            return jsonify({"error": "推送未启用"}), 400
        store = PushRecordStore(DB_PATH)
        try:
            ids = store.list_failed_batch_ids()
            if not ids:
                return jsonify({"ok": True, "results": [], "message": "没有可重推的失败记录"})
            results = retry_push_batches(
                store,
                push_cfg,
                ids,
                refresh_tenant=bool(refresh_tenant),
                config_path=CONFIG_PATH,
                db_path=DB_PATH,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            store.close()
        ok_cnt = sum(1 for r in results if r.get("status") == "success")
        return jsonify({
            "ok": True,
            "results": results,
            "message": f"已按最新租户重推全部失败记录：成功 {ok_cnt}/{len(results)}",
        })
