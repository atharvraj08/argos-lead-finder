"""LLM extraction of event dates from news text (RSS and GDELT items).

The LLM is used only to pull fields out of text we fetched; nothing is guessed.
Article pages are fetched only when the source sets `fetch_article: true` in
config/targets.yaml (SOURCES.md ground rule: feed fields only unless enabled), and
only if robots.txt allows.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..http import RobotsDisallowed
from ..models import EventRecord, RawItem
from .sources.base import ItemFailed, Source
from .util import article_text, iso_date, is_in_region, make_record

if TYPE_CHECKING:
    from ..context import RunContext

log = logging.getLogger("argos_leads.discovery")

DEFAULT_EVENT_TERMS = [
    "tour", "concert", "concerts", "dates", "tickets", "on sale", "fan meeting", "fanmeeting", "fan-meeting",
    "fancon", "showcase", "live in", "arena", "stadium", "festival",
]
RAW_TEXT_LIMIT = 8_000


class ExtractedDate(BaseModel):
    city: str = Field("", description="City of this show, as stated")
    state: str = Field("", description="U.S. state or province as stated (e.g. 'CA' or 'California'); empty if not stated")
    country: str = Field("", description="Country of this show as stated or unambiguous from the text "
                                         "(e.g. 'City, ST' form, or listed under a 'U.S. tour' heading); empty if unclear")
    venue: str = Field("", description="Venue name as stated")
    date: str = Field("", description="Show date YYYY-MM-DD; empty if not stated")
    ticket_url: str = Field("", description="Ticket link for this specific show, if the text gives one")


class ExtractedEvent(BaseModel):
    artist: str = Field("", description="Headlining artist or group")
    event_name: str = Field("", description="Tour or event name")
    on_sale_date: str = Field("", description="General public on-sale date YYYY-MM-DD; empty if not stated")
    ticket_url: str = Field("", description="Main ticket link, if stated")
    ticket_seller: str = Field("", description="Ticketing company explicitly named as selling tickets (e.g. 'tickets via AXS'); empty if not stated")
    promoter: str = Field("", description="Promoter if explicitly named")
    organizer: str = Field("", description="Organizer / presenter if explicitly named")
    agency: str = Field("", description="Artist's agency or label if explicitly named")
    dates: list[ExtractedDate] = Field(default_factory=list, description="Every individual show date listed")


class ExtractedEvents(BaseModel):
    events: list[ExtractedEvent] = Field(default_factory=list)


INSTRUCTIONS = (
    "This is a music news item. List each upcoming concert, tour, fan meeting, showcase or "
    "festival appearance it announces, with every individual show date (city, state, country, "
    "venue, date). Include shows in any country; state the country only when the text makes it "
    "clear. Skip reviews of past shows, album releases and anything that is not a live event. "
    "If no live event dates are announced, return an empty list."
)


def news_text(ctx: "RunContext", src: Source, item: RawItem) -> tuple[str, str]:
    """Text to extract from, and the URL it came from. Uses the feed fields; fetches the
    article page only if the source enables `fetch_article` and robots.txt allows."""
    feed_text = "\n\n".join(t for t in (item.title, item.text) if t)
    if src.params.get("fetch_article") and item.url:
        try:
            resp = ctx.http.get(item.url)
            body = article_text(resp.text)
            if body:
                return f"{item.title}\n\n{body}", item.url
        except RobotsDisallowed:
            log.info("robots.txt disallows article; using feed fields", extra={"source": src.slug, "url": item.url})
        except Exception as e:  # network/HTTP errors: fall back to feed fields
            log.warning("article fetch failed (%s); using feed fields", e, extra={"source": src.slug, "url": item.url})
    return feed_text, item.url


def extract_records(ctx: "RunContext", src: Source, item: RawItem, text: str, url: str) -> list[EventRecord]:
    try:
        result = ctx.llm.extract(ExtractedEvents, INSTRUCTIONS, text, url=url)
    except Exception as e:  # missing API key, client errors
        raise ItemFailed(f"llm extract error: {e}") from e
    if result is None:
        raise ItemFailed("llm extract returned nothing")
    region = src.region(ctx)
    default_year = int((item.published or "")[:4]) if (item.published or "")[:4].isdigit() else ctx.today.year
    out: list[EventRecord] = []
    for ev in result.events:
        artist = (ev.artist or "").strip()
        if not artist:
            continue
        for d in ev.dates:
            if not is_in_region(d.country, d.state, region):
                continue
            if not (d.city or d.venue):
                continue
            out.append(make_record(
                src.slug, src.segment, url,
                event=(ev.event_name or "").strip() or artist,
                artist=artist,
                city=d.city, state=d.state, venue=d.venue,
                event_date=iso_date(d.date, default_year),
                on_sale_date=iso_date(ev.on_sale_date, default_year),
                ticket_url=(d.ticket_url or ev.ticket_url or "").strip(),
                ticket_seller=(ev.ticket_seller or "").strip(),
                promoter=(ev.promoter or "").strip(),
                organizer=(ev.organizer or "").strip(),
                agency=(ev.agency or "").strip(),
                source_event_id=item.item_id,
                raw_text=text[:RAW_TEXT_LIMIT],
            ))
    return out
