#!/usr/bin/env python3
"""补全推送 payload / 解析记录中缺失的姓名，并重推近 N 天因姓名失败/部分失败的批次。"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.push_orchestrator import retry_push_batches
from src.push_store import PushRecordStore


def has_name_error(resp: dict) -> bool:
    for err in resp.get("errors") or []:
        msg = str(err.get("msg") or err)
        if "姓名" in msg or "name" in msg.lower():
            return True
    for item in resp.get("failures") or []:
        err = str(item.get("error") or item)
        if "姓名" in err or "name" in err.lower():
            return True
    return False


def fix_candidate(c: dict) -> bool:
    if str(c.get("name") or "").strip():
        return False
    nick = str(c.get("channel_nickname") or "").strip()
    if not nick:
        return False
    c["name"] = nick
    return True


def _phone_from_record(pr: dict) -> str:
    phone = str(pr.get("phone") or "").strip()
    parsed = pr.get("parsed_json")
    if not phone and isinstance(parsed, dict):
        phone = str(parsed.get("phone") or "").strip()
    return phone


def _job_from_record(pr: dict) -> str:
    job = str(pr.get("job_display_id") or pr.get("job_id") or "").strip()
    parsed = pr.get("parsed_json")
    if not job and isinstance(parsed, dict):
        job = str(parsed.get("job_display_id") or parsed.get("job_id") or "").strip()
    return job


def match_parse_record(
    *,
    phone: str,
    job: str,
    by_phone_job: dict[tuple[str, str], dict],
    by_phone: dict[str, list[dict]],
) -> dict | None:
    if phone and job and (phone, job) in by_phone_job:
        return by_phone_job[(phone, job)]
    matches = by_phone.get(phone) or []
    if len(matches) == 1:
        return matches[0]
    if phone and job:
        for m in matches:
            if _job_from_record(m) == job:
                return m
    return None


def update_json_archive(output_dir: Path, source_file: str, name: str, nickname: str) -> bool:
    json_dir = output_dir / "json"
    if not json_dir.is_dir() or not source_file:
        return False
    stem = Path(source_file).stem
    paths = [json_dir / f"{stem}.json", *json_dir.glob(f"{stem}(*).json")]
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or str(data.get("name") or "").strip():
            continue
        data["name"] = name
        if nickname and not str(data.get("channel_nickname") or "").strip():
            data["channel_nickname"] = nickname
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    db_path = args.config.parent / "data" / "processed.db"
    output_dir = Path(cfg.parser.output_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, status, request_payload, response_body
        FROM push_batches
        WHERE status IN ('failed', 'partial')
          AND created_at >= datetime('now', ?)
        ORDER BY id
        """,
        (f"-{args.days} days",),
    ).fetchall()

    parse_rows = conn.execute(
        """
        SELECT id, source_file, job_id, parsed_json
        FROM parse_records
        WHERE status LIKE '成功%' AND parsed_json IS NOT NULL AND parsed_json != ''
        """
    ).fetchall()
    parse_rows = [dict(r) for r in parse_rows]
    for pr in parse_rows:
        try:
            pr["parsed_json"] = json.loads(pr["parsed_json"])
        except json.JSONDecodeError:
            pr["parsed_json"] = None
        pr["job_display_id"] = pr.get("job_id") or ""

    by_phone_job: dict[tuple[str, str], dict] = {}
    by_phone: dict[str, list[dict]] = {}
    for pr in parse_rows:
        phone = _phone_from_record(pr)
        job = _job_from_record(pr)
        if phone:
            by_phone.setdefault(phone, []).append(pr)
            if job:
                by_phone_job[(phone, job)] = pr

    affected_batch_ids: list[int] = []
    payload_fixes = 0
    parse_db_updates = 0
    json_updates = 0

    for row in rows:
        payload = json.loads(row["request_payload"] or "{}")
        resp = json.loads(row["response_body"] or "{}") if row["response_body"] else {}
        cands = payload.get("candidates") or []
        fixed_count = sum(1 for c in cands if fix_candidate(c))
        if not fixed_count:
            continue
        if not has_name_error(resp):
            continue
        affected_batch_ids.append(row["id"])
        payload_fixes += fixed_count
        if args.dry_run:
            continue
        conn.execute(
            "UPDATE push_batches SET request_payload = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), row["id"]),
        )
        for c in cands:
            name = str(c.get("name") or "").strip()
            phone = str(c.get("phone") or "").strip()
            job = str(c.get("job_display_id") or "").strip()
            if not name or not phone:
                continue
            pr = match_parse_record(
                phone=phone,
                job=job,
                by_phone_job=by_phone_job,
                by_phone=by_phone,
            )
            if not pr:
                continue
            parsed = pr.get("parsed_json")
            if not isinstance(parsed, dict):
                continue
            if str(parsed.get("name") or "").strip():
                continue
            source_file = str(pr.get("source_file") or parsed.get("_source_file") or "")
            updated = dict(parsed)
            updated["name"] = name
            nick = str(c.get("channel_nickname") or "").strip()
            if nick:
                updated["channel_nickname"] = nick
            conn.execute(
                "UPDATE parse_records SET parsed_json = ? WHERE id = ?",
                (json.dumps(updated, ensure_ascii=False), pr["id"]),
            )
            parse_db_updates += 1
            if update_json_archive(output_dir, source_file, name, str(c.get("channel_nickname") or "")):
                json_updates += 1

    if not args.dry_run:
        conn.commit()
    conn.close()

    print(f"days={args.days} affected_batches={len(affected_batch_ids)} ids={affected_batch_ids}")
    print(f"payload_candidates_with_name={payload_fixes} parse_db_updates={parse_db_updates} json_updates={json_updates}")
    if args.dry_run:
        return 0
    if not affected_batch_ids:
        return 0

    push_store = PushRecordStore(db_path)
    try:
        results = retry_push_batches(
            push_store,
            cfg.push,
            affected_batch_ids,
            refresh_tenant=True,
            config_path=args.config,
            db_path=db_path,
        )
    finally:
        push_store.close()

    ok = sum(1 for r in results if r.get("status") == "success")
    partial = sum(1 for r in results if r.get("status") == "partial")
    failed = sum(1 for r in results if r.get("status") == "failed")
    print(f"retry: success={ok} partial={partial} failed={failed}")
    for r in results:
        if r.get("status") in ("failed", "partial"):
            print(f"  batch {r['batch_id']}: {r.get('status')} {r.get('error') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
