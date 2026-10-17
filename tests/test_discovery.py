"""Discovery tests: adapters parse realistic fixtures, filters, U.S.-only, dedupe across
sources and past runs, collect(daily_only). No network, no real LLM."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import responses

from argos_leads.config import Config, EmailConfig, TargetsConfig, load_config
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.discovery import collect, discover
from argos_leads.discovery.extract import ExtractedDate, ExtractedEvent, ExtractedEvents
from argos_leads.discovery.sources import ItemFailed, get_adapter
from argos_leads.discovery.sources.tokyonoizu import artist_from_headline, parse_row
from argos_leads.discovery.util import is_in_region, term_pattern
from argos_leads.http import HttpClient
from argos_leads.models import RawItem

FIX = Path(__file__).parent / "fixtures" / "discovery"
TODAY = date(2026, 10, 17)

WP_URL = "https://kpopofficial.com/wp-json/wp/v2/event"
TN_URL = "https://tokyonoizu.com/japanese-band-tours"
KC_URL = "https://kpopconcerts.com/feed/"
MG_URL = "https://mamasgeeky.com/category/jpop/feed/"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
SG_URL = "https://api.seatgeek.com/2/events"


class FakeLLM:
    """Stand-in for argos_leads.llm.LLM: returns canned extractions per URL."""

    def __init__(self, by_url: dict[str, ExtractedEvents] | None = None, fail: bool = False):
        self.by_url = by_url or {}
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def extract(self, output, instructions, page_text, url=""):
        self.calls.append((url, page_text))
        if self.fail:
            return None
        return self.by_url.get(url, output())


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def make_ctx(sources: dict[str, list[dict]] | None = None, llm: FakeLLM | None = None, **kw) -> RunContext:
    if sources is None:
        cfg = load_config()
    else:
        targets = TargetsConfig(
            active_segments=list(sources), region="US",
            segments={seg: {"sources": srcs, "artist_watchlist": ["BAND-MAID", "Ado", "XG"]}
                      for seg, srcs in sources.items()},
        )
        cfg = Config(targets=targets, email=EmailConfig(recipients=["x@example.com"]))
    db = Database(":memory:")
    http = HttpClient(db=db, check_robots=kw.pop("check_robots", False), default_min_interval_s=0, max_retries=0)
    return RunContext(config=cfg, db=db, http=http, llm=llm or FakeLLM(), today=TODAY, run_id=1, **kw)


def src(ctx: RunContext, segment: str, slug: str):
    seg = ctx.config.targets.segments[segment]
    scfg = next(s for s in seg.sources if s.slug == slug)
    return get_adapter(scfg.adapter)(scfg, segment, seg)


def source_log(ctx: RunContext) -> dict[str, str]:
    return {r["source"]: r["status"] for r in ctx.db.query("SELECT source, status FROM source_log")}


WP_SRC = {"slug": "kpopofficial_events", "adapter": "wp_events", "url": WP_URL}
TN_SRC = {"slug": "tokyonoizu_tours", "adapter": "tokyonoizu", "url": TN_URL}
SG_SRC = {"slug": "seatgeek_kpop", "adapter": "seatgeek", "queries": ["kpop"], "genre_slug": "k-pop"}
KC_SRC = {"slug": "kpopconcerts_rss", "adapter": "rss", "url": KC_URL,
          "include_terms": ["[TOUR]", "tour", "U.S.", "North America", "concert", "fan meeting", "showcase"]}
GD_SRC = {"slug": "gdelt_news_kpop", "adapter": "gdelt", "query": '(kpop OR "k-pop") tour sourcecountry:US'}


# --- utilities ----------------------------------------------------------------
def test_term_pattern_word_boundaries():
    p = term_pattern(["Ado", "U.S.", "[TOUR]", "fan meeting"])
    assert p.search("Ado announces U.S. dates")
    assert not p.search("Adobe launches new app")
    assert p.search("[TOUR] IVE comes to Newark")
    assert p.search("A FAN MEETING in LA")
    assert term_pattern([]) is None


def test_is_in_region():
    assert is_in_region("United States", "", "US")
    assert is_in_region("USA", "", "US")
    assert is_in_region("", "California", "US")
    assert is_in_region("", "TX", "US")
    assert not is_in_region("Canada", "ON", "US")
    assert not is_in_region("", "", "US")
    assert not is_in_region("", "BC", "US")


# --- wp_events ------------------------------------------------------------------
@responses.activate
def test_wp_events_collect_and_parse():
    responses.add(responses.GET, WP_URL, body=fixture("wp_events.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [WP_SRC]})
    s = src(ctx, "kpop", "kpopofficial_events")
    items = s.collect(ctx)
    assert len(items) == 3
    recs = s.parse(ctx, items)
    assert {r.city for r in recs} == {"Los Angeles", "Fort Worth"}  # Vancouver dropped (not U.S.)
    la = next(r for r in recs if r.city == "Los Angeles")
    assert la.artist == "tripleS"
    assert la.state == "CA" and la.venue == "The Wiltern"
    assert la.event_date == "2026-11-24"
    assert la.on_sale_date == "2026-09-28"
    assert la.ticket_url == "https://www.ticketmaster.com/event/09006523EB64E79F"
    assert la.ticket_seller == ""  # seller is left to enrichment
    assert la.source_url == "https://kpopofficial.com/event/triples-world-tour-2026-los-angeles/"
    assert la.field_sources["ticket_url"] == la.source_url
    assert set(la.field_sources) >= {"artist", "city", "venue", "event_date", "on_sale_date"}


# --- tokyonoizu -----------------------------------------------------------------
def test_tokyonoizu_row_parsing():
    assert parse_row("12 October US, Los Angeles, CA, The Wiltern")["state"] == "CA"
    r = parse_row("-10 November USA, POMONA, The Glasshouse")
    assert (r["city"], r["venue"], r["country"]) == ("Pomona", "The Glasshouse", "USA")
    r = parse_row("15 October San Francisco, CA, August Hall")
    assert (r["country"], r["city"], r["state"], r["venue"]) == ("US", "San Francisco", "CA", "August Hall")
    r = parse_row("-12 November Netherlands, UTRECHT, De Helling, SOLD OUT")
    assert r["venue"] == "De Helling"
    assert parse_row("Status: Confirmed") is None
    assert artist_from_headline("BAND-MAID NORTH AMERICA 2026", ["BAND-MAID"]) == "BAND-MAID"
    assert artist_from_headline('BURNOUT SYNDROMES - "Fly Me To The USA" Tour 2026', []) == "BURNOUT SYNDROMES"
    assert artist_from_headline("D'ESPAIRSRAY WORLD TOUR 2027", []) == "D'ESPAIRSRAY"


@responses.activate
def test_tokyonoizu_collect_parse_and_skip_unchanged():
    responses.add(responses.GET, TN_URL, body=fixture("tokyonoizu.html"))
    ctx = make_ctx({"jpop": [TN_SRC]})
    counts = collect(ctx)
    assert counts["tokyonoizu_tours"] > 5
    new = discover(ctx, "jpop")
    keys = {r.key() for r in new}
    assert "burnout syndromes|pomona|2026-11-10" in keys
    assert "sim|los angeles|2026-11-22" in keys
    assert not any(r.artist == "BABYBEARD" for r in new)  # Europe only
    assert not any(r.event_date < "2026-10-17" for r in new)  # past dates dropped (BAND-MAID Oct)
    sim = next(r for r in new if r.key() == "sim|los angeles|2026-11-22")
    assert sim.venue == "The Belasco" and sim.ticket_url == "https://tix.soundrink.com/tours/sim"
    assert sim.source_url == TN_URL

    # Same "last updated" string next run: nothing collected, nothing parsed.
    ctx.http._memo.clear()
    assert collect(ctx)["tokyonoizu_tours"] == 0
    assert discover(ctx, "jpop") == []


# --- rss --------------------------------------------------------------------------
@responses.activate
def test_rss_filters_then_extracts_us_only():
    responses.add(responses.GET, KC_URL, body=fixture("kpopconcerts.xml"))
    gallery = "https://kpopconcerts.com/gallery/2026-jay-park-world-tour-with-lngshot-in-cologne/"
    llm = FakeLLM({gallery: ExtractedEvents(events=[ExtractedEvent(
        artist="Jay Park", event_name="Serenades & Body Rolls World Tour", on_sale_date="2026-10-20",
        promoter="Example Live", dates=[
            ExtractedDate(city="Cologne", country="Germany", venue="Palladium", date="2026-10-09"),
            ExtractedDate(city="Newark", state="NJ", venue="Prudential Center", date="2026-11-14"),
            ExtractedDate(city="Atlanta", country="United States", venue="Coca-Cola Roxy", date="2026-11-18"),
            ExtractedDate(city="Toronto", state="ON", venue="History", date="2026-11-20"),
        ])])})
    ctx = make_ctx({"kpop": [KC_SRC]}, llm=llm)
    s = src(ctx, "kpop", "kpopconcerts_rss")
    items = s.collect(ctx)
    assert len(items) == 4
    by_title = {i.title: i for i in items}
    assert not s.wanted(by_title["WAYF BOYS Release New Single ‘A4M’"])
    assert s.wanted(next(i for i in items if "Jay Park" in i.title))

    item = next(i for i in items if "Jay Park" in i.title)
    item.url = gallery
    recs = s.parse(ctx, items)
    # Only items passing the term filter reach the LLM.
    called = {u for u, _ in llm.calls}
    assert gallery in called
    assert not any("wayf" in u for u in called)
    assert {(r.city, r.state) for r in recs} == {("Newark", "NJ"), ("Atlanta", "")}
    nwk = next(r for r in recs if r.city == "Newark")
    assert nwk.artist == "Jay Park" and nwk.on_sale_date == "2026-10-20" and nwk.promoter == "Example Live"
    assert nwk.field_sources["promoter"] == gallery and nwk.source_url == gallery
    assert nwk.ticket_seller == ""


def test_rss_watchlist_filter():
    ctx = make_ctx({"jpop": [{"slug": "pollstar_rss_jpop", "adapter": "rss", "url": "https://news.pollstar.com/feed/",
                              "include_terms": ["J-pop"], "include_watchlist": True}]})
    s = src(ctx, "jpop", "pollstar_rss_jpop")
    mk = lambda t: RawItem(source=s.slug, item_id=t, url="u", title=t)
    assert s.wanted(mk("BAND-MAID Announce 2027 Arena Run"))
    assert s.wanted(mk("J-pop is booming on U.S. stages"))
    assert not s.wanted(mk("Adobe hosts summit at Moscone"))


@responses.activate
def test_rss_article_fetch_only_when_enabled():
    responses.add(responses.GET, MG_URL, body=fixture("mamasgeeky.xml"))
    article = "https://mamasgeeky.com/2026/08/kpex-2027-details.html"
    responses.add(responses.GET, article, body="<html><body><article><p>KPEX will be held at the Javits Center, "
                                               "New York, NY on December 5, 2026.</p></article></body></html>")
    base = {"slug": "mamasgeeky_jpop_rss", "adapter": "rss", "url": MG_URL, "include_terms": ["expo"]}
    for fetch in (False, True):
        llm = FakeLLM()
        ctx = make_ctx({"jpop": [dict(base, fetch_article=fetch)]}, llm=llm)
        s = src(ctx, "jpop", "mamasgeeky_jpop_rss")
        s.parse(ctx, s.collect(ctx))
        texts = [t for u, t in llm.calls if u == article]
        assert len(texts) == 1
        assert ("Javits Center" in texts[0]) is fetch


# --- gdelt ------------------------------------------------------------------------
@responses.activate
def test_gdelt_collect_title_filter_and_extract():
    responses.add(responses.GET, GDELT_URL, body=fixture("gdelt.json"), content_type="application/json")
    cnbc = "https://www.cnbc.com/2026/10/16/bts-concerts-hybe-katseye-cortis.html"
    llm = FakeLLM({cnbc: ExtractedEvents(events=[ExtractedEvent(artist="BTS", event_name="BTS World Tour", dates=[
        ExtractedDate(city="Las Vegas", state="NV", venue="Allegiant Stadium", date="2027-04-10"),
        ExtractedDate(city="Bogotá", country="Colombia", venue="Estadio El Campín", date="2026-10-30"),
    ])])})
    ctx = make_ctx({"kpop": [GD_SRC]}, llm=llm)
    s = src(ctx, "kpop", "gdelt_news_kpop")
    items = s.collect(ctx)
    assert len(items) == 5 and items[0].published.startswith("2026-10-16")
    recs = s.parse(ctx, items)
    called = {u for u, _ in llm.calls}
    # "Willmar Area Symphonic Orchestra to kick off 69th season" has no event term -> no LLM call.
    assert not any("wctrib" in u for u in called)
    assert cnbc in called
    assert [(r.artist, r.city, r.state, r.event_date) for r in recs] == [("BTS", "Las Vegas", "NV", "2027-04-10")]


@responses.activate
def test_gdelt_non_json_is_logged_and_other_sources_continue():
    responses.add(responses.GET, GDELT_URL, body="Please limit requests to one every 5 seconds")
    responses.add(responses.GET, WP_URL, body=fixture("wp_events.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [GD_SRC, WP_SRC]})
    counts = collect(ctx)
    assert counts == {"gdelt_news_kpop": 0, "kpopofficial_events": 3}
    assert source_log(ctx) == {"gdelt_news_kpop": "error", "kpopofficial_events": "ok"}


# --- seatgeek ---------------------------------------------------------------------
def test_seatgeek_skipped_without_client_id(monkeypatch):
    monkeypatch.delenv("SEATGEEK_CLIENT_ID", raising=False)
    ctx = make_ctx({"kpop": [SG_SRC]})
    assert collect(ctx) == {"seatgeek_kpop": 0}
    assert source_log(ctx) == {"seatgeek_kpop": "skipped"}


@responses.activate
def test_seatgeek_parse(monkeypatch):
    monkeypatch.setenv("SEATGEEK_CLIENT_ID", "test-id")
    responses.add(responses.GET, SG_URL, body=fixture("seatgeek.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [SG_SRC]})
    s = src(ctx, "kpop", "seatgeek_kpop")
    items = s.collect(ctx)
    assert sorted(i.item_id for i in items) == ["7001001", "7001004"]  # genre filter drops unrelated hits
    assert "client_id=test-id" in responses.calls[0].request.url
    assert ctx.db.one("SELECT COUNT(*) AS n FROM http_cache")["n"] == 0  # client_id never cached
    recs = s.parse(ctx, items)
    assert len(recs) == 1  # Toronto dropped
    r = recs[0]
    assert (r.artist, r.city, r.state, r.venue, r.event_date) == ("tripleS", "Los Angeles", "CA", "The Wiltern", "2026-11-24")
    assert r.ticket_seller == "" and r.ticket_url == ""  # resale marketplace: seller left to enrichment
    assert r.source_url.startswith("https://seatgeek.com/")


# --- discover: dedupe & merge -------------------------------------------------------
@responses.activate
def test_dedupe_across_sources_and_past_runs(monkeypatch):
    monkeypatch.setenv("SEATGEEK_CLIENT_ID", "test-id")
    responses.add(responses.GET, SG_URL, body=fixture("seatgeek.json"), content_type="application/json")
    responses.add(responses.GET, WP_URL, body=fixture("wp_events.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [SG_SRC, WP_SRC]})
    assert collect(ctx) == {"seatgeek_kpop": 2, "kpopofficial_events": 3}
    new = discover(ctx, "kpop")
    keys = sorted(r.key() for r in new)
    assert keys == ["triples|fort worth|2026-11-21", "triples|los angeles|2026-11-24"]
    la = next(r for r in new if r.city == "Los Angeles")
    # Both structured; SeatGeek listed first -> its values kept, gaps filled from kpopofficial.
    assert la.on_sale_date == "2026-09-28"
    assert la.field_sources["on_sale_date"].startswith("https://kpopofficial.com/")
    assert la.ticket_url.startswith("https://www.ticketmaster.com/")
    srcs = la.field_sources["sources"].split()
    assert any("seatgeek.com" in u for u in srcs) and any("kpopofficial.com" in u for u in srcs)
    row = ctx.db.one("SELECT * FROM events WHERE event_key = ?", ("triples|los angeles|2026-11-24",))
    assert row["first_seen_week"] == "2026-W42" and row["segment"] == "kpop"
    assert json.loads(row["sources_json"]) == ["kpopofficial_events", "seatgeek_kpop"]
    assert ctx.db.one("SELECT COUNT(*) AS n FROM raw_items WHERE processed_at IS NULL")["n"] == 0

    # Next week: the same show shows up again in a new item -> not new.
    ctx.db.execute("INSERT INTO raw_items (source, item_id, url, title, published, text, payload_json, collected_at)"
                   " SELECT source, item_id || '-v2', url, title, published, text, payload_json, collected_at"
                   " FROM raw_items WHERE source = 'kpopofficial_events'")
    assert discover(ctx, "kpop") == []


def test_merge_prefers_structured_over_news():
    from argos_leads.discovery import merge_records
    from argos_leads.discovery.util import make_record

    news = make_record("billboard_kpop_rss", "kpop", "https://news/a", event="X Tour", artist="X", city="Newark",
                       venue="Prudential Ctr", event_date="2026-11-14", promoter="Promo Co")
    struct = make_record("kpopofficial_events", "kpop", "https://kpo/x", event="X World Tour", artist="X",
                         city="Newark", venue="Prudential Center", event_date="2026-11-14")
    m = merge_records([news, struct], {"kpopofficial_events": True, "billboard_kpop_rss": False})
    assert m.venue == "Prudential Center" and m.source == "kpopofficial_events"
    assert m.promoter == "Promo Co" and m.field_sources["promoter"] == "https://news/a"
    assert m.field_sources["venue"] == "https://kpo/x"
    assert m.field_sources["sources"] == "https://kpo/x https://news/a"


@responses.activate
def test_llm_failure_leaves_item_for_retry():
    responses.add(responses.GET, KC_URL, body=fixture("kpopconcerts.xml"))
    ctx = make_ctx({"kpop": [KC_SRC]}, llm=FakeLLM(fail=True))
    collect(ctx)
    assert discover(ctx, "kpop") == []
    left = ctx.db.query("SELECT title FROM raw_items WHERE processed_at IS NULL")
    # Items rejected by the cheap filter are done; those that needed the LLM are retried.
    assert left and all("WAYF" not in r["title"] for r in left)
    with pytest.raises(ItemFailed):
        s = src(ctx, "kpop", "kpopconcerts_rss")
        s.parse_item(ctx, RawItem(source=s.slug, item_id="z", url="u", title="Big U.S. tour"))


@responses.activate
def test_collect_never_overwrites_processed_at():
    responses.add(responses.GET, WP_URL, body=fixture("wp_events.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [WP_SRC]})
    collect(ctx)
    discover(ctx, "kpop")
    before = {r["item_id"]: r["processed_at"] for r in ctx.db.query("SELECT * FROM raw_items")}
    ctx.http._memo.clear()
    assert collect(ctx) == {"kpopofficial_events": 0}
    after = {r["item_id"]: r["processed_at"] for r in ctx.db.query("SELECT * FROM raw_items")}
    assert before == after and all(after.values())


@responses.activate
def test_dry_run_does_not_store_events():
    responses.add(responses.GET, WP_URL, body=fixture("wp_events.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [WP_SRC]}, dry_run=True)
    collect(ctx)
    assert len(discover(ctx, "kpop")) == 2
    assert ctx.db.one("SELECT COUNT(*) AS n FROM events")["n"] == 0
    assert len(discover(ctx, "kpop")) == 2  # items still unprocessed


# --- collect(daily_only) with the real targets.yaml ---------------------------------
@responses.activate
def test_collect_daily_only_uses_daily_sources(monkeypatch):
    monkeypatch.delenv("SEATGEEK_CLIENT_ID", raising=False)
    responses.add(responses.GET, "https://news.pollstar.com/feed/", body=fixture("kpopconcerts.xml"))
    responses.add(responses.GET, "https://www.animenewsnetwork.com/news/rss.xml", body=fixture("mamasgeeky.xml"))
    ctx = make_ctx()  # real config/targets.yaml
    daily = {s.slug for seg in ctx.config.targets.active().values() for s in seg.sources if s.daily}
    counts = collect(ctx, daily_only=True)
    assert set(counts) == daily
    assert daily == {"pollstar_rss_kpop", "pollstar_rss_jpop", "ann_news_rss"}
    assert all(v == 4 for v in counts.values())
    # Pollstar is shared by both segments but fetched once per run.
    assert sum("pollstar" in c.request.url for c in responses.calls) == 1


@responses.activate
def test_robots_disallow_skips_source():
    responses.add(responses.GET, "https://kpopconcerts.com/robots.txt", body="User-agent: *\nDisallow: /\n")
    ctx = make_ctx({"kpop": [KC_SRC]}, check_robots=True)
    assert collect(ctx) == {"kpopconcerts_rss": 0}
    assert source_log(ctx) == {"kpopconcerts_rss": "skipped"}


# --- regressions from the live dry run (2026-10-17) ----------------------------------
@responses.activate
def test_wp_events_venue_variants_and_tba_tickets():
    """Bug 1: ROSÉ/LACMA venue landed in city. Bug 2: TXT 2027 rows have no venue/ticket/on-sale
    on the page (Buy Ticket = TBA) -> blanks, and the Website link must not become ticket_url.
    Also: etix.com links were skipped because 'x.com' matched as a substring."""
    responses.add(responses.GET, WP_URL, body=fixture("wp_events_variants.json"), content_type="application/json")
    ctx = make_ctx({"kpop": [WP_SRC]})
    s = src(ctx, "kpop", "kpopofficial_events")
    recs = {r.source_url.rstrip("/").split("/")[-1]: r for r in s.parse(ctx, s.collect(ctx))}

    rose = recs["rose-lacma-art-film-gala-2026"]
    assert (rose.venue, rose.city, rose.state) == ("Los Angeles County Museum of Art", "Los Angeles", "CA")
    assert rose.ticket_url == "https://www.lacma.org/ArtPlusFilmGala" and rose.event_date == "2026-11-07"

    txt = recs["txt-world-tour-2027-chicago"]
    assert (txt.city, txt.state, txt.venue) == ("Chicago", "IL", "")
    assert txt.ticket_url == "" and txt.on_sale_date is None
    assert "ibighit" not in txt.ticket_url
    belmont = recs["txt-world-tour-2027-belmont-park"]
    assert (belmont.city, belmont.state, belmont.venue) == ("Belmont Park", "NY", "")

    tri = recs["triples-world-tour-2026-chicago"]
    assert tri.venue == "Copernicus Center" and tri.ticket_url.startswith("https://www.etix.com/ticket/")
    assert tri.on_sale_date == "2026-09-28"

    assert "mtv-vmas-2026" not in recs  # lineup post without an Artist row -> skipped
    museum = recs["rose-grammy-museum-performance-2025"]  # "[ Google Maps ]" lines ignored
    assert (museum.venue, museum.city) == ("GRAMMY Museum", "Los Angeles")


def test_wp_split_venue_never_puts_venue_in_city():
    from argos_leads.discovery.sources.wp_events import city_hint, split_venue

    assert split_venue("Los Angeles County Museum of Art", "Los Angeles") == \
        {"venue": "Los Angeles County Museum of Art", "city": "", "state": "", "country": ""}
    assert split_venue(["Great Lawn, Central Park", "New York, United States"], "New York City")["venue"] == \
        "Great Lawn, Central Park"
    assert split_venue("Chicago, Illinois, United States", "Chicago")["city"] == "Chicago"
    assert split_venue("Peacock Theater in Los Angeles, California", "Los Angeles") == \
        {"venue": "Peacock Theater", "city": "Los Angeles", "state": "California", "country": ""}
    assert city_hint("LACMA ART+FILM GALA 2026", ["event-city-los-angeles-california-united-states"]) == "Los Angeles"


def test_tokyonoizu_state_only_when_given():
    """Bug 3: fill state when the page gives it, in any of its forms; never guess."""
    rows = {l: parse_row(l) for l in [
        "-16 November USA, BROOKLYN, Meadows",
        "-16 November USA, BROOKLYN NY, Meadows",
        "-16 November USA, Brooklyn, New York, Meadows",
        "12 October US, Los Angeles, CA, The Wiltern",
        "01 November Philadelphia, PA, Brooklyn Bowl | Philadelphia",
    ]}
    got = [(r["city"], r["state"], r["venue"]) for r in rows.values()]
    assert got == [("Brooklyn", "", "Meadows"), ("Brooklyn", "NY", "Meadows"),
                   ("Brooklyn", "New York", "Meadows"), ("Los Angeles", "CA", "The Wiltern"),
                   ("Philadelphia", "PA", "Brooklyn Bowl")]


@responses.activate
def test_tokyonoizu_artist_cleanup():
    """Bug 4: headline lead-in text leaked into the artist ("What is Paledusk??")."""
    assert artist_from_headline("What is Paledusk?? US TOUR 2026", [], "https://tix.soundrink.com/tours/paledusk",
                                ["About Tour : What is Paledusk?? US TOUR 2026 is with GUEST Slow Degrade"]) == "Paledusk"
    assert artist_from_headline("What is Paledusk?? US TOUR 2026", [], "", []) == "Paledusk"
    assert artist_from_headline("Boris PINK ”20th Anniversary EURO & UK Tour 2026″", [],
                                "https://borisheavyrocks.com/news/10208/", []) == "Boris"
    assert artist_from_headline("Acid Mothers Temple - “Survive to Metaverse Tour 2026 IN USA”", [],
                                "https://acidmothers.com/x", []) == "Acid Mothers Temple"
    # Watchlist names win wherever they appear in the headline.
    assert artist_from_headline("Special guest night: BAND-MAID NORTH AMERICA 2026", ["BAND-MAID"]) == "BAND-MAID"

    responses.add(responses.GET, TN_URL, body=fixture("tokyonoizu.html"))
    ctx = make_ctx({"jpop": [TN_SRC]})
    collect(ctx)
    artists = {r.artist for r in discover(ctx, "jpop")}
    assert "Paledusk" in artists and not any("What is" in a for a in artists)
