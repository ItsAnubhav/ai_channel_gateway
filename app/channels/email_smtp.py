from __future__ import annotations

import smtplib
from email.message import EmailMessage

from channel_gateway.app.config import Settings


def send_email_reply(
    settings: Settings,
    *,
    to_address: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> None:
    if not settings.email_smtp_host or not settings.email_from:
        return

    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_address
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.set_content(body)

    with smtplib.SMTP(settings.email_smtp_host, settings.email_smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.email_smtp_user:
            smtp.login(settings.email_smtp_user, settings.email_smtp_password)
        smtp.send_message(message)
