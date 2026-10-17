"""Tests for argos_leads.history (no network: fake LLM)."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from argos_leads import history
from argos_leads.config import load_config
from argos_leads.db import Database
from argos_leads.models import Lead

ARTIST_HIT = history.ScalpingFinding(
    found=True, summary="Bots and resellers swept presale tickets for the 2023 U.S. tour, pushing resale above $1,000.",
    url="https://example-news.com/artist-scalping")
SELLER_HIT = history.ScalpingFinding(
    found=True, summary="Seller faced bot attacks during a 2024 on-sale.", url="https://news.example.org/seller-bots")
MISS = history.ScalpingFinding(found=False, summary="", url="")


class FakeLLM:
    """Answers by matching the quoted name in the question."""

    def __init__(self, answers: dict[str, history.ScalpingFinding] | None = None):
        self.answers = answers or {}
        self.calls: list[str] = []

    def search(self, output, question, max_searches=2):
        assert output is history.ScalpingFinding
        assert max_searches <= 2
        self.calls.append(question)
        for name, ans in self.answers.items():
            if f'"{name}"' in question:
                return ans
        return MISS


def make_ctx(llm, db=None, today=date(2026, 10, 17)):
    return SimpleNamespace(config=load_config(), db=db or Database(":memory:"), llm=llm, today=today)


def lead(artist="SEVENTEEN", seller="SeatGeek", city="Newark", key=None):
    return Lead(event_key=key or f"{artist}-{city}", week_found="2026-W42", segment="kpop",
                event=f"{artist} Tour", artist_or_headliner=artist, city=city, ticket_seller=seller)


def test_artist_hit():
    llm = FakeLLM({"SEVENTEEN": ARTIST_HIT})
    ls = [lead()]
    history.apply(make_ctx(llm), ls)
    assert ls[0].scalping_evidence_url == ARTIST_HIT.url
    assert ls[0].scalping_evidence_summary.startswith("Bots and resellers")
    assert len(llm.calls) == 1  # no seller search when artist hits


def test_artist_miss_falls_back_to_seller():
    llm = FakeLLM({"SeatGeek": SELLER_HIT})
    ls = [lead()]
    history.apply(make_ctx(llm), ls)
    assert len(llm.calls) == 2
    assert "SEVENTEEN" in llm.calls[0] and "SeatGeek" in llm.calls[1]
    assert ls[0].scalping_evidence_summary.startswith("Seller SeatGeek: ")
    assert ls[0].scalping_evidence_url == SELLER_HIT.url


def test_both_miss_blank():
    llm = FakeLLM()
    ls = [lead()]
    ls[0].scalping_evidence_summary = "stale"
    history.apply(make_ctx(llm), ls)
    assert ls[0].scalping_evidence_summary == "" and ls[0].scalping_evidence_url == ""
    assert len(llm.calls) == 2


def test_no_seller_only_artist_search():
    llm = FakeLLM()
    ls = [lead(seller="")]
    history.apply(make_ctx(llm), ls)
    assert len(llm.calls) == 1


def test_max_two_searches_per_event_and_dedupe_across_cities():
    llm = FakeLLM()
    ls = [lead(city=c) for c in ("Newark", "Chicago", "Los Angeles", "Houston")]
    history.apply(make_ctx(llm), ls)
    assert len(llm.calls) == 2  # one artist + one seller for all four city rows


def test_cache_reuse_across_runs():
    db = Database(":memory:")
    llm = FakeLLM({"SEVENTEEN": ARTIST_HIT})
    history.apply(make_ctx(llm, db), [lead()])
    llm2 = FakeLLM()
    ls = [lead(artist="Seventeen", city="Seattle")]  # same artist, different spelling
    history.apply(make_ctx(llm2, db, today=date(2026, 10, 17) + timedelta(days=60)), ls)
    assert llm2.calls == []
    assert ls[0].scalping_evidence_url == ARTIST_HIT.url


def test_stale_cache_researches():
    db = Database(":memory:")
    history.apply(make_ctx(FakeLLM({"SEVENTEEN": ARTIST_HIT}), db), [lead()])
    llm2 = FakeLLM({"SEVENTEEN": ARTIST_HIT})
    history.apply(make_ctx(llm2, db, today=date(2026, 10, 17) + timedelta(days=91)), [lead()])
    assert len(llm2.calls) == 1


def test_negative_caching():
    db = Database(":memory:")
    history.apply(make_ctx(FakeLLM(), db), [lead()])
    row = db.one("SELECT summary, url FROM scalping_cache WHERE kind='artist' AND key_norm='seventeen'")
    assert row["summary"] == "" and row["url"] == ""
    llm2 = FakeLLM({"SEVENTEEN": ARTIST_HIT, "SeatGeek": SELLER_HIT})
    ls = [lead()]
    history.apply(make_ctx(llm2, db, today=date(2026, 10, 24)), ls)
    assert llm2.calls == []  # both artist and seller misses cached
    assert ls[0].scalping_evidence_url == ""


def test_llm_failure_not_cached():
    class Failing(FakeLLM):
        def search(self, output, question, max_searches=2):
            self.calls.append(question)
            return None

    db = Database(":memory:")
    history.apply(make_ctx(Failing(), db), [lead()])
    assert db.one("SELECT COUNT(*) AS n FROM scalping_cache")["n"] == 0


def test_found_without_valid_url_rejected():
    bad = history.ScalpingFinding(found=True, summary="Bots bought tickets.", url="not a url")
    ls = [lead(seller="")]
    history.apply(make_ctx(FakeLLM({"SEVENTEEN": bad})), ls)
    assert ls[0].scalping_evidence_summary == "" and ls[0].scalping_evidence_url == ""


def test_summary_one_line_and_length():
    long = history.ScalpingFinding(
        found=True,
        summary="Fans reported\nbots " + "and resale markups " * 30,
        url="https://example.com/x")
    ls = [lead()]
    history.apply(make_ctx(FakeLLM({"SEVENTEEN": long})), ls)
    s = ls[0].scalping_evidence_summary
    assert "\n" not in s and len(s) <= history.SUMMARY_MAX
    assert s.startswith("Fans reported bots")


def test_clean_summary_drops_long_quotes_and_keeps_first_sentence():
    text = 'Resale prices spiked. "' + "x" * 60 + '" More text follows here.'
    assert history.clean_summary(text) == "Resale prices spiked."
    s = history.clean_summary('Report: "' + "copied words " * 10 + '" bots hit the presale')
    assert "copied" not in s


def test_seller_summary_fits_limit():
    llm = FakeLLM({"A Very Long Ticketing Company Name LLC": history.ScalpingFinding(
        found=True, summary="y " * 100, url="https://example.com/s")})
    ls = [lead(seller="A Very Long Ticketing Company Name LLC")]
    history.apply(make_ctx(llm), ls)
    assert ls[0].scalping_evidence_summary.startswith("Seller A Very Long")
    assert len(ls[0].scalping_evidence_summary) <= history.SUMMARY_MAX


@pytest.mark.parametrize("url,ok", [("https://a.com/x", True), ("http://b.org", True), ("ftp://a.com", False),
                                    ("", False), ("https://localhost/x", False), ("example.com/x", False)])
def test_valid_url(url, ok):
    assert history.valid_url(url) is ok
