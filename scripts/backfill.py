#!/usr/bin/env python3
"""历史简历回填脚本。

利用 IMAP SEARCH 在服务端先按主题关键词过滤，只拉取匹配的邮件，
然后下载符合条件的附件。不影响 watermarks 表，复用 processed_emails 去重。

用法:
    python scripts/backfill.py --account brbc-recruit
    python scripts/backfill.py --account brbc-recruit --keywords "BOSS直聘" "泰安"
    python scripts/backfill.py --account brbc-recruit --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from pathlib import Path

import imaplib
import yaml

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.imap_client import send_imap_id

# ── CLI ────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="历史简历回填（不影响水位线）")
    p.add_argument("--config", type=Path, default=Path("config.yaml"))
    p.add_argument("--account", required=True, help="要回填的账号名")
    p.add_argument(
        "--keywords",
        nargs="+",
        default=["BOSS直聘", "泰安"],
        help="主题需同时包含的关键词（默认: BOSS直聘 泰安）",
    )
    p.add_argument("--dry-run", action="store_true", help="只扫描不下载，输出统计")
    p.add_argument("--batch-size", type=int, default=100, help="每批处理的邮件数")
    p.add_argument("--output", type=Path, default=None, help="自定义下载目录（覆盖配置文件）")
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="仅处理此日期及之后的邮件（YYYY-MM-DD，按邮件 Date 本地日期）",
    )
    p.add_argument(
        "--until",
        type=str,
        default=None,
        help="仅处理此日期及之前的邮件（YYYY-MM-DD，按邮件 Date 本地日期）",
    )
    return p.parse_args(argv)


# ── IMAP helpers ────────────────────────────────────────────────────────────

def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    chunks: list[str] = []
    for fragment, charset in decode_header(value):
        if isinstance(fragment, bytes):
            chunks.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(fragment)
    return "".join(chunks).strip()


def extract_message_id(msg: Message, uid: int | None = None) -> str:
    mid = msg.get("Message-ID")
    if mid:
        return mid.strip()
    if uid is not None:
        return f"<uid-{uid}@local.generated>"
    return "<unknown@local.generated>"


def parse_date(msg: Message) -> datetime:
    import email.utils
    date_hdr = msg.get("Date")
    if date_hdr:
        mail_date = email.utils.parsedate_to_datetime(date_hdr)
        if mail_date.tzinfo is None:
            mail_date = mail_date.replace(tzinfo=timezone.utc)
        return mail_date
    return datetime.now(timezone.utc)


def extract_attachments(msg: Message, allowed_exts: set[str]) -> list[dict]:
    results: list[dict] = []
    if not msg.is_multipart():
        return results
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_mime_header(filename)
        ext = Path(filename).suffix.lower()
        if ext and ext not in allowed_exts:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        results.append({"filename": filename, "content": payload})
    return results


# ── DB helpers ─────────────────────────────────────────────────────────────

class ProcessedDB:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    def is_processed(self, account_name: str, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE account_name = ? AND message_id = ?",
            (account_name, message_id),
        ).fetchone()
        return row is not None

    def mark_processed(
        self, account_name: str, message_id: str, subject: str,
        mail_date: datetime, saved_files: list[str], matched: bool,
    ) -> None:
        import json
        self._conn.execute(
            """INSERT OR REPLACE INTO processed_emails
               (account_name, message_id, subject, mail_date, processed_at, saved_files, matched)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                account_name,
                message_id,
                subject,
                mail_date.astimezone().isoformat(),
                datetime.now().astimezone().isoformat(),
                json.dumps(saved_files, ensure_ascii=False),
                1 if matched else 0,
            ),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()


# ── Core logic ─────────────────────────────────────────────────────────────

