"""Small helpers shared by discovery adapters: text cleanup, term filters, U.S. checks,
date parsing, and EventRecord construction with per-field provenance."""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Any, Iterable

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from ..models import EventRecord

US_STATES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC", "washington d c": "DC", "puerto rico": "PR",
}
US_STATE_CODES = set(US_STATES.values())

# Country spellings per region code (config `region`). Only US is used today.
REGION_COUNTRIES: dict[str, set[str]] = {
    "US": {"us", "usa", "u s", "u s a", "united states", "united states of america", "america"},
}

# Fields merged across sources (in EventRecord order).
RECORD_FIELDS = [
    "event", "artist", "city", "state", "venue", "event_date", "on_sale_date", "ticket_url",
    "ticket_seller", "promoter", "organizer", "agency", "source_event_id",
]


def _n(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def state_code(state: str | None) -> str:
    """Return the 2-letter U.S. state code for a code or full name, else ''."""
    s = _n(state)
    if not s:
        return ""
    if s.upper() in US_STATE_CODES and len(s) == 2:
        return s.upper()
    return US_STATES.get(s, "")


def is_country(country: str | None, region: str = "US") -> bool:
    return _n(country) in REGION_COUNTRIES.get(region.upper(), {_n(region)})


def is_in_region(country: str | None = "", state: str | None = "", region: str = "US") -> bool:
    """True when the stated country matches the region, or (US only, no country stated)
    the state is a recognised U.S. state."""
    if _n(country):
        return is_country(country, region)
    return region.upper() == "US" and bool(state_code(state))


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def article_text(html: str, limit: int = 60_000) -> str:
    """Main readable text of an article page (prefers <article>, drops nav/footer)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    return re.sub(r"\n{3,}", "\n\n", root.get_text("\n", strip=True))[:limit]


def term_pattern(terms: Iterable[str]) -> re.Pattern | None:
    """Case-insensitive alternation with word-ish boundaries ("Ado" won't match "Adobe")."""
    parts = [re.escape(t.strip()) for t in terms if t and t.strip()]
    if not parts:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(sorted(parts, key=len, reverse=True)) + r")(?![A-Za-z0-9])",
                      re.IGNORECASE)


def matches(text: str, pattern: re.Pattern | None) -> bool:
    return pattern is None or bool(pattern.search(text or ""))


def iso_date(value: Any, default_year: int | None = None) -> str | None:
    """Normalise a date-ish value to YYYY-MM-DD, or None if it can't be read."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None
    try:
        default = datetime(default_year or datetime.now().year, 1, 1)
        return dateparser.parse(s, fuzzy=True, default=default).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None


def stable_id(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]


def tidy_place(s: str) -> str:
    """Trim and title-case ALL-CAPS place names ("LOS ANGELES" -> "Los Angeles")."""
    s = (s or "").strip(" ,.-–")
    if s.isupper() and len(s) > 3:
        s = s.title()
    return s


def make_record(source: str, segment: str, url: str, **fields: Any) -> EventRecord:
    """Build an EventRecord; every non-empty field is attributed to `url` unless the
    caller passes its own `field_sources` entries."""
    given = dict(fields.pop("field_sources", {}) or {})
    rec = EventRecord(source=source, segment=segment, event=fields.pop("event", "") or "",
                      artist=fields.pop("artist", "") or "", source_url=url)
    for k, v in fields.items():
        setattr(rec, k, v if v is not None else getattr(rec, k))
    rec.city = tidy_place(rec.city)
    rec.venue = (rec.venue or "").strip()
    rec.state = state_code(rec.state) or (rec.state or "").strip()
    fs: dict[str, str] = {}
    for f in RECORD_FIELDS:
        if getattr(rec, f):
            fs[f] = given.get(f, url)
    rec.field_sources = fs
    return rec
