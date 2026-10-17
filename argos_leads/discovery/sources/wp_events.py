"""kpopofficial.com WordPress REST `event` posts (structured "Event Details" block).

Each post is one event date. content.rendered holds labelled rows (Artist, Event, Date,
Venue, Buy Ticket) and a "Ticket Sales Schedule" with the general on-sale date. The Buy
Ticket link becomes ticket_url; the seller itself is left to enrichment.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup, NavigableString

from ...models import EventRecord, RawItem, norm
from ..util import US_STATES, html_to_text, is_country, iso_date, make_record, state_code
from .base import Source, register

if TYPE_CHECKING:
    from ...context import RunContext

FIELDS = "id,date_gmt,modified_gmt,link,title,content,class_list"
LABELS = {"Artist", "Event", "Date", "Venue", "Buy Ticket", "Official Source"}
# Any of these ends a multi-line value (e.g. the Venue rows, the Buy Ticket link).
STOP_LABELS = LABELS | {"Host", "Hosts", "Artist Lineup", "Lineup", "Guest", "Guests", "Artist Hub",
                        "Ticket Sales Schedule", "Ticket Price", "Ticket Prices", "Website:"}
MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})"
    r"(?:\s*[–-]\s*(?:[A-Za-z]+\s+)?\d{1,2})?,\s*(\d{4})")
ONSALE_RE = re.compile(
    r"General[^:\n]*:\s*(\d{4}[./-]\d{1,2}[./-]\d{1,2}|(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
SKIP_LINK_HOSTS = ("kpopofficial.com", "x.com", "twitter.com", "instagram.com", "youtube.com", "tiktok.com",
                   "facebook.com", "weverse.io")


def details_html(html: str) -> str:
    """Keep only the Event Details .. Ticket Sales Schedule part of the post (the rest is
    related-events widgets that would confuse parsing and bloat the DB)."""
    start = html.find("Event Details")
    start = html.rfind("<", 0, start) if start > 0 else 0
    end = len(html)
    for marker in ("Total Page Views", "Deal Score", "Comebacks"):
        i = html.find(marker, start)
        if i > 0:
            end = min(end, i)
    return html[max(start, 0):end][:30_000]


def _buy_link(soup: BeautifulSoup) -> str:
    """The first external link after the 'Buy Ticket' label and before the next label
    (so 'To Be Announced (TBA)' never picks up the Official Source / Website links)."""
    label = soup.find(string=lambda s: s and s.strip() == "Buy Ticket")
    if not label:
        return ""
    for el in label.next_elements:
        if isinstance(el, NavigableString):
            if el.strip() in STOP_LABELS:
                return ""
            continue
        href = (el.get("href") or "").strip() if el.name == "a" else ""
        if href.startswith("http") and not _skip_host(href):
            return href
    return ""


def _skip_host(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in SKIP_LINK_HOSTS)


JUNK_LINE = re.compile(r"^(?:[\[\]()|·,]+|google maps|map|maps|directions|view map)$", re.IGNORECASE)


def parse_details(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
    out: dict = {}
    for i, line in enumerate(lines[:-1]):
        if line in LABELS and line not in out and lines[i + 1] not in STOP_LABELS:
            out[line] = lines[i + 1]
    if "Venue" in lines:
        i = lines.index("Venue") + 1
        rows = []
        while i < len(lines) and lines[i] not in STOP_LABELS and len(rows) < 3:
            if not JUNK_LINE.match(lines[i]):
                rows.append(lines[i])
            i += 1
        out["venue_lines"] = rows
    out["ticket_url"] = _buy_link(soup)
    m = ONSALE_RE.search(soup.get_text("\n"))
    if m:
        out["on_sale"] = m.group(1)
    return out


def _pop_location(parts: list[str], res: dict[str, str]) -> None:
    """Pop trailing country and state/province from comma parts (state only if a city part remains)."""
    if parts and (is_country(parts[-1]) or (len(parts) > 1 and not state_code(parts[-1])
                                            and not re.fullmatch(r"[A-Z]{2}", parts[-1]))):
        res["country"] = parts.pop()
    if len(parts) >= 2 and (state_code(parts[-1]) or re.fullmatch(r"[A-Z]{2}", parts[-1])):  # state / province
        res["state"] = parts.pop()


def _matches_city(name: str, hint: str) -> bool:
    return bool(norm(name)) and bool(norm(hint)) and (norm(name) == norm(hint) or norm(hint).startswith(norm(name) + " "))


def split_venue(text: str | list[str], city_hint: str = "") -> dict[str, str]:
    """Venue rows -> venue/city/state/country. Handles
    'The Wiltern, Los Angeles, CA, United States' (one line),
    ['Los Angeles County Museum of Art', 'Los Angeles, California'] (venue line + location line),
    'Peacock Theater in Los Angeles, California', and city-only rows ('Chicago, Illinois, United States').
    A lone name is a city only if it matches the post's city (title/taxonomy); otherwise it is the venue."""
    rows = [text] if isinstance(text, str) else [r for r in text if r]
    res = {"venue": "", "city": "", "state": "", "country": ""}
    if not rows:
        return res
    if len(rows) >= 2:
        res["venue"] = rows[0].strip()
        parts = [p.strip() for p in ", ".join(rows[1:]).split(",") if p.strip()]
        _pop_location(parts, res)
        res["city"] = ", ".join(parts)
        if not res["city"] and res["state"]:
            res["city"], res["state"] = res["state"], ""
        return res
    parts = [p.strip() for p in rows[0].split(",") if p.strip()]
    _pop_location(parts, res)
    if len(parts) >= 2:
        res["city"] = parts.pop()
        res["venue"] = ", ".join(parts)
    elif parts:
        one = parts[0]
        m = re.match(r"^(.+?)\s+in\s+(.+)$", one)
        if m:
            res["venue"], res["city"] = m.group(1), m.group(2)
        elif not city_hint or _matches_city(one, city_hint):
            res["city"] = one
        else:
            res["venue"] = one
    if res["city"] and city_hint and not _matches_city(res["city"], city_hint) and not res["venue"] \
            and norm(city_hint) and norm(city_hint) not in norm(res["city"]):
        # 'Los Angeles County Museum of Art' alone: a name that isn't the city is the venue.
        res["venue"], res["city"] = res["city"], ""
    return res


