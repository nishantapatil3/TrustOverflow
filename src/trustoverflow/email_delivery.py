from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol


class EmailSender(Protocol):
    def send_password_reset(self, recipient: str, reset_url: str) -> None: ...


class DisabledEmailSender:
    def send_password_reset(self, recipient: str, reset_url: str) -> None:
        return None


class SMTPEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        *,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        use_ssl: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.starttls = starttls
        self.use_ssl = use_ssl

    def send_password_reset(self, recipient: str, reset_url: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Reset your TrustOverflow password"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            "A password reset was requested for your TrustOverflow account.\n\n"
            f"Reset your password: {reset_url}\n\n"
            "This link expires in 30 minutes and can be used once. "
            "If you did not request it, you can ignore this email."
        )

        smtp_class = smtplib.SMTP_SSL if self.use_ssl else smtplib.SMTP
        with smtp_class(self.host, self.port, timeout=10) as client:
            if self.starttls:
                client.starttls(context=ssl.create_default_context())
            if self.username:
                client.login(self.username, self.password or "")
            client.send_message(message)


def email_sender_from_environment() -> EmailSender:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        return DisabledEmailSender()
    return SMTPEmailSender(
        host,
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_FROM", "TrustOverflow <no-reply@trustoverflow.local>"),
        username=os.getenv("SMTP_USERNAME") or None,
        password=os.getenv("SMTP_PASSWORD") or None,
        starttls=os.getenv("SMTP_STARTTLS", "true").casefold() in {"1", "true", "yes"},
        use_ssl=os.getenv("SMTP_SSL", "false").casefold() in {"1", "true", "yes"},
    )
