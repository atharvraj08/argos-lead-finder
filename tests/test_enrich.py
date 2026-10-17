"""Enrichment tests: no network (responses), fake LLM, in-memory SQLite."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import responses

from argos_leads.config import load_config
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.enrich import PageFacts, enrich, html_to_text
from argos_leads.enrich.sellers import (canonical_seller_name, may_fetch, seller_from_url,
                                        seller_homepage)
from argos_leads.http import HttpClient
from argos_leads.models import EventRecord

FIX = Path(__file__).parent / "fixtures" / "enrich"
SRC = "https://kpopofficial.com/event/nova-los-angeles/"
TOUR = "https://mamasgeeky.com/2026/10/nova-us-tour/"


class FakeLLM:
    """Returns canned PageFacts keyed by (url) or (url, city); records every call."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.calls: list[tuple[str, str]] = []

    def extract(self, output, instructions, page_text, url=""):
        self.calls.append((url, instructions))
        for key, facts in self.answers.items():
            if isinstance(key, tuple):
                if key[0] == url and key[1] in instructions:
                    return facts
            elif key == url:
                return facts
        return None

    def search(self, *a, **k):  # enrichment must never web-search
        raise AssertionError("enrich must not call llm.search")


@pytest.fixture
def make_ctx():
    def _make(llm=None):
        db = Database(":memory:")
        http = HttpClient(db=None, check_robots=False, default_min_interval_s=0, max_retries=0)
        return RunContext(config=load_config(), db=db, http=http, llm=llm or FakeLLM(), today=date(2026, 10, 19))
    return _make


def rec(**kw) -> EventRecord:
    base = dict(source="kpopofficial_events", segment="kpop", event="NOVA Starlight 2026 US Tour", artist="NOVA",
                city="Los Angeles", state="CA", venue="The Novo", event_date="2026-12-05",
                on_sale_date="2026-10-24", source_url=SRC)
    base.update(kw)
    return EventRecord(**base)


def seed_seller(ctx, venue, city, seller, url="https://example-venue.com/tickets", updated="2026-10-01T00:00:00+00:00"):
    ctx.db.execute("INSERT INTO venue_seller VALUES (?,?,?,?,?,?,?)",
                   (venue.lower(), city.lower(), venue, city, seller, url, updated))


def seed_agency(ctx, artist, agency, url="https://news.example.com/nova", updated="2026-10-01T00:00:00+00:00"):
    ctx.db.execute("INSERT INTO artist_agency VALUES (?,?,?,?,?)", (artist.lower(), artist, agency, url, updated))


# ---------------------------------------------------------------- sellers mapping
@pytest.mark.parametrize("url,expected", [
    ("https://www.ticketmaster.com/event/0B00", "Ticketmaster"),
    ("https://concerts.livenation.com/nova/event/1", "Ticketmaster"),
    ("https://www.universe.com/events/x", "Universe"),
    ("https://www.ticketweb.com/event/x", "TicketWeb"),
    ("https://www.frontgatetickets.com/events/x", "Front Gate Tickets"),
    ("https://www.axs.com/events/1/x", "AXS"),
    ("https://www.aegpresents.com/tours/x", "AXS"),
    ("https://www.eventbrite.com/e/x-123", "Eventbrite"),
    ("https://dice.fm/event/x", "DICE"),
    ("https://www.etix.com/ticket/p/1", "Etix"),
    ("https://www.tixr.com/groups/x", "Tixr"),
    ("https://wl.seetickets.us/event/x", "See Tickets"),
    ("https://www.showclix.com/event/x", "ShowClix"),
    ("https://feverup.com/m/1", "Fever"),
    ("https://www.prekindle.com/event/x", "Prekindle"),
    ("https://x.ticketspice.com/y", "TicketSpice"),
    ("https://www.freshtix.com/events/x", "Freshtix"),
    ("https://gtx.evenue.net/cgi-bin/x", "Paciolan"),
    ("https://world.nol.com/en/ticket/x", "Interpark (NOL)"),
    ("https://ticket.yes24.com/x", "Yes24"),
    ("https://weverse.io/nova/notice/1", "Weverse"),
    ("https://www.mymusictaste.com/project/x", "MyMusicTaste"),
    ("https://kpopmasterz.com/x", "Kpop Masterz"),
    ("https://tickets.kohai.com/e/1", "Kohai Tickets"),
    ("https://premier.ticketek.com.au/x", "Ticketek"),
])
def test_seller_from_url_known(url, expected):
    assert seller_from_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://seatgeek.com/nova-tickets/x", "https://www.stubhub.com/x", "https://www.vividseats.com/x",
    "https://www.tickpick.com/x", "https://gametime.co/x", "https://instagram.com/nova", "https://bit.ly/abc",
    "", "not a url at all",
])
def test_seller_from_url_aggregators_and_non_sellers_blank(url):
    assert seller_from_url(url) == ""


