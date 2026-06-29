from __future__ import annotations

import email
import email.utils
import imaplib
from datetime import datetime, timezone
from email.header import decode_header
from email.message import Message
from typing import Protocol

from src.models import Attachment, MailMessage


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


def send_imap_id(conn: imaplib.IMAP4, username: str) -> None:
    """163/126 等邮箱要求登录后发送 ID，否则 SELECT 会报 Unsafe Login。"""
    if "ID" not in imaplib.Commands:
        imaplib.Commands["ID"] = ("AUTH",)
    args = (
        "name",
        "mail-resume-fetcher",
        "version",
        "1.0",
        "vendor",
        "local",
        "contact",
        username,
    )
    id_arg = '("' + '" "'.join(args) + '")'
    typ, data = conn._simple_command("ID", id_arg)
    if typ != "OK":
        raise RuntimeError(f"IMAP ID failed: {typ} {data}")


def extract_message_id(msg: Message, uid: int | None = None) -> str:
    mid = msg.get("Message-ID")
    if mid:
        return mid.strip()
    if uid is not None:
        return f"<uid-{uid}@local.generated>"
    return "<unknown@local.generated>"


def extract_attachments(msg: Message) -> list[Attachment]:
    results: list[Attachment] = []
    if not msg.is_multipart():
        return results
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = decode_mime_header(filename)
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        results.append(Attachment(filename=filename, content=payload))
    return results


def parse_mail_message(uid: int, raw_bytes: bytes) -> MailMessage:
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject", "") or "")
    date_hdr = msg.get("Date")
    if date_hdr:
        mail_date = email.utils.parsedate_to_datetime(date_hdr)
        if mail_date.tzinfo is None:
            mail_date = mail_date.replace(tzinfo=timezone.utc)
    else:
        mail_date = datetime.now(timezone.utc)
    return MailMessage(
        uid=uid,
        message_id=extract_message_id(msg, uid=uid),
        subject=subject,
        mail_date=mail_date,
        attachments=tuple(extract_attachments(msg)),
    )


class MailFetcher(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_max_uid(self) -> int: ...
    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]: ...


class ImapMailClient:
    def __init__(
        self,
        host: str,
        port: int,
        ssl: bool,
        username: str,
        password: str,
        mailbox: str = "INBOX",
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._username = username
        self._password = password
        self._mailbox = mailbox
        self._conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        if self._ssl:
            self._conn = imaplib.IMAP4_SSL(self._host, self._port, timeout=120)
        else:
            self._conn = imaplib.IMAP4(self._host, self._port, timeout=120)
        assert self._conn is not None
        self._conn.login(self._username, self._password)
        send_imap_id(self._conn, self._username)
        status, data = self._conn.select(self._mailbox, readonly=True)
        if status != "OK":
            detail = data[0].decode(errors="replace") if data else ""
            raise RuntimeError(
                f"Cannot select mailbox {self._mailbox}: {status} {detail}".strip()
            )

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except imaplib.IMAP4.error:
                pass
            try:
                self._conn.logout()
            except imaplib.IMAP4.error:
                pass
            self._conn = None

    def get_max_uid(self) -> int:
        assert self._conn is not None
        status, data = self._conn.uid("SEARCH", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return 0
        uids = [int(x) for x in data[0].split()]
        return max(uids) if uids else 0

    def fetch_messages_after_uid(self, after_uid: int) -> list[MailMessage]:
        assert self._conn is not None
        if after_uid <= 0:
            criteria = "ALL"
        else:
            criteria = f"UID {after_uid + 1}:*"
        status, data = self._conn.uid("SEARCH", None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        uids = [int(x) for x in data[0].split()]
        messages: list[MailMessage] = []
        for uid in uids:
            st, fetched = self._conn.uid("FETCH", str(uid), "(RFC822)")
            if st != "OK" or not fetched:
                continue
            raw = fetched[0][1]
            if isinstance(raw, bytes):
                messages.append(parse_mail_message(uid, raw))
        return messages
