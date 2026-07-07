"""Webhook email delivery via Google Apps Script (Railway Safe)."""

import requests
from config import (
    EMAIL_TO,
    GOOGLE_WEBHOOK_URL,
    log,
)

def send_email(subject: str, html: str, plain: str) -> None:
    # Recipients ki list ko comma-separated string mein convert karein
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    to_string = ", ".join(recipients)

    # Google Apps Script ke liye data prepare karein
    payload = {
        "to": to_string,
        "subject": subject,
        "html": html,
        "plain": plain
    }

    try:
        # Railway is HTTP POST request (port 443) ko allow karega
        response = requests.post(GOOGLE_WEBHOOK_URL, json=payload)
        
        # Check if the script executed successfully
        if response.status_code == 200:
            log.info("Email sent via Webhook to %d recipients: %s", len(recipients), to_string)
        else:
            log.error("Webhook error. Status code: %s", response.status_code)
            raise RuntimeError(f"Webhook returned status {response.status_code}")

    except Exception as exc:
        log.error("Unexpected email error: %s", exc)
        raise