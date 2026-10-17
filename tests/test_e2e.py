"""End-to-end: pipeline.run_weekly over both segments with no network.

Feeds and article pages come from tests/fixtures/e2e via `responses`; anything else
(robots.txt, seller homepages) gets a 404. The LLM is a fake returning canned pydantic
outputs, Google Sheets is a fake gspread client, SMTP is mocked. Discovery runs its real
RSS adapter, but every event field comes from the fake LLM, so these tests do not depend
on any adapter's own HTML/feed parsing beyond feedparser reading the fixture feeds.
"""
from __future__ import annotations

import hashlib
import re
import smtplib
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
import responses
import yaml

from argos_leads import __main__ as cli
from argos_leads import context as context_mod
from argos_leads import history, pipeline
from argos_leads.competitors import SearchAnswer, SearchFinding
from argos_leads.config import CONFIG_DIR, Config, EmailConfig, TargetsConfig
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.discovery.extract import ExtractedDate, ExtractedEvent, ExtractedEvents
from argos_leads.enrich import PageFacts
from argos_leads.history import ScalpingFinding
from argos_leads.http import HttpClient
from argos_leads.models import COLUMNS
from argos_leads.output import sheets
from argos_leads.output.sheets import OUTREACH_HEADER

FIX = Path(__file__).parent / "fixtures" / "e2e"
TODAY = date(2026, 10, 19)  # Monday, 2026-W43
RECIPIENTS = ["team@example.com", "lead@example.org"]

KPOP_FEED = "https://feeds.example.com/kpop.xml"
JPOP_FEED = "https://feeds.example.com/jpop.xml"
SKZ_URL = "https://news.example.com/kpop/stray-kids-tour"
ATEEZ_URL = "https://news.example.com/kpop/ateez-fanmeeting"
YOASOBI_URL = "https://news.example.com/jpop/yoasobi-na-tour"
ETIX_EVIDENCE = "https://press.example.com/etix-persona"
SKZ_SCALPING = "https://news.example.com/skz-resale"


# ------------------------------------------------------------------ fakes

def D(city, state, country, venue, day, ticket=""):
    return ExtractedDate(city=city, state=state, country=country, venue=venue, date=day, ticket_url=ticket)


EXTRACTED = {
    SKZ_URL: ExtractedEvents(events=[ExtractedEvent(
        artist="Stray Kids", event_name="dominATE World Tour", on_sale_date="2026-10-25",
        dates=[
            D("Los Angeles", "CA", "US", "Crypto.com Arena", "2026-12-05", "https://www.axs.com/events/111/stray-kids"),
            D("Newark", "NJ", "US", "Prudential Center", "2026-12-09", "https://www.ticketmaster.com/event/222"),
            D("Toronto", "ON", "Canada", "Scotiabank Arena", "2026-12-12", "https://www.ticketmaster.ca/event/333"),
        ])]),
    ATEEZ_URL: ExtractedEvents(events=[ExtractedEvent(
        artist="ATEEZ", event_name="ATEEZ Fan Meeting", on_sale_date="2026-10-30",
        dates=[
            D("Chicago", "IL", "US", "Rosemont Theatre", "2026-11-20", "https://www.etix.com/ticket/p/444"),
            D("Dallas", "TX", "US", "Toyota Music Factory", "2026-09-01"),  # past: dropped
        ])]),
    YOASOBI_URL: ExtractedEvents(events=[ExtractedEvent(
        artist="YOASOBI", event_name="YOASOBI North American Tour", agency="Sony Music Entertainment Japan",
        on_sale_date="2026-11-01",
        dates=[
            D("Seattle", "WA", "US", "Climate Pledge Arena", "2026-11-28", "https://dice.fm/event/yoasobi-seattle"),
            D("San Francisco", "CA", "US", "Bill Graham Civic Auditorium", "2026-11-30"),
        ])]),
}


class FakeLLM:
    available = True

    def __init__(self):
        self.extract_calls: list[tuple[str, str]] = []
        self.search_calls: list[tuple[str, str]] = []

    def extract(self, output, instructions, page_text, url=""):
        self.extract_calls.append((output.__name__, url))
        if output is ExtractedEvents:
            return EXTRACTED.get(url, ExtractedEvents())
        if output is PageFacts:
            return PageFacts(promoter="Live Nation") if "Live Nation" in page_text else PageFacts()
        raise AssertionError(f"unexpected extract schema {output}")

    def search(self, output, question, max_searches=2):
        self.search_calls.append((output.__name__, question))
        if output is SearchAnswer:
            if '"Etix"' in question:
                return SearchAnswer(findings=[SearchFinding(vendor="Persona", evidence_url=ETIX_EVIDENCE,
                                                            explicit=True, summary="Etix uses Persona")])
            return SearchAnswer()
        if output is ScalpingFinding:
            if '"Stray Kids"' in question:
                return ScalpingFinding(found=True, summary="Resale prices for Stray Kids shows hit 5x face value.",
                                       url=SKZ_SCALPING)
            return ScalpingFinding()
        raise AssertionError(f"unexpected search schema {output}")