def load_account_config(config_path: Path, account_name: str) -> dict:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for acct in raw.get("accounts", []):
        if acct.get("name") == account_name:
            return {
                "name": acct["name"],
                "host": acct["imap"]["host"],
                "port": acct["imap"].get("port", 993),
                "ssl": acct["imap"].get("ssl", True),
                "username": acct["imap"]["username"],
                "password": acct["imap"]["password"],
                "mailbox": acct.get("mailbox", "INBOX"),
                "download_path": Path(acct["download"]["path"]),
                "attachment_extensions": [e.lower() for e in raw.get("attachment_extensions", [])],
            }
    raise ValueError(f"account '{account_name}' not found in config")


def uid_search(conn: imaplib.IMAP4_SSL, keyword: str) -> list[int]:
    """在服务端用 IMAP SEARCH 搜索主题含指定关键词的 UID。"""
    # Python imaplib 默认用 ASCII 编码，中文需要手动发 UTF-8 字节
    keyword_bytes = keyword.encode("utf-8")
    status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", "SUBJECT", keyword_bytes)
    if status != "OK" or not data or not data[0]:
        return []
    return [int(x) for x in data[0].split()]


def _parse_date_arg(value: str | None):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _in_date_range(mail_date: datetime, since, until) -> bool:
    local_day = mail_date.astimezone().date()
    if since and local_day < since:
        return False
    if until and local_day > until:
        return False
    return True


def _save_attachment(
    *,
    mail_date: datetime,
    original_filename: str,
    content: bytes,
    account_email: str,
    download_dir: Path,
    seq: int,
) -> Path:
    safe_name = original_filename.replace("/", "_").replace(":", "_").strip() or "attachment"
    stamp = mail_date.strftime("%Y%m%d_%H%M%S")
    stem, _, ext = safe_name.rpartition(".")
    if stem:
        name_part = f"{stem}_{account_email}.{ext}" if account_email else safe_name
    else:
        name_part = f"{safe_name}_{account_email}" if account_email else safe_name
    filename = f"{stamp}_{seq:04d}_{name_part}"
    path = download_dir / filename
    path.write_bytes(content)
    return path