def test_seller_from_url_unknown_domain_is_registrable_domain():
    assert seller_from_url("https://tickets.thewarfieldtheatre.com/e/1") == "thewarfieldtheatre.com"
    assert seller_from_url("https://box.venue.co.uk/x") == "venue.co.uk"


def test_homepage_and_aliases():
    assert seller_homepage("AXS") == "https://www.axs.com"
    assert seller_homepage("axs") == "https://www.axs.com"
    assert seller_homepage("thewarfieldtheatre.com") == "https://thewarfieldtheatre.com"
    assert seller_homepage("Some Unknown Co") is None
    assert canonical_seller_name("Ticketmaster.com") == "Ticketmaster"
    assert not may_fetch("https://www.ticketmaster.com/x") and not may_fetch("https://www.stubhub.com/x")
    assert may_fetch(SRC)


def test_html_to_text_keeps_links_and_drops_scripts():
    text = html_to_text((FIX / "event_page.html").read_text(), SRC)
    assert "https://www.etix.com/ticket/p/12345/nova-los-angeles" in text
    assert "tracking" not in text
    assert "Starlight Entertainment" in text


# ---------------------------------------------------------------- enrich behaviour
@responses.activate
def test_lookup_hit_avoids_fetch(make_ctx):
    ctx = make_ctx()
    seed_seller(ctx, "The Novo", "Los Angeles", "AXS", url="https://www.axs.com/venues/the-novo")
    seed_agency(ctx, "NOVA", "Starlight Entertainment")
    [lead] = enrich(ctx, [rec()])
    assert lead.ticket_seller == "AXS"
    assert lead.field_sources["ticket_seller"] == "https://www.axs.com/venues/the-novo"
    assert lead.agency == "Starlight Entertainment"
    assert len(responses.calls) == 0 and ctx.llm.calls == []
    assert lead.low_confidence == []


@responses.activate
def test_record_and_ticket_url_win_without_fetch(make_ctx):
    ctx = make_ctx()
    seed_agency(ctx, "NOVA", "Starlight Entertainment")
    [lead] = enrich(ctx, [rec(ticket_url="https://www.etix.com/ticket/p/1")])
    assert lead.ticket_seller == "Etix" and lead.field_sources["ticket_seller"] == "https://www.etix.com/ticket/p/1"
    assert len(responses.calls) == 0
    # Fresh evidence from this event is written to the lookup.
    row = ctx.db.one("SELECT * FROM venue_seller WHERE venue_norm='the novo' AND city_norm='los angeles'")
    assert row["seller"] == "Etix" and row["source_url"] == "https://www.etix.com/ticket/p/1" and row["updated_at"]


@responses.activate
def test_miss_fetches_page_and_extracts(make_ctx):
    responses.get(SRC, body=(FIX / "event_page.html").read_text(), content_type="text/html")
    llm = FakeLLM({SRC: PageFacts(official_ticket_url="https://www.etix.com/ticket/p/12345/nova-los-angeles",
                                  agency="Starlight Entertainment", promoter="Bright Stage Productions")})
    ctx = make_ctx(llm)
    [lead] = enrich(ctx, [rec(on_sale_date=None)])
    assert len(responses.calls) == 1 and len(llm.calls) == 1
    assert lead.ticket_seller == "Etix"
    assert lead.agency == "Starlight Entertainment" and lead.promoter == "Bright Stage Productions"
    for f in ("ticket_seller", "agency", "promoter"):
        assert lead.field_sources[f] == SRC
    assert SRC in lead.sources.split()
    assert lead.city == "Los Angeles, CA" and lead.event_key == "nova|los angeles|2026-12-05"
    assert lead.week_found == "2026-W43"
    # Both lookups updated with provenance.
    assert ctx.db.one("SELECT seller, source_url FROM venue_seller")["source_url"] == SRC
    row = ctx.db.one("SELECT agency, source_url FROM artist_agency WHERE artist_norm='nova'")
    assert tuple(row) == ("Starlight Entertainment", SRC)