def city_hint(title: str, classes: list[str]) -> str:
    """The post's city from its title ('... – Los Angeles, United States') or its event-city
    taxonomy class ('event-city-los-angeles-california-united-states')."""
    m = re.search(r"[–-]\s*([^–,]+),\s*(?:United States|USA)\s*$", title or "")
    if m:
        return m.group(1).strip()
    for c in classes or []:
        if c.startswith("event-city-"):
            words = c[len("event-city-"):].split("-")
            for tail in (["united", "states"], ["usa"], ["us"]):
                if words[-len(tail):] == tail:
                    words = words[:-len(tail)]
            for n in (3, 2, 1):
                if len(words) > n and " ".join(words[-n:]) in US_STATES:
                    words = words[:-n]
                    break
            return " ".join(words).title()
    return ""


def parse_date(text: str) -> str | None:
    m = MONTH_RE.search(text or "")
    if not m:
        return None
    return iso_date(f"{m[1]} {m[2]} {m[3]}")


@register("wp_events")
class WpEventsSource(Source):
    structured = True

    def collect(self, ctx: "RunContext") -> list[RawItem]:
        params = {"per_page": int(self.params.get("per_page", 50)), "orderby": "date", "order": "desc",
                  "_fields": FIELDS}
        row = ctx.db.one("SELECT MAX(published) AS p FROM raw_items WHERE source = ?", (self.slug,))
        if row and row["p"]:
            try:
                after = datetime.fromisoformat(row["p"].replace("Z", "+00:00")) - timedelta(days=7)
                params["after"] = after.replace(tzinfo=None).isoformat(timespec="seconds")
            except ValueError:
                pass
        posts = ctx.http.get(self.params["url"], params=params).json()
        items = []
        for p in posts:
            title = html_to_text((p.get("title") or {}).get("rendered", ""))
            html = details_html((p.get("content") or {}).get("rendered", ""))
            items.append(RawItem(
                source=self.slug, item_id=str(p["id"]), url=p.get("link", ""), title=title,
                published=(p.get("date_gmt") + "+00:00") if p.get("date_gmt") else None,
                text=html_to_text(html)[:5000],
                payload={"class_list": p.get("class_list", []), "details_html": html,
                         "modified_gmt": p.get("modified_gmt")},
            ))
        return items

    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        classes = item.payload.get("class_list", []) or []
        d = parse_details(item.payload.get("details_html", ""))
        hint = city_hint(item.title, classes)
        place = split_venue(d.get("venue_lines") or d.get("Venue", ""), hint)
        region = self.region(ctx)
        us_tax = region == "US" and "event-country-united-states" in classes
        if not (us_tax or is_country(place["country"], region)):
            return []
        if not place["city"]:
            place["city"] = hint
        artist = d.get("Artist", "")
        event = d.get("Event") or item.title
        if not artist:
            return []
        return [make_record(
            self.slug, self.segment, item.url,
            event=event, artist=artist, city=place["city"], state=place["state"], venue=place["venue"],
            event_date=parse_date(d.get("Date", "")), on_sale_date=iso_date(d.get("on_sale")),
            ticket_url=d.get("ticket_url", ""), source_event_id=f"kpopofficial:{item.item_id}",
            raw_text=item.text,
        )]