def backfill(
    account_cfg: dict,
    keywords: list[str],
    dry_run: bool,
    batch_size: int,
    db: ProcessedDB,
    output_dir: Path | None = None,
    *,
    since=None,
    until=None,
) -> None:
    account_name = account_cfg["name"]
    download_dir = output_dir or account_cfg["download_path"]
    allowed_exts = set(account_cfg["attachment_extensions"])

    if not dry_run:
        download_dir.mkdir(parents=True, exist_ok=True)

    # 连接 IMAP
    print(f"连接 {account_cfg['host']}:{account_cfg['port']} ...")
    conn = imaplib.IMAP4_SSL(account_cfg["host"], account_cfg["port"])
    conn.login(account_cfg["username"], account_cfg["password"])
    send_imap_id(conn, account_cfg["username"])
    status, data = conn.select(account_cfg["mailbox"], readonly=dry_run)
    if status != "OK":
        raise RuntimeError(f"SELECT {account_cfg['mailbox']} failed: {status}")
    total_msgs = int(data[0]) if data and data[0] else 0
    print(f"邮箱共 {total_msgs} 封邮件")

    # 第一步：服务端按每个关键词搜索，取交集
    print(f"服务端搜索关键词: {keywords}")
    uid_sets: list[set[int]] = []
    for kw in keywords:
        uids = uid_search(conn, kw)
        uid_sets.append(set(uids))
        print(f"  '{kw}' → 命中 {len(uids)} 封")

    if not uid_sets:
        print("无匹配关键词，退出")
        conn.close()
        return

    candidate_uids = set.intersection(*uid_sets)
    print(f"取交集后候选: {len(candidate_uids)} 封")
    if since or until:
        print(f"日期过滤: {since or '不限'} ~ {until or '不限'}")

    # 第二步：排除已处理的
    skipped_processed = 0
    remaining: list[int] = []
    for uid in candidate_uids:
        # 需要先 FETCH 拿 message_id 才能查 DB，但为减少请求，
        # 先全部拉下来再逐条查 DB
        remaining.append(uid)

    print(f"开始逐封拉取验证（共 {len(remaining)} 尪）...")

    scanned = 0
    matched = 0
    downloaded = 0
    skipped_date = 0
    seq = 0

    for i, uid in enumerate(remaining):
        # FETCH 邮件内容
        st, fetched = conn.uid("FETCH", str(uid), "(RFC822)")
        if st != "OK" or not fetched:
            continue

        raw = fetched[0][1]
        if not isinstance(raw, bytes):
            continue

        msg = __import__("email").message_from_bytes(raw)
        subject = decode_mime_header(msg.get("Subject", "") or "")
        message_id = extract_message_id(msg, uid=uid)
        mail_date = parse_date(msg)

        scanned += 1

        if not _in_date_range(mail_date, since, until):
            skipped_date += 1
            continue

        # DB 去重
        if db.is_processed(account_name, message_id):
            skipped_processed += 1
            if (i + 1) % batch_size == 0:
                print(f"  进度 {i+1}/{len(remaining)} (scanned={scanned}, "
                      f"skipped={skipped_processed}, downloaded={downloaded})")
            continue

        # 主题二次确认（服务端 SEARCH 可能有编码差异）
        all_match = all(kw in subject for kw in keywords)
        if not all_match:
            # 服务端匹配但客户端不匹配，dry-run 时不写 DB
            if not dry_run:
                db.mark_processed(account_name, message_id, subject,
                                   mail_date, [], False)
            if (i + 1) % batch_size == 0:
                print(f"  进度 {i+1}/{len(remaining)} (scanned={scanned}, "
                      f"skipped={skipped_processed}, downloaded={downloaded})")
            continue

        matched += 1
        attachments = extract_attachments(msg, allowed_exts)

        if dry_run:
            print(f"  [DRY-RUN] UID={uid} 主题={subject} 附件={len(attachments)}个")
            for att in attachments:
                print(f"            → {att['filename']} ({len(att['content'])} bytes)")
            # dry-run 不写 DB，避免污染数据
            continue

        # 下载附件
        file_paths = []
        for att in attachments:
            seq += 1
            path = _save_attachment(
                mail_date=mail_date,
                original_filename=att["filename"],
                content=att["content"],
                account_email=account_cfg["username"],
                download_dir=download_dir,
                seq=seq,
            )
            file_paths.append(str(path))
            downloaded += 1
            print(f"  ✅ {path.name}")

        db.mark_processed(account_name, message_id, subject,
                           mail_date, file_paths, True)

        if (i + 1) % batch_size == 0:
            print(f"  进度 {i+1}/{len(remaining)} (scanned={scanned}, "
                  f"skipped={skipped_processed}, downloaded={downloaded})")

    conn.close()

    # 输出汇总
    print("\n" + "=" * 50)
    print("回填完成")
    print("=" * 50)
    print(f"  候选邮件:   {len(candidate_uids)}")
    print(f"  已扫描:     {scanned}")
    print(f"  跳过已处理: {skipped_processed}")
    print(f"  主题匹配:   {matched}")
    print(f"  日期跳过:   {skipped_date}")
    print(f"  下载附件:   {downloaded}")
    if dry_run:
        print(f"  [DRY-RUN 模式，未实际下载文件]")
    print("=" * 50)


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 加载配置
    account_cfg = load_account_config(args.config, args.account)
    db_path = Path("data/processed.db")
    if not db_path.is_absolute():
        db_path = args.config.parent / db_path

    db = ProcessedDB(db_path)
    try:
        backfill(
            account_cfg=account_cfg,
            keywords=args.keywords,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            db=db,
            output_dir=args.output,
            since=_parse_date_arg(args.since),
            until=_parse_date_arg(args.until),
        )
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