@responses.activate
def test_fresh_page_evidence_overrides_stale_cache(make_ctx):
    responses.get(SRC, body=(FIX / "event_page.html").read_text(), content_type="text/html")
    llm = FakeLLM({SRC: PageFacts(official_ticket_url="https://www.etix.com/ticket/p/12345/nova-los-angeles")})
    ctx = make_ctx(llm)
    seed_seller(ctx, "The Novo", "Los Angeles", "AXS", updated="2025-01-01T00:00:00+00:00")  # stale
    [lead] = enrich(ctx, [rec()])
    assert lead.ticket_seller == "Etix" and "ticket_seller" not in lead.low_confidence
    row = ctx.db.one("SELECT * FROM venue_seller WHERE venue_norm='the novo'")
    assert row["seller"] == "Etix" and row["source_url"] == SRC and row["updated_at"] > "2026"


@responses.activate
def test_fresh_page_evidence_overrides_cached_seller_when_page_fetched_for_agency(make_ctx):
    responses.get(SRC, body=(FIX / "event_page.html").read_text(), content_type="text/html")
    llm = FakeLLM({SRC: PageFacts(official_ticket_url="https://www.etix.com/ticket/p/12345/nova-los-angeles")})
    ctx = make_ctx(llm)
    seed_seller(ctx, "The Novo", "Los Angeles", "AXS")  # fresh cache, but no agency -> page fetched anyway
    [lead] = enrich(ctx, [rec()])
    assert lead.ticket_seller == "Etix"
    assert ctx.db.one("SELECT seller FROM venue_seller")["seller"] == "Etix"


@responses.activate
def test_never_guess_blank_when_absent(make_ctx):
    url = "https://mamasgeeky.com/2026/10/nova-seattle/"
    responses.get(url, body=(FIX / "bare_page.html").read_text(), content_type="text/html")
    # LLM hallucinates names that are not on the page, and an aggregator link: all rejected.
    llm = FakeLLM({url: PageFacts(agency="HYBE", promoter="Live Nation", organizer="AEG",
                                  official_ticket_url="https://seatgeek.com/nova-tickets", ticket_seller="SeatGeek")})
    ctx = make_ctx(llm)
    [lead] = enrich(ctx, [rec(city="Seattle", state="WA", venue="", source_url=url)])
    assert (lead.ticket_seller, lead.agency, lead.promoter, lead.organizer) == ("", "", "", "")
    assert "agency" not in lead.field_sources
    assert ctx.db.one("SELECT COUNT(*) FROM artist_agency")[0] == 0
    assert ctx.db.one("SELECT COUNT(*) FROM venue_seller")[0] == 0


@responses.activate
def test_seed_agencies_are_never_assigned(make_ctx):
    url = "https://mamasgeeky.com/2026/10/nova-seattle/"
    responses.get(url, body=(FIX / "bare_page.html").read_text(), content_type="text/html")
    ctx = make_ctx(FakeLLM())  # LLM returns nothing
    [lead] = enrich(ctx, [rec(source_url=url)])
    assert lead.agency == "" and lead.ticket_seller == ""


@responses.activate
def test_aggregator_record_seller_ignored_and_not_fetched(make_ctx):
    ctx = make_ctx()
    seed_agency(ctx, "NOVA", "Starlight Entertainment")
    seed_seller(ctx, "The Novo", "Los Angeles", "AXS")
    [lead] = enrich(ctx, [rec(ticket_seller="SeatGeek", ticket_url="https://seatgeek.com/nova-tickets/1",
                              source="seatgeek_kpop", source_url="https://seatgeek.com/nova-tickets/1")])
    assert lead.ticket_seller == "AXS"
    assert len(responses.calls) == 0  # seatgeek pages are never fetched


