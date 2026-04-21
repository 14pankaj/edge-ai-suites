# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
send_email_alert tool — sends an SMTP email notification when an alert fires.

Configuration (environment variables):
    SMTP_HOST, SMTP_PORT, SMTP_USE_TLS, SMTP_USER, SMTP_PASSWORD
    ALERT_EMAIL_FROM   — sender address
    ALERT_EMAIL_TO     — comma-separated default recipients (overridable per call)

Requires:  aiosmtplib
"""

import logging
from email.message import EmailMessage
from typing import Optional

import aiosmtplib

from src.config import settings

logger = logging.getLogger(__name__)


async def send_email_alert(
    subject: str,
    body: str,
    recipient: Optional[str] = None,
    stream_id: str = "",
    alert_name: str = "",
    severity: str = "medium",
) -> dict:
    """Send an SMTP email notification for a triggered alert."""
    smtp_host = settings.SMTP_HOST
    if not smtp_host:
        logger.warning("send_email_alert: SMTP_HOST not configured — skipping")
        return {"status": "skipped", "reason": "SMTP_HOST not configured"}

    to_addr = recipient or settings.ALERT_EMAIL_TO
    if not to_addr:
        logger.warning("send_email_alert: no recipient address — skipping")
        return {"status": "skipped", "reason": "no recipient configured"}

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.ALERT_EMAIL_FROM or settings.SMTP_USER
        msg["To"] = to_addr
        msg.set_content(body)

        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=settings.SMTP_PORT,
            start_tls=settings.SMTP_USE_TLS,
            username=settings.SMTP_USER or None,
            password=settings.SMTP_PASSWORD or None,
        )

        logger.info(
            f"Email sent | alert={alert_name} severity={severity} "
            f"stream={stream_id} to={to_addr}"
        )
        return {"status": "sent", "recipients": to_addr}

    except Exception as exc:
        logger.error(f"send_email_alert failed: {exc}")
        return {"status": "error", "reason": str(exc)}
