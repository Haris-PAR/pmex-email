"""SMTP email delivery."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    EMAIL_FROM,
    EMAIL_TO,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_SERVER,
    SMTP_USERNAME,
    log,
)


def send_email(subject: str, html: str, plain: str) -> None:
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
        log.info("Email sent to %d recipients: %s", len(recipients), ", ".join(recipients))
    except smtplib.SMTPAuthenticationError as exc:
        log.error("SMTP authentication failed: %s", exc)
        raise
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
        raise
    except Exception as exc:
        log.error("Unexpected email error: %s", exc)
        raise
