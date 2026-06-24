from __future__ import annotations

import logging
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

    for account in accounts:
        result = AccountRunResult(account_name=account.name)
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
        except Exception as exc:
            logger.exception("%s: account run failed", account.name)
            result.error = str(exc)
            try:
                fetcher.disconnect()
            except Exception:
                pass
        summary.results.append(result)

    return summary
