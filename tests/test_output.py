"""Tests for argos_leads.output (no network)."""
from __future__ import annotations

import smtplib
from datetime import date
from email import message_from_bytes
from unittest import mock

import pytest
import responses

from argos_leads.config import Config, EmailConfig, TargetsConfig
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.models import COLUMNS, Lead
from argos_leads.output import email as em
from argos_leads.output import sheets, store


# ---------------------------------------------------------------- fixtures

def make_ctx(dry_run=False, method="gmail_smtp", recipients=("team@example.com",)):
    cfg = Config(
        targets=TargetsConfig(active_segments=["kpop", "jpop"], segments={"kpop": {}, "jpop": {}}),
        email=EmailConfig(recipients=list(recipients), method=method),
    )
    return RunContext(config=cfg, db=Database(":memory:"), http=None, llm=None,
                      today=date(2026, 10, 19), dry_run=dry_run)


def lead(i, segment="kpop", score=3, on_sale="", seller="Seller A", excluded="", **kw):
    d = dict(event_key=f"k{i}", week_found="2026-W43", segment=segment, event=f"Tour {i}",
             artist_or_headliner=f"Artist {i}", city="Los Angeles", venue="Arena", event_date=f"2026-12-{i:02d}",
             on_sale_date=on_sale, ticket_seller=seller, score=score, score_breakdown="on-sale soon +2",
             excluded_reason=excluded)
    d.update(kw)
    return Lead(**d)


class Forbidden(AssertionError):
    pass


class FakeWorksheet:
    def __init__(self, title, values=None):
        self.title = title
        self.values = [list(r) for r in (values or [])]
        self.append_calls = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def row_values(self, n):
        return list(self.values[n - 1]) if len(self.values) >= n else []

    def append_rows(self, rows, value_input_option=None, insert_data_option=None, **kw):
        assert value_input_option == "RAW"
        self.append_calls.append([list(r) for r in rows])
        self.values.extend([list(r) for r in rows])

    def __getattr__(self, name):  # clear/update/delete_rows/batch_clear/... must never be used
        if name in {"clear", "update", "batch_update", "batch_clear", "delete_rows", "delete_row",
                    "update_cell", "update_cells", "insert_rows", "insert_row", "resize"}:
            raise Forbidden(f"forbidden worksheet call: {name}")
        raise AttributeError(name)


class FakeSpreadsheet:
    def __init__(self, tabs=None):
        self.tabs = {t.title: t for t in (tabs or [])}
        self.created = []

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, title):
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        assert title not in self.tabs
        ws = FakeWorksheet(title)
        self.tabs[title] = ws
        self.created.append(title)
        return ws

    def del_worksheet(self, *a):
        raise Forbidden("del_worksheet")


