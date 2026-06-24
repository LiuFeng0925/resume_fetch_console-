from datetime import datetime, timezone

from src.store import ProcessedMailStore


def test_is_processed_false_initially(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    assert store.is_processed("hr-boss", "<id@example.com>") is False
    store.close()


def test_mark_and_check_processed(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    store.mark_processed(
        account_name="hr-boss",
        message_id="<id@example.com>",
        subject="应聘",
        mail_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        saved_files=["/tmp/a.pdf"],
        matched=True,
    )
    assert store.is_processed("hr-boss", "<id@example.com>") is True
    store.close()


def test_dedup_scoped_by_account(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    mid = "<same@example.com>"
    store.mark_processed("acc-a", mid, "s", datetime.now(timezone.utc), [], True)
    assert store.is_processed("acc-a", mid) is True
    assert store.is_processed("acc-b", mid) is False
    store.close()


def test_watermark_first_run_none_then_set(tmp_path):
    store = ProcessedMailStore(tmp_path / "test.db")
    assert store.get_watermark_uid("hr-boss") is None
    store.set_watermark_uid("hr-boss", 42)
    assert store.get_watermark_uid("hr-boss") == 42
    store.close()