class Forbidden(AssertionError):
    pass


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.values: list[list] = []
        self.append_calls: list[list[list]] = []

    def get_all_values(self):
        return [[str(c) for c in r] for r in self.values]

    def append_rows(self, rows, value_input_option=None, insert_data_option=None, **kw):
        assert value_input_option == "RAW"
        self.append_calls.append([list(r) for r in rows])
        self.values.extend([list(r) for r in rows])

    def __getattr__(self, name):  # any mutating call other than append_rows is a bug
        raise Forbidden(f"forbidden worksheet call: {name}")


class FakeSpreadsheet:
    def __init__(self):
        self.tabs: dict[str, FakeWorksheet] = {}
        self.created: list[str] = []

    def worksheets(self):
        return list(self.tabs.values())

    def worksheet(self, title):
        return self.tabs[title]

    def add_worksheet(self, title, rows, cols):
        if title in self.tabs:
            raise Forbidden(f"tab {title} created twice")
        ws = FakeWorksheet(title)
        self.tabs[title] = ws
        self.created.append(title)
        return ws


class FakeClient:
    def __init__(self):
        self.sh = FakeSpreadsheet()
        self.opened = 0

    def open_by_key(self, key):
        assert key == "SHEET-E2E"
        self.opened += 1
        return self.sh


# ------------------------------------------------------------------ setup

def e2e_config() -> Config:
    raw = yaml.safe_load((CONFIG_DIR / "targets.yaml").read_text())
    raw["active_segments"] = ["kpop", "jpop"]
    raw["segments"]["kpop"]["sources"] = [{"slug": "e2e_kpop_rss", "adapter": "rss", "url": KPOP_FEED}]
    raw["segments"]["jpop"]["sources"] = [{"slug": "e2e_jpop_rss", "adapter": "rss", "url": JPOP_FEED}]
    raw["http"] = {"contact_email": "qa@example.com", "default_min_interval_s": 0, "host_min_interval_s": {}}
    return Config(targets=TargetsConfig(**raw), email=EmailConfig(recipients=RECIPIENTS, method="gmail_smtp"))


def make_ctx(db_path, llm, dry_run=False) -> RunContext:
    db = Database(db_path)
    http = HttpClient(db=db, contact_email="qa@example.com", default_min_interval_s=0, max_retries=0)
    return RunContext(config=e2e_config(), db=db, http=http, llm=llm, today=TODAY, dry_run=dry_run)


def register_fixtures(rsps):
    rsps.add(responses.GET, KPOP_FEED, body=(FIX / "kpop_feed.xml").read_text(), content_type="application/rss+xml")
    rsps.add(responses.GET, JPOP_FEED, body=(FIX / "jpop_feed.xml").read_text(), content_type="application/rss+xml")
    for url, name in ((SKZ_URL, "stray_kids_article.html"), (ATEEZ_URL, "ateez_article.html"),
                      (YOASOBI_URL, "yoasobi_article.html")):
        rsps.add(responses.GET, url, body=(FIX / name).read_text(), content_type="text/html")
    # Everything else (robots.txt, seller homepages, ticket pages): 404.
    rsps.add(responses.GET, re.compile(r"https?://.*"), status=404)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET-E2E")
    monkeypatch.setenv("GMAIL_SENDER", "bot@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")
    monkeypatch.setattr(sheets, "_sleep", lambda s: None)
    client = FakeClient()
    monkeypatch.setattr(sheets, "get_client", lambda: client)
    history_calls: list[list] = []
    real_apply = history.apply

    def spy(ctx, leads):
        history_calls.append(list(leads))
        return real_apply(ctx, leads)

    monkeypatch.setattr(history, "apply", spy)
    return client, history_calls


def run_once(db_path, llm):
    ctx = make_ctx(db_path, llm)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            mock.patch.object(smtplib, "SMTP_SSL") as smtp_cls:
        register_fixtures(rsps)
        stats = pipeline.run_weekly(ctx)
    return ctx, stats, smtp_cls


def leads_by_artist_city(sheet_rows):
    ia, ic = COLUMNS.index("artist_or_headliner"), COLUMNS.index("city")
    return {(r[ia], r[ic]): dict(zip(COLUMNS, r)) for r in sheet_rows}


# ------------------------------------------------------------------ tests

