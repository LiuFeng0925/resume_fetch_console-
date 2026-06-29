from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from src.config import AccountConfig, AppConfig
from src.downloader import AttachmentDownloader
from src.filter import AttachmentFilter
from src.imap_client import ImapMailClient, MailFetcher
from src.matcher import SubjectMatcher
from src.models import AccountRunResult, RunSummary
from src.store import ProcessedMailStore

logger = logging.getLogger(__name__)

FetcherFactory = Callable[[AccountConfig], MailFetcher]


def _write_progress(accounts: list[dict]) -> None:
    """将当前收取进度写入 JSON 文件，供 Web 控制面板实时读取。"""
    _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    _PROGRESS_FILE.write_text(
        json.dumps(accounts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _init_progress(account_names: list[str]) -> None:
    """初始化所有账号为「等待中」状态。"""
    progress = [
        {
            "name": name,
            "status": "pending",   # pending | running | done | error
            "scanned": 0,
            "matched": 0,
            "downloaded": 0,
            "error": "",
            "started_at": "",
            "finished_at": "",
        }
        for name in account_names
    ]
    _write_progress(progress)


def _update_progress(
    account_name: str,
    status: str,
    *,
    scanned: int = 0,
    matched: int = 0,
    downloaded: int = 0,
    error: str = "",
) -> None:
    """更新单个账号的进度。"""
    if not _PROGRESS_FILE.exists():
        return
    try:
        progress = json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
        for item in progress:
            if item["name"] == account_name:
                item["status"] = status
                if scanned > 0:
                    item["scanned"] = scanned
                if matched > 0:
                    item["matched"] = matched
                if downloaded > 0:
                    item["downloaded"] = downloaded
                if error:
                    item["error"] = error[:200]
                if status == "running":
                    import datetime as _dt
                    item["started_at"] = _dt.datetime.now().astimezone().isoformat()
                if status in ("done", "error"):
                    import datetime as _dt
                    item["finished_at"] = _dt.datetime.now().astimezone().isoformat()
                break
        _write_progress(progress)
    except Exception:
        pass


def _clear_progress() -> None:
    """清除进度文件。"""
    if _PROGRESS_FILE.exists():
        try:
            _PROGRESS_FILE.unlink()
        except OSError:
            pass

# 进度文件路径（供 Web 控制面板读取）
_PROGRESS_DIR = Path(__file__).resolve().parent.parent / "data"
_PROGRESS_FILE = _PROGRESS_DIR / "fetch_progress.json"


def run_job(
    config: AppConfig,
    store: ProcessedMailStore,
    fetcher_factory: FetcherFactory | None = None,
    account_filter: str | None = None,
) -> RunSummary:
    matcher = SubjectMatcher(list(config.subject_patterns), list(config.subject_exclude_patterns))
    attachment_filter = AttachmentFilter(list(config.attachment_extensions))
    summary = RunSummary()

    accounts = config.accounts
    if account_filter:
        accounts = tuple(a for a in accounts if a.name == account_filter)
        if not accounts:
            raise ValueError(f"Unknown account: {account_filter}")

    # 初始化进度
    _init_progress([a.name for a in accounts])

    for account in accounts:
        result = AccountRunResult(account_name=account.name)
        _update_progress(account.name, "running")

        fetcher: MailFetcher = (
            fetcher_factory(account)
            if fetcher_factory
            else ImapMailClient(
                host=account.imap.host,
                port=account.imap.port,
                ssl=account.imap.ssl,
                username=account.imap.username,
                password=account.password,
                mailbox=account.mailbox,
            )
        )
        try:
            fetcher.connect()
            watermark = store.get_watermark_uid(account.name)
            if watermark is None:
                max_uid = fetcher.get_max_uid()
                store.set_watermark_uid(account.name, max_uid)
                logger.info(
                    "%s: first run, watermark set to UID %s (no backfill)",
                    account.name,
                    max_uid,
                )
                fetcher.disconnect()
                _update_progress(account.name, "done")
                summary.results.append(result)
                continue

            messages = fetcher.fetch_messages_after_uid(watermark)
            downloader = AttachmentDownloader(account.download_path)
            max_seen_uid = watermark

            for msg in messages:
                result.scanned += 1
                max_seen_uid = max(max_seen_uid, msg.uid)
                if store.is_processed(account.name, msg.message_id):
                    result.skipped_processed += 1
                    continue

                saved_paths: list[str] = []
                subject_matched = matcher.matches(msg.subject)
                if subject_matched:
                    result.matched += 1
                    for att in msg.attachments:
                        if not attachment_filter.is_allowed(att.filename):
                            continue
                        path = downloader.save(
                            msg.mail_date, att.filename, att.content,
                            account_email=account.imap.username,
                        )
                        saved_paths.append(str(path))
                        result.downloaded += 1

                store.mark_processed(
                    account_name=account.name,
                    message_id=msg.message_id,
                    subject=msg.subject,
                    mail_date=msg.mail_date,
                    saved_files=saved_paths,
                    matched=subject_matched,
                )

            store.set_watermark_uid(account.name, max_seen_uid)
            fetcher.disconnect()
            _update_progress(account.name, "done",
                             scanned=result.scanned,
                             matched=result.matched,
                             downloaded=result.downloaded)
        except Exception as exc:
            logger.exception("%s: account run failed", account.name)
            result.error = str(exc)
            _update_progress(account.name, "error",
                             scanned=result.scanned,
                             matched=result.matched,
                             downloaded=result.downloaded,
                             error=str(exc))
            try:
                fetcher.disconnect()
            except Exception:
                pass
        summary.results.append(result)

    _clear_progress()
    return summary
