from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    subject: str
    mail_date: datetime
    attachments: tuple[Attachment, ...] = ()


@dataclass
class AccountRunResult:
    account_name: str
    scanned: int = 0
    matched: int = 0
    downloaded: int = 0
    skipped_processed: int = 0
    error: str | None = None


@dataclass
class RunSummary:
    results: list[AccountRunResult] = field(default_factory=list)

    @property
    def any_failed(self) -> bool:
        return any(r.error for r in self.results)

    @property
    def total_downloaded(self) -> int:
        return sum(r.downloaded for r in self.results)
