"""Weekly summary / failure / test emails. Recipients come ONLY from config/email.yaml.

Methods (config email.method):
  gmail_smtp: smtp.gmail.com:465 SSL; env GMAIL_SENDER, GMAIL_APP_PASSWORD
  resend:     POST https://api.resend.com/emails; env RESEND_API_KEY, RESEND_FROM
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
from datetime import date
from email.message import EmailMessage

import requests

from ..models import Lead

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465


class EmailConfigError(RuntimeError):
    """Missing credentials/recipients or unsupported method."""


class EmailSendError(RuntimeError):
    """The provider rejected the message."""


# ---------------------------------------------------------------- content

def _sort_key(lead: Lead):
    return (-(lead.score or 0), 0 if lead.on_sale_date else 1, lead.on_sale_date or "", lead.event_date or "~")


def top_leads(leads: list[Lead], segment: str, n: int = 3) -> list[Lead]:
    """Top n non-excluded leads in a segment: score desc, then soonest on_sale_date (blanks last)."""
    pool = [l for l in leads if l.segment == segment and not l.excluded_reason]
    return sorted(pool, key=_sort_key)[:n]


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def why_now(lead: Lead, today: date) -> str:
    parts = []
    d = _parse_date(lead.on_sale_date)
    if d:
        delta = (d - today).days
        if delta > 1:
            parts.append(f"on sale in {delta} days ({d.isoformat()})")
        elif delta == 1:
            parts.append(f"on sale tomorrow ({d.isoformat()})")
        elif delta == 0:
            parts.append("on sale today")
        else:
            parts.append(f"on sale since {d.isoformat()}")
    if lead.scalping_evidence_summary:
        s = lead.scalping_evidence_summary.strip().split("\n")[0]
        parts.append("scalping: " + (s if len(s) <= 90 else s[:87].rstrip() + "..."))
    if lead.score_breakdown:
        parts.append(f"score {lead.score} ({lead.score_breakdown.strip()})")
    elif not parts:
        parts.append(f"score {lead.score}")
    out = "; ".join(parts)
    return out if len(out) <= 240 else out[:237].rstrip() + "..."


def _segments(ctx, leads: list[Lead], new_counts: dict[str, int]) -> list[str]:
    order: list[str] = []
    for s in list(ctx.config.targets.active_segments) + list(new_counts) + [l.segment for l in leads]:
        if s and s not in order:
            order.append(s)
    return order


def _safe_sheet_url(ctx) -> str:
    try:
        from .sheets import sheet_url
        return sheet_url(ctx)
    except Exception as e:  # missing env should not block the summary
        log.warning("sheet url unavailable: %s", e)
        return ""


def build_summary(ctx, leads: list[Lead], new_counts: dict[str, int]) -> tuple[str, str, str]:
    """Return (subject, plain_text, html)."""
    total = sum(new_counts.values())
    subject = f"{ctx.config.email.subject_prefix} {ctx.week}: {total} new event{'s' if total != 1 else ''}"
    url = _safe_sheet_url(ctx)
    e = html.escape

    text = [f"Argos Lead Finder weekly summary ({ctx.week})", ""]
    h = [f"<h2>Argos Lead Finder weekly summary ({e(ctx.week)})</h2>"]
    text.append("New events by segment:")
    h.append("<h3>New events by segment</h3><ul>")
    segs = _segments(ctx, leads, new_counts)
    for s in segs:
        text.append(f"  - {s}: {new_counts.get(s, 0)}")
        h.append(f"<li>{e(s)}: <b>{new_counts.get(s, 0)}</b></li>")
    h.append("</ul>")

    for s in segs:
        top = top_leads(leads, s)
        text += ["", f"Top leads: {s}"]
        h.append(f"<h3>Top leads: {e(s)}</h3>")
        if not top:
            text.append("  (none this week)")
            h.append("<p><i>None this week.</i></p>")
            continue
        h.append("<ol>")
        for i, l in enumerate(top, 1):
            seller = l.ticket_seller or "(seller unknown)"
            ev = l.event or l.artist_or_headliner
            where = ", ".join(x for x in (l.venue, l.city) if x) or "(city unknown)"
            when = l.event_date or "date TBD"
            reason = why_now(l, ctx.today)
            text.append(f"  {i}. {seller} | {ev} | {where} | {when}")
            text.append(f"     Why now: {reason}")
            h.append(f"<li><b>{e(seller)}</b>: {e(ev)}, {e(where)}, {e(when)}"
                     f"<br><small>Why now: {e(reason)}</small></li>")
        h.append("</ol>")

    text.append("")
    if url:
        text.append(f"Full list: {url}")
        h.append(f'<p><a href="{e(url, quote=True)}">Open the lead sheet</a></p>')
    else:
        text.append("Full list: (sheet link unavailable)")
        h.append("<p>Sheet link unavailable.</p>")
    body_html = "<html><body style=\"font-family:sans-serif\">" + "\n".join(h) + "</body></html>"
    return subject, "\n".join(text) + "\n", body_html


# ---------------------------------------------------------------- sending

def _recipients(ctx) -> list[str]:
    rcpts = [r.strip() for r in ctx.config.email.recipients if r and r.strip()]
    if not rcpts:
        raise EmailConfigError("config/email.yaml has no recipients.")
    return rcpts


def _require_env(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise EmailConfigError(f"Missing environment variable(s) for email: {', '.join(missing)}")
    return [os.environ[n].strip() for n in names]


def _send_gmail(rcpts: list[str], subject: str, text: str, body_html: str) -> None:
    sender, password = _require_env("GMAIL_SENDER", "GMAIL_APP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(rcpts)
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")  # -> multipart/alternative
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg, from_addr=sender, to_addrs=rcpts)


def _send_resend(rcpts: list[str], subject: str, text: str, body_html: str) -> None:
    key, sender = _require_env("RESEND_API_KEY", "RESEND_FROM")
    resp = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"from": sender, "to": rcpts, "subject": subject, "html": body_html, "text": text},
        timeout=60,
    )
    if resp.status_code >= 300:
        raise EmailSendError(f"Resend rejected the email: HTTP {resp.status_code}: {resp.text[:300]}")


def _send(ctx, subject: str, text: str, body_html: str) -> None:
    rcpts = _recipients(ctx)
    method = ctx.config.email.method
    if ctx.dry_run:
        log.info("[dry-run] would send email via %s to %s: %s", method, rcpts, subject)
        log.info("[dry-run] email body:\n%s", text)
        return
    if method == "gmail_smtp":
        _send_gmail(rcpts, subject, text, body_html)
    elif method == "resend":
        _send_resend(rcpts, subject, text, body_html)
    else:
        raise EmailConfigError(f"Unsupported email method: {method}")
    log.info("sent email via %s to %d recipient(s): %s", method, len(rcpts), subject)


def send_summary(ctx, leads: list[Lead], new_counts: dict[str, int]) -> None:
    subject, text, body_html = build_summary(ctx, list(leads), dict(new_counts))
    _send(ctx, subject, text, body_html)


def send_failure(ctx, message: str) -> None:
    subject = f"{ctx.config.email.subject_prefix} Run FAILED ({ctx.week})"
    text = f"The Argos Lead Finder run failed.\n\n{message}\n\nCheck the GitHub Actions log for details.\n"
    body_html = ("<html><body><p><b>The Argos Lead Finder run failed.</b></p>"
                 f"<pre>{html.escape(message)}</pre><p>Check the GitHub Actions log for details.</p></body></html>")
    _send(ctx, subject, text, body_html)


def send_test(ctx) -> None:
    subject = f"{ctx.config.email.subject_prefix} Test email"
    text = (f"This is a test email from Argos Lead Finder (method: {ctx.config.email.method}).\n"
            "If you received it, email delivery is configured correctly.\n")
    body_html = ("<html><body><p>This is a test email from Argos Lead Finder "
                 f"(method: {html.escape(ctx.config.email.method)}).</p>"
                 "<p>If you received it, email delivery is configured correctly.</p></body></html>")
    _send(ctx, subject, text, body_html)