def test_weekly_run_end_to_end(tmp_path, env):
    client, history_calls = env
    llm = FakeLLM()
    db_path = tmp_path / "argos.db"
    ctx, stats, smtp_cls = run_once(db_path, llm)

    # --- stats / discovery
    assert stats["segments"]["kpop"]["new_events"] == 3  # LA, Newark, Chicago (Toronto non-US, Dallas past)
    assert stats["segments"]["jpop"]["new_events"] == 2  # Seattle, San Francisco
    assert stats["appended"] == 3

    # --- sheet: exactly COLUMNS, both segments, no excluded rows
    sh = client.sh
    assert sh.created == ["Leads", "Outreach"]
    leads_ws = sh.tabs["Leads"]
    assert leads_ws.values[0] == COLUMNS
    rows = leads_ws.values[1:]
    assert all(len(r) == len(COLUMNS) for r in rows)
    by = leads_by_artist_city(rows)
    assert set(by) == {("Stray Kids", "Los Angeles, CA"), ("YOASOBI", "Seattle, WA"),
                       ("YOASOBI", "San Francisco, CA")}
    assert {r["segment"] for r in by.values()} == {"kpop", "jpop"}
    sellers = {r["ticket_seller"] for r in by.values()}
    assert "Ticketmaster" not in sellers and "Etix" not in sellers
    assert not any(r["artist_or_headliner"] == "ATEEZ" for r in by.values())

    skz = by[("Stray Kids", "Los Angeles, CA")]
    assert skz["week_found"] == "2026-W43"
    assert skz["ticket_seller"] == "AXS"
    assert skz["promoter"] == "Live Nation"
    assert skz["scalping_evidence_url"] == SKZ_SCALPING
    assert skz["score"] == 7  # scalping 3 + on-sale soon 2 + promoter 1 + large venue 1
    assert skz["score_breakdown"] == "scalping +3, on-sale soon +2, promoter +1, large venue +1 = 7"
    yo = by[("YOASOBI", "Seattle, WA")]
    assert yo["ticket_seller"] == "DICE" and yo["agency"] == "Sony Music Entertainment Japan"


def test_weekly_run_end_to_end_details(tmp_path, env):
    client, history_calls = env
    llm = FakeLLM()
    db_path = tmp_path / "argos.db"
    ctx, stats, smtp_cls = run_once(db_path, llm)

    # --- DB: excluded rows saved with reasons, not appended
    rows = {r["event_key"]: r for r in ctx.db.query("SELECT * FROM leads")}
    assert len(rows) == 5
    excl = {k: r["excluded_reason"] for k, r in rows.items() if r["excluded_reason"]}
    assert excl == {"stray kids|newark|2026-12-09": "excluded seller",
                    "ateez|chicago|2026-11-20": "competitor confirmed: Persona"}
    for k in excl:
        assert rows[k]["appended_to_sheet_at"] is None
    assert all(rows[k]["appended_to_sheet_at"] for k in rows if k not in excl)

    # --- provenance: every lead has sources; every filled optional role has a URL
    from argos_leads.output.store import lead_from_json
    for r in rows.values():
        lead = lead_from_json(r["data_json"])
        assert lead.sources.strip(), lead.event_key
        for role in ("agency", "promoter", "organizer", "ticket_seller"):
            if getattr(lead, role):
                assert lead.field_sources.get(role, "").startswith("http"), (lead.event_key, role)
        if lead.event_key == "stray kids|los angeles|2026-12-05":
            assert lead.field_sources["promoter"] == SKZ_URL
        if lead.event_key == "yoasobi|seattle|2026-11-28":
            assert lead.field_sources["agency"] == YOASOBI_URL

    # --- history only for non-excluded leads
    assert len(history_calls) == 2  # once per segment
    hist_keys = {l.event_key for call in history_calls for l in call}
    assert not hist_keys & set(excl)
    scalping_questions = [q for name, q in llm.search_calls if name == "ScalpingFinding"]
    assert scalping_questions and not any("ATEEZ" in q for q in scalping_questions)
    assert not any("Ticketmaster" in q for q in scalping_questions)

    # --- email: only config recipients, per-segment counts, top 3
    smtp_cls.assert_called_once()
    smtp = smtp_cls.return_value.__enter__.return_value
    smtp.send_message.assert_called_once()
    assert smtp.send_message.call_args.kwargs["to_addrs"] == RECIPIENTS
    msg = smtp.send_message.call_args.args[0]
    assert msg["To"] == ", ".join(RECIPIENTS)
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "  - kpop: 3" in text and "  - jpop: 2" in text
    kpop_top = text.split("Top leads: kpop")[1].split("Top leads: jpop")[0]
    jpop_top = text.split("Top leads: jpop")[1]
    assert "1. AXS | dominATE World Tour | Crypto.com Arena, Los Angeles, CA | 2026-12-05" in kpop_top
    assert "Ticketmaster" not in kpop_top and "ATEEZ" not in kpop_top and "2." not in kpop_top
    assert "1. DICE | YOASOBI North American Tour" in jpop_top
    assert "2. (seller unknown) | YOASOBI North American Tour" in jpop_top
    assert "https://docs.google.com/spreadsheets/d/SHEET-E2E" in text

    # --- Outreach seeded once with this run's sellers
    outreach = client.sh.tabs["Outreach"].values
    assert outreach[0] == OUTREACH_HEADER
    assert [r[0] for r in outreach[1:]] == ["AXS", "DICE"]


