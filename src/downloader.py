from __future__ import annotations

from datetime import datetime
from pathlib import Path


class AttachmentDownloader:
    """Saves every attachment to a unique path; no file-level deduplication."""

    def __init__(self, download_dir: Path) -> None:
        self._download_dir = download_dir
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def save(
        self,
        mail_date: datetime,
        original_filename: str,
        content: bytes,
        account_email: str = "",
    ) -> Path:
        safe_name = self._sanitize_filename(original_filename)
        stamp = mail_date.strftime("%Y%m%d_%H%M%S")
        self._seq += 1
        # 把扩展名分离出来，把 account_email 插在扩展名之前
        stem, _, ext = safe_name.rpartition(".")
        if stem:
            # 原文件名有扩展名，如 xxx.pdf
            name_part = f"{stem}_{account_email}.{ext}" if account_email else safe_name
        else:
            # 原文件名没有扩展名
            name_part = f"{safe_name}_{account_email}" if account_email else safe_name
        filename = f"{stamp}_{self._seq:04d}_{name_part}"
        path = self._download_dir / filename
        path.write_bytes(content)
        return path

    def _sanitize_filename(self, filename: str) -> str:
        name = filename.replace("/", "_").replace(":", "_")
        name = name.strip() or "attachment"
        return name
