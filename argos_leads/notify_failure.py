"""Failure notice using only the standard library, so it still works when the
failed step was `pip install`. Reads recipients/method from config/email.yaml
with a minimal line parser (that file is flat by design)."""
from __future__ import annotations

import json
import os
import re
import smtplib
import urllib.request
from email.message import EmailMessage
from pathlib import Path

EMAIL_YAML = Path(__file__).resolve().parent.parent / "config" / "email.yaml"


def _read_email_yaml(path: Path = EMAIL_YAML) -> tuple[list[str], str, str]:
    recipients: list[str] = []
    method, prefix = "gmail_smtp", "[Argos Lead Finder]"
    for line in path.read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if line.startswith("recipients:"):
            recipients = [r.strip().strip("'\"") for r in re.findall(r"[^\[\],]+", line.split(":", 1)[1]) if r.strip()]
        elif line.startswith("method:"):
            method = line.split(":", 1)[1].strip().strip("'\"")
        elif line.startswith("subject_prefix:"):
            prefix = line.split(":", 1)[1].strip().strip("'\"")
    if not recipients:
        raise ValueError(f"no recipients in {path}")
    return recipients, method, prefix


def send(message: str, path: Path = EMAIL_YAML) -> None:
    recipients, method, prefix = _read_email_yaml(path)
    subject = f"{prefix} Weekly run FAILED"
    body = f"The Argos Lead Finder run failed.\n\n{message}\n\nCheck the GitHub Actions logs for details."
    if method == "resend":
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps({"from": os.environ["RESEND_FROM"], "to": recipients, "subject": subject, "text": body}).encode(),
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=30).read()
        return
    msg = EmailMessage()
    msg["From"] = os.environ["GMAIL_SENDER"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(os.environ["GMAIL_SENDER"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)