class FakeClient:
    def __init__(self, sh):
        self.sh = sh

    def open_by_key(self, key):
        assert key == "SHEET123"
        return self.sh


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET123")
    monkeypatch.setattr(sheets, "_sleep", lambda s: None)
    for k in ("GMAIL_SENDER", "GMAIL_APP_PASSWORD", "RESEND_API_KEY", "RESEND_FROM"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- store

def test_store_upsert_pending_and_mark():
    ctx = make_ctx()
    store.save_leads(ctx, [lead(1), lead(2, excluded="competitor confirmed")])
    l1 = lead(1, score=9)
    l1.field_sources = {"city": "http://x"}
    store.save_leads(ctx, [l1])
    assert ctx.db.one("SELECT COUNT(*) c FROM leads")["c"] == 2
    pend = store.pending_sheet_rows(ctx)
    assert [l.event_key for l in pend] == ["k1"]
    assert pend[0].score == 9 and pend[0].field_sources == {"city": "http://x"}
    row2 = ctx.db.one("SELECT excluded_reason FROM leads WHERE event_key='k2'")
    assert row2["excluded_reason"] == "competitor confirmed"
    store.mark_appended(ctx, ["k1"])
    assert store.pending_sheet_rows(ctx) == []
    store.save_leads(ctx, [lead(1)])  # re-save keeps appended flag
    assert store.pending_sheet_rows(ctx) == []


# ---------------------------------------------------------------- sheets

def test_creates_tabs_and_appends_only_new():
    ctx = make_ctx()
    leads = [lead(1, seller="Seller B"), lead(2, seller="seller a"), lead(3, excluded="x")]
    store.save_leads(ctx, leads)
    sh = FakeSpreadsheet()
    n = sheets.append_new(ctx, leads, client=FakeClient(sh))
    assert n == 2
    ws = sh.tabs["Leads"]
    assert ws.values[0] == COLUMNS
    assert len(ws.values) == 3
    assert ws.values[1][COLUMNS.index("artist_or_headliner")] == "Artist 1"
    out = sh.tabs["Outreach"]
    assert out.values[0] == sheets.OUTREACH_HEADER
    assert [r[0] for r in out.values[1:]] == ["seller a", "Seller B"]
    assert all(r[1:] == [""] * 5 for r in out.values[1:])

    # Second run: same leads + one new seller -> only new row, Outreach untouched.
    out_before = [list(r) for r in out.values]
    leads2 = leads + [lead(4, seller="New Seller")]
    store.save_leads(ctx, leads2)
    assert sheets.append_new(ctx, leads2, client=FakeClient(sh)) == 1
    assert len(ws.values) == 4
    assert out.values == out_before and len(out.append_calls) == 1
    assert sh.created == ["Leads", "Outreach"]


def test_skips_events_already_in_sheet_and_marks_them():
    ctx = make_ctx()
    existing = lead(1)
    ws = FakeWorksheet("Leads", [COLUMNS, [str(v) for v in existing.row()]])
    ws.values[1][COLUMNS.index("city")] = "los angeles "  # normalized compare
    sh = FakeSpreadsheet([ws, FakeWorksheet("Outreach", [sheets.OUTREACH_HEADER])])
    leads = [lead(1), lead(2)]
    store.save_leads(ctx, leads)
    assert sheets.append_new(ctx, leads, client=FakeClient(sh)) == 1
    assert len(ws.values) == 3 and ws.values[2][COLUMNS.index("event")] == "Tour 2"
    assert store.pending_sheet_rows(ctx) == []
    assert sh.tabs["Outreach"].append_calls == []


def test_db_appended_leads_are_not_reappended_even_if_missing_from_sheet():
    ctx = make_ctx()
    store.save_leads(ctx, [lead(1)])
    store.mark_appended(ctx, ["k1"])  # e.g. a human deleted the row; we must not re-add
    sh = FakeSpreadsheet([FakeWorksheet("Leads", [COLUMNS]), FakeWorksheet("Outreach", [sheets.OUTREACH_HEADER])])
    assert sheets.append_new(ctx, [lead(1)], client=FakeClient(sh)) == 0
    assert sh.tabs["Leads"].append_calls == []


def test_pending_from_previous_failed_run_is_appended():
    ctx = make_ctx()
    store.save_leads(ctx, [lead(1)])  # saved last week, append failed
    store.save_leads(ctx, [lead(2)])  # this week
    sh = FakeSpreadsheet()
    assert sheets.append_new(ctx, [lead(2)], client=FakeClient(sh)) == 2
    assert {r[COLUMNS.index("event")] for r in sh.tabs["Leads"].values[1:]} == {"Tour 1", "Tour 2"}
    assert store.pending_sheet_rows(ctx) == []


def test_header_mismatch_raises_and_writes_nothing():
    ctx = make_ctx()
    bad = list(COLUMNS)
    bad[2], bad[3] = bad[3], bad[2]
    ws = FakeWorksheet("Leads", [bad, ["x"] * len(bad)])
    sh = FakeSpreadsheet([ws])
    store.save_leads(ctx, [lead(1)])
    with pytest.raises(sheets.SheetHeaderError):
        sheets.append_new(ctx, [lead(1)], client=FakeClient(sh))
    assert ws.append_calls == [] and sh.created == []


def test_header_with_trailing_blank_cells_ok():
    ctx = make_ctx()
    ws = FakeWorksheet("Leads", [COLUMNS + ["", ""]])
    sh = FakeSpreadsheet([ws, FakeWorksheet("Outreach", [sheets.OUTREACH_HEADER])])
    store.save_leads(ctx, [lead(1)])
    assert sheets.append_new(ctx, [lead(1)], client=FakeClient(sh)) == 1


def test_dry_run_touches_nothing():
    ctx = make_ctx(dry_run=True)
    store.save_leads(ctx, [lead(1)])
    client = mock.Mock()
    with mock.patch.object(sheets, "get_client") as gc:
        assert sheets.append_new(ctx, [lead(1)], client=client) == 0
        gc.assert_not_called()
    client.assert_not_called()
    assert client.method_calls == []
    assert len(store.pending_sheet_rows(ctx)) == 1


def test_retry_on_429_then_success():
    class Resp:
        status_code = 429

    class APIErr(Exception):
        response = Resp()

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise APIErr()
        return "ok"

    assert sheets._with_retry(fn, "x") == "ok" and calls["n"] == 3

    class Resp400:
        status_code = 400

    class Bad(Exception):
        response = Resp400()

    with pytest.raises(Bad):
        sheets._with_retry(lambda: (_ for _ in ()).throw(Bad()), "x")


def test_missing_sheet_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEET_ID")
    with pytest.raises(sheets.SheetsConfigError, match="GOOGLE_SHEET_ID"):
        sheets.sheet_url(make_ctx())


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    with pytest.raises(sheets.SheetsConfigError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        sheets.get_client()


def test_sheet_url():
    assert sheets.sheet_url(make_ctx()) == "https://docs.google.com/spreadsheets/d/SHEET123"


# ---------------------------------------------------------------- email content

def test_top3_ordering_and_tiebreak():
    leads = [
        lead(1, score=5, on_sale=""),
        lead(2, score=5, on_sale="2026-11-10"),
        lead(3, score=5, on_sale="2026-10-25"),
        lead(4, score=9, excluded="competitor"),
        lead(5, score=2, on_sale="2026-10-20"),
        lead(6, segment="jpop", score=10),
    ]
    assert [l.event_key for l in em.top_leads(leads, "kpop")] == ["k3", "k2", "k1"]
    assert [l.event_key for l in em.top_leads(leads, "jpop")] == ["k6"]


def test_why_now():
    l = lead(1, on_sale="2026-10-29", scalping_evidence_summary="Resale at 4x face on StubHub")
    s = em.why_now(l, date(2026, 10, 19))
    assert "on sale in 10 days (2026-10-29)" in s and "scalping: Resale at 4x" in s and "score 3" in s


def test_summary_content_and_html_escaping():
    ctx = make_ctx()
    leads = [lead(1, seller="<script>alert(1)</script>", event="Tom & Jerry <Live>"), lead(2, segment="jpop")]
    subject, text, body = em.build_summary(ctx, leads, {"kpop": 4, "jpop": 1})
    assert "5 new events" in subject
    assert "<script>" not in body and "&lt;script&gt;" in body and "Tom &amp; Jerry &lt;Live&gt;" in body
    assert "kpop: 4" in text and "Why now:" in text
    assert "https://docs.google.com/spreadsheets/d/SHEET123" in text and 'href="https://docs.google.com' in body


# ---------------------------------------------------------------- sending

def test_dry_run_sends_nothing(monkeypatch):
    ctx = make_ctx(dry_run=True)
    with mock.patch.object(smtplib, "SMTP_SSL") as smtp, mock.patch.object(em.requests, "post") as post:
        em.send_summary(ctx, [lead(1)], {"kpop": 1})
        em.send_failure(ctx, "boom")
        em.send_test(ctx)
    smtp.assert_not_called()
    post.assert_not_called()


def test_gmail_missing_env():
    with pytest.raises(em.EmailConfigError, match="GMAIL_SENDER, GMAIL_APP_PASSWORD"):
        em.send_test(make_ctx())


def test_gmail_smtp_sends_multipart_to_config_recipients_only(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER", "bot@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    ctx = make_ctx(recipients=["a@x.com", "b@y.com"])
    with mock.patch.object(smtplib, "SMTP_SSL") as smtp_cls:
        em.send_summary(ctx, [lead(1, seller="A&B")], {"kpop": 1})
    smtp_cls.assert_called_once_with("smtp.gmail.com", 465, timeout=60)
    smtp = smtp_cls.return_value.__enter__.return_value
    smtp.login.assert_called_once_with("bot@gmail.com", "pw")
    msg = smtp.send_message.call_args.args[0]
    assert smtp.send_message.call_args.kwargs["to_addrs"] == ["a@x.com", "b@y.com"]
    assert msg.get_content_type() == "multipart/alternative"
    parsed = message_from_bytes(msg.as_bytes())
    types = [p.get_content_type() for p in parsed.walk()]
    assert "text/plain" in types and "text/html" in types
    assert msg["To"] == "a@x.com, b@y.com"


@responses.activate
def test_resend(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_123")
    monkeypatch.setenv("RESEND_FROM", "Argos <bot@argos.example>")
    responses.add(responses.POST, em.RESEND_URL, json={"id": "abc"}, status=200)
    ctx = make_ctx(method="resend")
    em.send_failure(ctx, "pipeline <broke>")
    assert len(responses.calls) == 1
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer re_123"
    import json
    body = json.loads(req.body)
    assert body["to"] == ["team@example.com"] and body["from"] == "Argos <bot@argos.example>"
    assert "pipeline &lt;broke&gt;" in body["html"] and "pipeline <broke>" in body["text"]


@responses.activate
def test_resend_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_123")
    monkeypatch.setenv("RESEND_FROM", "bot@argos.example")
    responses.add(responses.POST, em.RESEND_URL, json={"message": "bad"}, status=422)
    with pytest.raises(em.EmailSendError, match="422"):
        em.send_test(make_ctx(method="resend"))


def test_resend_missing_env():
    with pytest.raises(em.EmailConfigError, match="RESEND_API_KEY, RESEND_FROM"):
        em.send_test(make_ctx(method="resend"))


def test_no_recipients():
    with pytest.raises(em.EmailConfigError, match="no recipients"):
        em.send_test(make_ctx(recipients=[]))
