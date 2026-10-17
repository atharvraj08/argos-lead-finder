"""Competitor detection tests: no network (responses), fake LLM."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import responses

import argos_leads.competitors as comp
from argos_leads.competitors import CompetitorResult, SearchAnswer, SearchFinding, analyze_scripts, check_seller
from argos_leads.competitors.vendors import vendors_for
from argos_leads.config import load_config
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.http import HttpClient
from argos_leads.models import Lead

FX = Path(__file__).parent / "fixtures" / "competitors"
TODAY = date(2026, 10, 17)


def fx(name: str) -> str:
    return (FX / name).read_text()


class FakeLLM:
    def __init__(self, answer: SearchAnswer | None = None):
        self.answer = answer
        self.calls: list[str] = []

    def search(self, output, question, max_searches=2):
        self.calls.append(question)
        return self.answer

    def extract(self, *a, **k):  # pragma: no cover - not used here
        return None


@pytest.fixture
def make_ctx(monkeypatch):
    def _make(homepages: dict[str, str] | None = None, answer: SearchAnswer | None = None) -> RunContext:
        homepages = homepages or {}
        monkeypatch.setattr(comp, "_homepage", lambda seller: homepages.get(seller))
        db = Database(":memory:")
        http = HttpClient(db=None, default_min_interval_s=0, check_robots=False, max_retries=0)
        return RunContext(config=load_config(), db=db, http=http, llm=FakeLLM(answer), today=TODAY)
    return _make


def lead(seller: str, key: str = "e1") -> Lead:
    return Lead(event_key=key, week_found="2026-W42", segment="kpop", event="Tour", artist_or_headliner="A",
                ticket_seller=seller)


@responses.activate
def test_script_signature_confirmed(make_ctx):
    ctx = make_ctx({"TixHouse": "https://tixhouse.example/"})
    responses.get("https://tixhouse.example/", body=fx("home_persona_login.html"))
    responses.get("https://tixhouse.example/login", body=fx("login_persona.html"))
    responses.get("https://tixhouse.example/privacy", status=404)
    res = check_seller(ctx, "TixHouse")
    assert res == CompetitorResult("Persona", "confirmed", "https://tixhouse.example/login")
    assert ctx.llm.calls == []  # confirmed from pages -> no search spent


@responses.activate
def test_subprocessor_list_confirmed(make_ctx):
    ctx = make_ctx({"FanTix": "https://www.fantix.example/"})
    responses.get("https://www.fantix.example/", body=fx("home_policy.html"))
    responses.get("https://www.fantix.example/legal/privacy", body=fx("privacy_with_subprocessors_link.html"))
    responses.get("https://www.fantix.example/legal/subprocessors", body=fx("subprocessors.html"))
    res = check_seller(ctx, "FanTix")
    assert res.vendor == "Onfido"
    assert res.status == "confirmed"
    assert res.evidence_url == "https://www.fantix.example/legal/subprocessors"


@responses.activate
def test_ambiguous_mention_likely(make_ctx):
    ctx = make_ctx({"AmbiTix": "https://ambitix.example/"})
    responses.get("https://ambitix.example/", body=fx("home_policy.html"))
    responses.get("https://ambitix.example/legal/privacy", body=fx("privacy_ambiguous.html"))
    res = check_seller(ctx, "AmbiTix")
    assert res == CompetitorResult("Veriff", "likely", "https://ambitix.example/legal/privacy")
    assert len(ctx.llm.calls) == 1  # not confirmed -> one search attempted


@responses.activate
def test_nothing_found_unknown(make_ctx):
    ctx = make_ctx({"PlainTix": "https://plaintix.example/"})
    responses.get("https://plaintix.example/", body=fx("home_plain.html"))
    responses.get("https://plaintix.example/help", body=fx("help_plain.html"))
    res = check_seller(ctx, "PlainTix")
    assert res == CompetitorResult("", "unknown", "")


def test_stripe_payments_not_stripe_identity():
    vendors = vendors_for(load_config().targets.competitor_vendors)
    assert analyze_scripts(fx("home_plain.html"), "https://plaintix.example/", vendors) == []
    html = '<script src="https://js.stripe.com/v3/"></script><script>stripe.verifyIdentity(secret)</script>'
    found = analyze_scripts(html, "https://x.example/verify", vendors)
    assert [f.vendor for f in found] == ["Stripe Identity"]


def test_no_homepage_uses_search_only(make_ctx):
    answer = SearchAnswer(findings=[
        SearchFinding(vendor="Jumio", evidence_url="https://www.jumio.com/case-study/bigtix/", explicit=True),
        SearchFinding(vendor="NotAVendor", evidence_url="https://x.example/", explicit=True),
        SearchFinding(vendor="Socure", evidence_url="", explicit=True),  # no URL -> ignored
    ])
    ctx = make_ctx({}, answer)
    res = check_seller(ctx, "BigTix")
    assert res == CompetitorResult("Jumio", "confirmed", "https://www.jumio.com/case-study/bigtix/")


def test_indirect_search_is_likely(make_ctx):
    ctx = make_ctx({}, SearchAnswer(findings=[SearchFinding(vendor="id.me", evidence_url="https://n.example/a")]))
    assert check_seller(ctx, "BigTix") == CompetitorResult("ID.me", "likely", "https://n.example/a")


def test_cache_hit_skips_fetches(make_ctx):
    ctx = make_ctx({"TixHouse": "https://tixhouse.example/"})
    ctx.db.execute("INSERT INTO competitor_cache VALUES (?,?,?,?,?,?)",
                   ("tixhouse", "TixHouse", "Persona", "confirmed", "https://tixhouse.example/login",
                    (TODAY - timedelta(days=10)).isoformat() + "T00:00:00+00:00"))
    with responses.RequestsMock() as rsps:  # any HTTP call would raise ConnectionError
        res = check_seller(ctx, "TixHouse")
        assert len(rsps.calls) == 0
    assert res == CompetitorResult("Persona", "confirmed", "https://tixhouse.example/login")
    assert ctx.llm.calls == []


@responses.activate
def test_stale_cache_rechecks(make_ctx):
    ctx = make_ctx({"PlainTix": "https://plaintix.example/"})
    ctx.db.execute("INSERT INTO competitor_cache VALUES (?,?,?,?,?,?)",
                   ("plaintix", "PlainTix", "Persona", "confirmed", "https://old.example/",
                    (TODAY - timedelta(days=31)).isoformat() + "T00:00:00+00:00"))
    responses.get("https://plaintix.example/", body=fx("home_plain.html"))
    responses.get("https://plaintix.example/help", body=fx("help_plain.html"))
    res = check_seller(ctx, "PlainTix")
    assert res.status == "unknown"
    assert len(responses.calls) == 2 and len(ctx.llm.calls) == 1
    row = ctx.db.one("SELECT * FROM competitor_cache WHERE seller_norm='plaintix'")
    assert row["status"] == "unknown" and row["vendor"] == "" and row["checked_at"] > "2026-01-01"


def test_apply_checks_each_seller_once(make_ctx, monkeypatch):
    ctx = make_ctx()
    calls = []

    def fake_check(c, seller):
        calls.append(seller)
        return CompetitorResult("Veriff", "likely", "https://e.example/") if seller == "A" else comp.UNKNOWN

    monkeypatch.setattr(comp, "check_seller", fake_check)
    leads = [lead("A", "1"), lead("a", "2"), lead("B", "3"), lead("", "4")]
    comp.apply(ctx, leads)
    assert calls == ["A", "B"]
    assert [(l.competitor_vendor, l.competitor_status) for l in leads] == [
        ("Veriff", "likely"), ("Veriff", "likely"), ("", "unknown"), ("", "unknown")]
    assert all(l.excluded_reason == "" for l in leads)


def test_persona_concert_listing_is_not_evidence():
    vendors = vendors_for(["Persona"])
    text = comp.page_text(fx("home_persona_login.html"))
    assert comp.analyze_text(text, "https://t.example/", vendors, kind="page_text") == []
