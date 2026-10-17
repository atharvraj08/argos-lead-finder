"""Enrichment: EventRecord -> Lead (ticket seller, agency, promoter, organizer, dates).

Per event (one city/date; a tour can use different sellers per city):

  ticket_seller: record.ticket_seller -> seller_from_url(ticket_url) -> venue_seller lookup
                 -> the event's own pages (ticket_url, source_url) + LLM extraction.
                 Evidence from this event (record, ticket URL, its pages) overrides the cached
                 lookup and is written back to venue_seller (with source_url, updated_at).
  agency:        record (with URL) -> artist_agency lookup -> event pages (LLM) -> blank.
                 A page statement overrides the lookup and updates artist_agency.
                 config seed_agencies are never used as an assignment.
  promoter/organizer/venue/event_date/on_sale_date: record, then event pages.

Never guess: agency/promoter/organizer are filled only from a page or a lookup row with a
source URL, and every page-extracted name must literally appear in that page's text. Each
filled field records its URL in lead.field_sources; uncertain fills go in lead.low_confidence.
No general web search, no new sources: only the event's own source_url and ticket_url are
fetched (robots-respecting via ctx.http), and never on ticketing sites that bar scraping.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from ..db import utcnow
from ..http import RobotsDisallowed
from ..models import EventRecord, Lead, norm
from . import sellers
from .sellers import (canonical_seller_name, is_aggregator_name, is_aggregator_url,
                      is_known_seller_url, may_fetch, seller_from_url, seller_homepage)

__all__ = ["enrich", "enrich_one", "seller_from_url", "seller_homepage", "PageFacts", "sellers"]

log = logging.getLogger(__name__)

MAX_LINKS = 300
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PageFacts(BaseModel):
    """Fields the LLM may pull from one of the event's own pages."""

    official_ticket_url: str = Field("", description="The exact URL (copied from the page's links) where tickets "
                                     "for this specific show/date are bought. Empty if not given.")
    ticket_seller: str = Field("", description="Ticketing company named on the page as selling tickets for this "
                               "show (e.g. AXS, Etix, DICE, venue box office). Empty if not stated.")
    seller_stated_official: bool = Field(False, description="True only if the page explicitly calls this seller "
                                         "the official / exclusive / primary ticket outlet.")
    agency: str = Field("", description="The artist's entertainment agency, management company or label, only "
                        "if the page states it. Empty otherwise.")
    promoter: str = Field("", description="Company stated as promoting / presenting this show. Empty otherwise.")
    organizer: str = Field("", description="Company stated as organizing / producing / hosting the event. "
                           "Empty otherwise.")
    venue: str = ""
    event_date: str = Field("", description="YYYY-MM-DD for this city's show.")
    on_sale_date: str = Field("", description="YYYY-MM-DD general public on-sale date for this show.")


@dataclass
class _Page:
    url: str
    text: str  # plain text sent to the LLM, including a link list
    facts: PageFacts | None = None


@dataclass
class _Pick:
    value: str = ""
    url: str = ""
    low: bool = False
    fresh: bool = False  # from this event's own evidence (not the cached lookup)


@dataclass
class _State:
    pages: list[_Page] = field(default_factory=list)
    fetched: bool = False


# --------------------------------------------------------------------------- page text
def html_to_text(html: str, base_url: str = "") -> str:
    """Visible text + JSON-LD + a list of links ("anchor -> absolute URL") for the LLM."""
    if "<" not in html or ">" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    jsonld = [s.get_text() for s in soup.find_all("script", type="application/ld+json")]
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip()) if base_url else a["href"].strip()
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        anchor = " ".join(a.get_text(" ").split())[:80]
        ticketish = "ticket" in anchor.lower() or is_known_seller_url(href) or is_aggregator_url(href)
        links.append((0 if ticketish else 1, f"- {anchor or '(no text)'} -> {href}"))
    links.sort(key=lambda x: x[0])
    text = "\n".join(line for line in (ln.strip() for ln in soup.get_text("\n").splitlines()) if line)
    out = [text]
    if jsonld:
        out.append("\nStructured data (JSON-LD):\n" + "\n".join(j.strip() for j in jsonld)[:20000])
    if links:
        out.append("\nLinks on page:\n" + "\n".join(line for _, line in links[:MAX_LINKS]))
    return "\n".join(out)


