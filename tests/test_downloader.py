from datetime import datetime, timezone
from pathlib import Path

from src.downloader import AttachmentDownloader


def test_save_uses_date_time_and_original_name(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, 14, 0, 3, tzinfo=timezone.utc)
    path = dl.save(
        mail_date=mail_date,
        original_filename="【AI产品经理_北京_30-40K】刘烨_7年.pdf",
        content=b"%PDF-1.4",
    )
    assert path.name == "20260529_140003_0001_【AI产品经理_北京_30-40K】刘烨_7年.pdf"
    assert path.read_bytes() == b"%PDF-1.4"


def test_same_filename_saves_both_without_overwrite(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, tzinfo=timezone.utc)
    path1 = dl.save(mail_date, "resume.pdf", b"1")
    path2 = dl.save(mail_date, "resume.pdf", b"2")
    assert path1 != path2
    assert path1.exists() and path2.exists()
    assert path1.read_bytes() == b"1"
    assert path2.read_bytes() == b"2"


def test_sanitize_path_separators(tmp_path):
    dl = AttachmentDownloader(tmp_path)
    mail_date = datetime(2026, 5, 29, tzinfo=timezone.utc)
    path = dl.save(mail_date, "bad/name:file.pdf", b"x")
    assert "/" not in path.name
    assert ":" not in path.name
    assert path.exists()
