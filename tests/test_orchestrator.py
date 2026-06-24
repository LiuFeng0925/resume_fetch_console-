from datetime import datetime, timezone

import pytest

from src.config import AccountConfig, AppConfig, ImapSettings
from src.models import Attachment, MailMessage
from src.orchestrator import run_job
from src.store import ProcessedMailStore


class FakeFetcher:
    def __init__(self, messages: list[MailMessage], max_uid: int = 10):
        self._messages = messages
        self._max_uid = max_uid
        self.connected = False

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def get_max_uid(self) -> int:
        return self._max_uid

    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]:
        return [m for m in self._messages if m.uid > after_uid]


def _app_config(tmp_path) -> AppConfig:
    acc = AccountConfig(
        name="hr-boss",
        imap=ImapSettings("h", 993, True, "u", "IMAP_PASSWORD_HR"),
        mailbox="INBOX",
        download_path=tmp_path / "hr",
        password="pw",
    )
    return AppConfig(
        accounts=(acc,),
        subject_patterns=(r".*应聘.*",),
        attachment_extensions=(".pdf",),
        db_path=tmp_path / "data.db",
        log_path=tmp_path / "run.log",
    )


def test_first_run_sets_watermark_without_download(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    msg = MailMessage(
        uid=5,
        message_id="<old@x>",
        subject="应聘 BOSS",
        mail_date=datetime.now(timezone.utc),
        attachments=(Attachment("cv.pdf", b"%PDF"),),
    )
    fetcher = FakeFetcher([msg], max_uid=5)

    summary = run_job(cfg, store=store, fetcher_factory=lambda a: fetcher)
    assert summary.results[0].downloaded == 0
    assert store.get_watermark_uid("hr-boss") == 5
    store.close()


def test_second_run_downloads_new_mail(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("hr-boss", 5)
    msg = MailMessage(
        uid=6,
        message_id="<new@x>",
        subject="刘烨 | 应聘 AI产品经理【BOSS直聘】",
        mail_date=datetime(2026, 5, 29, 14, 0, 3, tzinfo=timezone.utc),
        attachments=(Attachment("【AI产品经理_北京_30-40K】刘烨_7年.pdf", b"%PDF"),),
    )
    fetcher = FakeFetcher([msg], max_uid=6)

    summary = run_job(cfg, store=store, fetcher_factory=lambda a: fetcher)
    assert summary.results[0].downloaded == 1
    saved = list((tmp_path / "hr").glob("*.pdf"))
    assert len(saved) == 1
    assert saved[0].name.startswith("20260529_140003_")
    store.close()


def test_non_matching_subject_still_marked_processed(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("hr-boss", 0)
    msg = MailMessage(
        uid=1,
        message_id="<news@x>",
        subject="公司周报",
        mail_date=datetime.now(timezone.utc),
        attachments=(),
    )
    summary = run_job(
        cfg,
        store=store,
        fetcher_factory=lambda a: FakeFetcher([msg], max_uid=1),
    )
    assert summary.results[0].downloaded == 0
    assert store.is_processed("hr-boss", "<news@x>")
    store.close()


def test_one_account_failure_continues_other(tmp_path):
    acc1 = AccountConfig(
        name="ok",
        imap=ImapSettings("h", 993, True, "u1", "E1"),
        mailbox="INBOX",
        download_path=tmp_path / "ok",
        password="pw",
    )
    acc2 = AccountConfig(
        name="bad",
        imap=ImapSettings("h", 993, True, "u2", "E2"),
        mailbox="INBOX",
        download_path=tmp_path / "bad",
        password="pw",
    )
    cfg = AppConfig(
        accounts=(acc1, acc2),
        subject_patterns=(r".*应聘.*",),
        attachment_extensions=(".pdf",),
        db_path=tmp_path / "data.db",
        log_path=tmp_path / "run.log",
    )
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("ok", 0)

    class FailFetcher:
        def connect(self):
            raise RuntimeError("AUTH FAILED")

        def disconnect(self):
            pass

        def get_max_uid(self):
            return 0

        def fetch_messages_after_uid(self, after_uid):
            return []

    def factory(account):
        if account.name == "bad":
            return FailFetcher()
        return FakeFetcher([], max_uid=0)

    summary = run_job(cfg, store=store, fetcher_factory=factory)
    assert len(summary.results) == 2
    assert summary.results[0].error is None
    assert summary.results[1].error is not None
    assert summary.any_failed is True
    store.close()


def test_multiple_attachments_all_saved(tmp_path):
    cfg = _app_config(tmp_path)
    store = ProcessedMailStore(cfg.db_path)
    store.set_watermark_uid("hr-boss", 0)
    msg = MailMessage(
        uid=1,
        message_id="<multi@x>",
        subject="应聘 两份简历",
        mail_date=datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc),
        attachments=(
            Attachment("resume.pdf", b"a"),
            Attachment("resume.pdf", b"b"),
        ),
    )
    summary = run_job(
        cfg,
        store=store,
        fetcher_factory=lambda a: FakeFetcher([msg], max_uid=1),
    )
    assert summary.results[0].downloaded == 2
    files = list((tmp_path / "hr").glob("*.pdf"))
    assert len(files) == 2
    store.close()