def _in_text(value: str, text: str) -> bool:
    """Anti-hallucination check: the extracted name must literally appear in the page text."""
    v = norm(value)
    return bool(v) and v in norm(text)


def _url_in_text(url: str, text: str) -> bool:
    u = url.strip().rstrip("/")
    return bool(u) and (u in text or u.replace("https://", "http://") in text
                        or u.replace("http://", "https://") in text)


# --------------------------------------------------------------------------- lookups
def _stale_cutoff(ctx) -> str:
    days = ctx.config.targets.caching.lookup_stale_days
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _lookup_seller(ctx, venue: str, city: str) -> _Pick:
    if not venue:
        return _Pick()
    row = ctx.db.one("SELECT seller, source_url, updated_at FROM venue_seller WHERE venue_norm = ? AND city_norm = ?",
                     (norm(venue), norm(city)))
    if not row or not row["seller"] or not row["source_url"]:
        return _Pick()
    stale = (row["updated_at"] or "") < _stale_cutoff(ctx)
    return _Pick(row["seller"], row["source_url"], low=stale)


def _lookup_agency(ctx, artist: str) -> _Pick:
    if not artist:
        return _Pick()
    row = ctx.db.one("SELECT agency, source_url, updated_at FROM artist_agency WHERE artist_norm = ?", (norm(artist),))
    if not row or not row["agency"] or not row["source_url"]:
        return _Pick()
    stale = (row["updated_at"] or "") < _stale_cutoff(ctx)
    return _Pick(row["agency"], row["source_url"], low=stale)


def _save_seller(ctx, venue: str, city: str, seller: str, url: str) -> None:
    if not (venue and seller and url):
        return
    row = ctx.db.one("SELECT seller, source_url, updated_at FROM venue_seller WHERE venue_norm = ? AND city_norm = ?",
                     (norm(venue), norm(city)))
    if row and row["seller"] == seller and row["source_url"] and (row["updated_at"] or "") >= _stale_cutoff(ctx) \
            and row["source_url"] == url:
        return
    ctx.db.execute(
        "INSERT OR REPLACE INTO venue_seller (venue_norm, city_norm, venue, city, seller, source_url, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)", (norm(venue), norm(city), venue, city, seller, url, utcnow()))


def _save_agency(ctx, artist: str, agency: str, url: str) -> None:
    if not (artist and agency and url):
        return
    ctx.db.execute("INSERT OR REPLACE INTO artist_agency (artist_norm, artist, agency, source_url, updated_at)"
                   " VALUES (?, ?, ?, ?, ?)", (norm(artist), artist, agency, url, utcnow()))


# --------------------------------------------------------------------------- pages
def _fetch_text(ctx, url: str) -> str:
    try:
        resp = ctx.http.get(url)
    except RobotsDisallowed:
        log.info("robots disallows %s", url, extra={"url": url})
        return ""
    except (requests.RequestException, OSError) as e:
        log.warning("enrich fetch failed %s: %s", url, e, extra={"url": url})
        return ""
    return html_to_text(resp.text, resp.url or url)


def _instructions(rec: EventRecord) -> str:
    where = ", ".join(x for x in (rec.venue, rec.city, rec.state) if x) or "unknown city"
    return (
        f"Extract details for ONE show: artist/event \"{rec.artist or rec.event}\" ({rec.event}) at {where}"
        f" on {rec.event_date or 'date unknown'}. The page may list a whole tour; only report values that apply to "
        "this city/date. official_ticket_url must be copied exactly from the page's links. Report the agency, "
        "promoter or organizer only if the page names it in that role; do not infer from the artist's fame. "
        "Leave anything not stated empty."
    )


def _load_pages(ctx, rec: EventRecord, st: _State) -> None:
    """Fetch the event's own pages once (ticket page first: more specific)."""
    if st.fetched:
        return
    st.fetched = True
    urls: list[str] = []
    if rec.ticket_url and may_fetch(rec.ticket_url) and not sellers.is_non_seller_url(rec.ticket_url):
        urls.append(rec.ticket_url)
    if rec.source_url and rec.source_url not in urls and may_fetch(rec.source_url):
        urls.append(rec.source_url)
    for url in urls:
        text = _fetch_text(ctx, url)
        if not text and url == rec.source_url and rec.raw_text:
            text = html_to_text(rec.raw_text, url)  # text discovery already fetched from this page
        if text.strip():
            st.pages.append(_Page(url, text))
    if rec.raw_text and rec.source_url and rec.source_url not in urls:
        # Source page we may not refetch (e.g. ticketing site): use the text discovery kept.
        st.pages.append(_Page(rec.source_url, html_to_text(rec.raw_text, rec.source_url)))


