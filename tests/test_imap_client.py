from email.message import EmailMessage

from src.imap_client import (
    decode_mime_header,
    extract_attachments,
    extract_message_id,
    parse_mail_message,
)
from src.matcher import SubjectMatcher


def test_extract_message_id_from_header():
    msg = EmailMessage()
    msg["Message-ID"] = "<abc@boss.com>"
    assert extract_message_id(msg) == "<abc@boss.com>"


def test_extract_message_id_missing_generates_stable_fallback():
    msg = EmailMessage()
    assert extract_message_id(msg, uid=99).endswith("@local.generated>")


def test_extract_attachments_skips_inline():
    msg = EmailMessage()
    msg["Subject"] = "应聘"
    msg.set_type("multipart/mixed")
    part = EmailMessage()
    part.add_header("Content-Disposition", "attachment", filename="cv.pdf")
    part.set_payload(b"%PDF")
    msg.attach(part)
    inline = EmailMessage()
    inline.add_header("Content-Disposition", "inline")
    inline.set_payload(b"html")
    msg.attach(inline)
    attachments = extract_attachments(msg)
    assert len(attachments) == 1
    assert attachments[0].filename == "cv.pdf"


def test_parse_mail_message_roundtrip():
    raw = (
        b"From: hr@boss.com\r\n"
        b"To: me@co.com\r\n"
        b"Subject: =?utf-8?b?5Y+R8J+R?=?\r\n"
        b"Message-ID: <m@x>\r\n"
        b"Date: Thu, 29 May 2026 14:00:03 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"body"
    )
    parsed = parse_mail_message(7, raw)
    assert parsed.uid == 7
    assert parsed.message_id == "<m@x>"


def test_decode_mime_header_utf8_subject():
    encoded = "=?utf-8?b?5paw6K6+5aSH55m75b2V5o+Q6YaS?="
    decoded = decode_mime_header(encoded)
    assert decoded == "新设备登录提醒"
    assert "=?" not in decoded


def test_parse_mail_message_decodes_boss_subject():
    raw = (
        b"From: boss@zhipin.com\r\n"
        b"Subject: =?GBK?B?uu66o8O3IHwgMjbE6tOmvezJ+qOs06bGuCDOxNSx?=\r\n"
        b" =?GBK?B?1vrA7SB8IMypsLI0LTdKob5CT1NT1rHGuKG/?=\r\n"
        b"Message-ID: <boss@test>\r\n"
        b"Date: Thu, 29 May 2026 07:18:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"body"
    )
    parsed = parse_mail_message(1, raw)
    matcher = SubjectMatcher([r".*应聘.*【BOSS直聘】"])
    assert "应聘" in parsed.subject
    assert "BOSS直聘" in parsed.subject
    assert matcher.matches(parsed.subject)


def test_extract_attachments_decodes_encoded_filename():
    msg = EmailMessage()
    msg.set_type("multipart/mixed")
    part = EmailMessage()
    part.add_header(
        "Content-Disposition",
        "attachment",
        filename="=?UTF-8?B?5paH5ZGY5Yqp55CGLnBkZg==?=",
    )
    part.set_payload(b"%PDF")
    msg.attach(part)
    attachments = extract_attachments(msg)
    assert len(attachments) == 1
    assert attachments[0].filename.endswith(".pdf")
    assert "=?" not in attachments[0].filename