@responses.activate
def test_aggregator_accepted_only_when_page_says_official(make_ctx):
    url = "https://mamasgeeky.com/2026/10/nova-sg/"
    responses.get(url, body='<a href="https://seatgeek.com/nova-tickets">Official tickets on SeatGeek</a>',
                  content_type="text/html")
    llm = FakeLLM({url: PageFacts(official_ticket_url="https://seatgeek.com/nova-tickets", seller_stated_official=True)})
    ctx = make_ctx(llm)
    [lead] = enrich(ctx, [rec(source_url=url)])
    assert lead.ticket_seller == "SeatGeek"


@responses.activate
def test_per_city_sellers_differ(make_ctx):
    responses.get(TOUR, body=(FIX / "tour_page.html").read_text(), content_type="text/html")
    llm = FakeLLM({
        (TOUR, "Los Angeles"): PageFacts(official_ticket_url="https://www.axs.com/events/111/nova"),
        (TOUR, "Chicago"): PageFacts(official_ticket_url="https://www.etix.com/ticket/p/222/nova-chicago"),
    })
    ctx = make_ctx(llm)
    la, chi = enrich(ctx, [rec(source_url=TOUR),
                           rec(source_url=TOUR, city="Chicago", state="IL", venue="Radius", event_date="2026-12-08")])
    assert (la.ticket_seller, chi.ticket_seller) == ("AXS", "Etix")
    assert la.city == "Los Angeles, CA" and chi.city == "Chicago, IL"
    assert len(responses.calls) == 1  # tour page fetched once per run (HTTP memo)
    rows = {r["city_norm"]: r["seller"] for r in ctx.db.query("SELECT city_norm, seller FROM venue_seller")}
    assert rows == {"los angeles": "AXS", "chicago": "Etix"}


@responses.activate
def test_unknown_domain_ticket_url_is_low_confidence(make_ctx):
    box = "https://tickets.smallclub.com/nova"
    responses.get(box, body="<p>NOVA tickets</p>", content_type="text/html")
    responses.get(SRC, body="<p>NOVA</p>", content_type="text/html")
    ctx = make_ctx()
    seed_agency(ctx, "NOVA", "Starlight Entertainment")
    [lead] = enrich(ctx, [rec(ticket_url=box)])
    assert lead.ticket_seller == "smallclub.com"
    assert "ticket_seller" in lead.low_confidence and lead.field_sources["ticket_seller"] == box
    assert ctx.db.one("SELECT COUNT(*) FROM venue_seller")[0] == 0  # low-confidence fills are not cached


@responses.activate
def test_fetch_failure_and_llm_failure_keep_row(make_ctx):
    responses.get(SRC, status=404)
    ctx = make_ctx()
    leads = enrich(ctx, [rec(), rec(city="Chicago", event_date="2026-12-08")])
    assert len(leads) == 2 and all(lead.ticket_seller == "" for lead in leads)
    assert all(SRC in lead.sources for lead in leads)


@responses.activate
def test_provenance_recorded_for_every_filled_field(make_ctx):
    responses.get(SRC, body=(FIX / "event_page.html").read_text(), content_type="text/html")
    llm = FakeLLM({SRC: PageFacts(official_ticket_url="https://www.etix.com/ticket/p/12345/nova-los-angeles",
                                  agency="Starlight Entertainment", promoter="Bright Stage Productions",
                                  on_sale_date="2026-10-24")})
    ctx = make_ctx(llm)
    [lead] = enrich(ctx, [rec(on_sale_date=None)])
    for f in ("event", "artist_or_headliner", "venue", "event_date", "on_sale_date",
              "ticket_seller", "agency", "promoter"):
        assert getattr(lead, f), f
        assert lead.field_sources.get(f), f
    assert lead.on_sale_date == "2026-10-24"
    assert lead.sources == SRC