def _extract(ctx, rec: EventRecord, page: _Page) -> PageFacts:
    if page.facts is None:
        try:
            facts = ctx.llm.extract(PageFacts, _instructions(rec), page.text, url=page.url)
        except Exception as e:  # LLM outage must not kill the run
            log.warning("enrich llm extract failed %s: %s", page.url, e, extra={"url": page.url})
            facts = None
        page.facts = facts or PageFacts()
    return page.facts


def _seller_from_facts(f: PageFacts, page: _Page) -> _Pick:
    """Seller stated by a page. Aggregators only when the page says they are official."""
    url = (f.official_ticket_url or "").strip()
    if url and _url_in_text(url, page.text):
        if is_aggregator_url(url):
            if f.seller_stated_official:
                return _Pick(sellers.aggregator_name(url), page.url, fresh=True)
        elif is_known_seller_url(url):
            return _Pick(seller_from_url(url), page.url, fresh=True)
        elif not sellers.is_non_seller_url(url):
            name = canonical_seller_name(f.ticket_seller) if _in_text(f.ticket_seller, page.text) else ""
            if name and not is_aggregator_name(name):
                return _Pick(name, page.url, fresh=True)
            dom = seller_from_url(url)
            if dom:
                return _Pick(dom, page.url, low=True, fresh=True)  # domain only: likely a box office
    if f.ticket_seller and _in_text(f.ticket_seller, page.text):
        name = canonical_seller_name(f.ticket_seller)
        if is_aggregator_name(name) and not f.seller_stated_official:
            return _Pick()
        return _Pick(name, page.url, fresh=True)
    return _Pick()


def _role_from_facts(value: str, page: _Page) -> str:
    value = (value or "").strip()
    return value if value and _in_text(value, page.text) else ""


# --------------------------------------------------------------------------- main
def _city_label(rec: EventRecord) -> str:
    city, st = rec.city.strip(), rec.state.strip()
    if city and st and not norm(city).endswith(norm(st)):
        return f"{city}, {st}"
    return city or st


