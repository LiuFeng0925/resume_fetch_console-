from datetime import datetime, timezone

from src.models import Attachment, MailMessage


def test_mail_message_fields():
    msg = MailMessage(
        uid=1,
        message_id="<a@b>",
        subject="应聘",
        mail_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        attachments=(Attachment("x.pdf", b"%PDF"),),
    )
    assert msg.attachments[0].filename == "x.pdf"