def test_yoasobi_score_breakdown(tmp_path, env):
    client, _ = env
    run_once(tmp_path / "argos.db", FakeLLM())
    by = leads_by_artist_city(client.sh.tabs["Leads"].values[1:])
    yo = by[("YOASOBI", "Seattle, WA")]
    assert yo["score_breakdown"] == "on-sale soon +2, agency +1, large venue +1 = 4"
    assert yo["score"] == 4
    sf = by[("YOASOBI", "San Francisco, CA")]
    assert sf["ticket_seller"] == ""  # never guessed


def test_second_run_appends_nothing_and_keeps_rows(tmp_path, env):
    client, _ = env
    db_path = tmp_path / "argos.db"
    run_once(db_path, FakeLLM())
    ws = client.sh.tabs["Leads"]
    before = [list(r) for r in ws.values]
    outreach_before = [list(r) for r in client.sh.tabs["Outreach"].values]
    n_calls = len(ws.append_calls)

    llm2 = FakeLLM()
    _, stats2, smtp2 = run_once(db_path, llm2)
    assert stats2["appended"] == 0
    assert stats2["segments"]["kpop"]["new_events"] == 0
    assert stats2["segments"]["jpop"]["new_events"] == 0
    assert len(ws.append_calls) == n_calls
    assert ws.values == before  # existing rows never modified
    assert client.sh.tabs["Outreach"].values == outreach_before
    assert client.sh.created == ["Leads", "Outreach"]  # Outreach created once
    assert not [c for c in llm2.extract_calls if c[0] == "ExtractedEvents"]  # items already processed
    text = smtp2.return_value.__enter__.return_value.send_message.call_args.args[0] \
        .get_body(preferencelist=("plain",)).get_content()
    assert "  - kpop: 0" in text and "  - jpop: 0" in text


def test_run_single_segment(tmp_path, env):
    client, _ = env
    ctx = make_ctx(tmp_path / "argos.db", FakeLLM())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            mock.patch.object(smtplib, "SMTP_SSL"):
        register_fixtures(rsps)
        stats = pipeline.run_weekly(ctx, segment="jpop")
        assert not [c for c in rsps.calls if c.request.url == KPOP_FEED]
    assert list(stats["segments"]) == ["jpop"]
    assert stats["appended"] == 2


def test_unknown_segment_raises(tmp_path, env):
    ctx = make_ctx(tmp_path / "argos.db", FakeLLM())
    with pytest.raises(ValueError, match="unknown segment"):
        pipeline.run_weekly(ctx, segment="nope")


# ------------------------------------------------------------------ dry run via the CLI

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_cli_dry_run_uses_db_copy_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "SHEET-E2E")
    db_path = tmp_path / "argos.db"
    seed = Database(db_path)
    seed.execute("INSERT INTO runs (kind, started_at, status) VALUES ('weekly', '2026-10-12T12:00:00+00:00', 'ok')")
    seed.close()
    digest = _sha(db_path)

    built: list[RunContext] = []

    def fake_build_context(config_dir=None, db_path=None, dry_run=False):
        ctx = make_ctx(db_path, FakeLLM(), dry_run=dry_run)
        built.append(ctx)
        return ctx

    monkeypatch.setattr(context_mod, "build_context", fake_build_context)

    def no_client():
        raise AssertionError("dry run must not open the sheet")

    monkeypatch.setattr(sheets, "get_client", no_client)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            mock.patch.object(smtplib, "SMTP_SSL") as smtp_cls, \
            mock.patch("argos_leads.output.email.requests.post") as post:
        register_fixtures(rsps)
        assert cli.main(["--db", str(db_path), "run", "--dry-run"]) == 0
    smtp_cls.assert_not_called()
    post.assert_not_called()

    assert len(built) == 1 and built[0].dry_run
    used = Path(built[0].db.path)
    assert used != db_path and used.exists()
    assert _sha(db_path) == digest  # original DB untouched
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Validation:" in out
    # the copy saw the run; the original did not
    orig = Database(db_path)
    try:
        assert len(orig.query("SELECT * FROM runs")) == 1
        assert orig.query("SELECT * FROM leads") == []
    finally:
        orig.close()
    assert len(built[0].db.query("SELECT * FROM leads")) == 5