def enrich_one(ctx, rec: EventRecord) -> Lead:
    lead = Lead(
        event_key=rec.key(), week_found=ctx.week, segment=rec.segment, event=rec.event,
        artist_or_headliner=rec.artist, city=_city_label(rec), venue=rec.venue,
        event_date=rec.event_date or "", on_sale_date=rec.on_sale_date or "", ticket_url=rec.ticket_url,
    )
    rec_url = lambda f: rec.field_sources.get(f) or rec.source_url  # noqa: E731

    def put(fld: str, pick: _Pick) -> None:
        setattr(lead, fld, pick.value)
        if pick.url:
            lead.field_sources[fld] = pick.url
        if pick.low and fld not in lead.low_confidence:
            lead.low_confidence.append(fld)
        elif not pick.low and fld in lead.low_confidence:
            lead.low_confidence.remove(fld)

    for fld, val in (("event", rec.event), ("artist_or_headliner", rec.artist), ("city", rec.city),
                     ("venue", rec.venue), ("event_date", rec.event_date), ("on_sale_date", rec.on_sale_date)):
        if val and rec_url(fld):
            lead.field_sources[fld] = rec_url(fld)
    # Intro roles from the record only with a URL (never guessed).
    for fld in ("promoter", "organizer"):
        val = getattr(rec, fld).strip()
        if val and rec_url(fld):
            put(fld, _Pick(val, rec_url(fld), fresh=True))

    # ---- ticket seller, fresh evidence first ----------------------------------------------
    seller = _Pick()
    if rec.ticket_seller.strip():
        name = canonical_seller_name(rec.ticket_seller)
        if is_aggregator_name(name):
            log.info("ignoring aggregator %s as seller for %s", name, rec.key(), extra={"event_key": rec.key()})
        else:
            url = rec.field_sources.get("ticket_seller") or rec.ticket_url or rec.source_url
            seller = _Pick(name, url, low=not url, fresh=True)
    tentative = _Pick()  # unknown-domain ticket URL: probably a box office; weakest evidence
    if not seller.value and rec.ticket_url:
        mapped = seller_from_url(rec.ticket_url)
        if mapped and is_known_seller_url(rec.ticket_url):
            seller = _Pick(mapped, rec.ticket_url, fresh=True)
        elif mapped:
            tentative = _Pick(mapped, rec.ticket_url, low=True, fresh=True)
    if not seller.value:
        seller = _lookup_seller(ctx, rec.venue, rec.city)

    # ---- agency ------------------------------------------------------------------------
    agency = _Pick()
    if rec.agency.strip() and rec_url("agency"):
        agency = _Pick(rec.agency.strip(), rec_url("agency"), fresh=True)
    else:
        agency = _lookup_agency(ctx, rec.artist)

    # ---- event pages (only when something is still missing or only cached) ---------------
    need_seller = not seller.fresh and (not seller.value or seller.low)
    need = need_seller or not agency.value or agency.low or not lead.venue or not lead.event_date \
        or not lead.on_sale_date
    st = _State()
    if need:
        _load_pages(ctx, rec, st)
        page_seller, page_agency = _Pick(), _Pick()
        for page in st.pages:
            f = _extract(ctx, rec, page)
            cand = _seller_from_facts(f, page)
            if cand.value and (not page_seller.value or (page_seller.low and not cand.low)):
                page_seller = cand
            if not page_agency.value:
                a = _role_from_facts(f.agency, page)
                if a:
                    page_agency = _Pick(a, page.url, fresh=True)
            for fld in ("promoter", "organizer", "venue"):
                v = _role_from_facts(getattr(f, fld), page)
                if v and not getattr(lead, fld):
                    put(fld, _Pick(v, page.url))
            for fld in ("event_date", "on_sale_date"):
                v = (getattr(f, fld) or "").strip()
                if v and _DATE_RE.match(v) and not getattr(lead, fld):
                    put(fld, _Pick(v, page.url))
        # Fresh page evidence beats the cached lookup and a bare unknown domain.
        if page_seller.value and not seller.fresh:
            if not page_seller.low or not seller.value or seller.low:
                seller = page_seller
        if page_agency.value and not agency.fresh:
            agency = page_agency

    if not seller.value and lead.venue != rec.venue:  # venue learned from a page: try the lookup now
        seller = _lookup_seller(ctx, lead.venue, rec.city)
    if not seller.value and tentative.value:
        seller = tentative
    if seller.value:
        put("ticket_seller", seller)
        if seller.fresh and not seller.low:
            _save_seller(ctx, lead.venue, rec.city, seller.value, seller.url)
    if agency.value:
        put("agency", agency)
        if agency.fresh and not agency.low:
            _save_agency(ctx, rec.artist, agency.value, agency.url)

    urls: list[str] = []
    # discovery stores every reporting source's URL, space-separated, under field_sources["sources"]
    for u in [rec.source_url, *rec.field_sources.get("sources", "").split(), *lead.field_sources.values()]:
        if u and u not in urls:
            urls.append(u)
    lead.sources = " ".join(urls)
    if not lead.ticket_seller:
        log.info("no ticket seller found", extra={"event_key": lead.event_key})
    return lead


def enrich(ctx, events: list[EventRecord]) -> list[Lead]:
    """One Lead per EventRecord, in order. Rows with no seller are kept (seller blank)."""
    leads: list[Lead] = []
    for rec in events:
        try:
            leads.append(enrich_one(ctx, rec))
        except Exception:  # one bad record must not drop the rest
            log.exception("enrich failed", extra={"event_key": rec.key()})
            leads.append(Lead(event_key=rec.key(), week_found=ctx.week, segment=rec.segment, event=rec.event,
                              artist_or_headliner=rec.artist, city=_city_label(rec), venue=rec.venue,
                              event_date=rec.event_date or "", on_sale_date=rec.on_sale_date or "",
                              ticket_url=rec.ticket_url,
                              sources=rec.field_sources.get("sources") or rec.source_url))
    return leads
