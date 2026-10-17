"""Shared record types passed between pipeline stages.

Flow: discovery -> EventRecord -> enrich -> Lead -> competitors/history fill
competitor_* and scalping_* -> scoring fills score/score_breakdown -> output.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

# Fixed output columns, same for every segment, in the DB and the Leads tab.
COLUMNS: list[str] = [
    "week_found", "segment", "event", "artist_or_headliner", "city", "venue", "event_date",
    "on_sale_date", "ticket_seller", "agency", "promoter", "organizer",
    "competitor_vendor", "competitor_status", "competitor_evidence_url",
    "scalping_evidence_summary", "scalping_evidence_url", "score", "score_breakdown",
    "sources",
]

COMPETITOR_STATUSES = ("confirmed", "likely", "unknown")


def norm(text: str | None) -> str:
    """Normalize a name for matching and cache keys: lowercase ASCII, single spaces."""
    if not text:
        return ""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    ascii_text = re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()
    if ascii_text:
        return ascii_text
    # Names written only in Hangul/kana/kanji: keep the characters so different artists don't collide.
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


@dataclass
class RawItem:
    """One item as collected from a source (feed entry, API event, page row), before parsing."""

    source: str  # source slug
    item_id: str  # stable per-source id (guid, API id, url)
    url: str
    title: str = ""
    published: str | None = None  # ISO 8601
    text: str = ""  # plain text (summary/body) for filtering and LLM extraction
    payload: dict[str, Any] = field(default_factory=dict)  # raw structured data


@dataclass
class EventRecord:
    """A single U.S. event date (one city/venue/date) found by discovery."""

    source: str
    segment: str
    event: str  # event or tour name
    artist: str
    city: str = ""
    state: str = ""
    venue: str = ""
    event_date: str | None = None  # YYYY-MM-DD
    on_sale_date: str | None = None  # YYYY-MM-DD (general on-sale)
    ticket_url: str = ""
    ticket_seller: str = ""
    promoter: str = ""
    organizer: str = ""
    agency: str = ""
    source_url: str = ""  # page the record came from
    source_event_id: str = ""
    raw_text: str = ""  # fetched text kept for enrichment
    field_sources: dict[str, str] = field(default_factory=dict)  # field -> source URL

    def key(self) -> str:
        """Dedup key across sources and runs: artist + city + event date."""
        return "|".join([norm(self.artist), norm(self.city), self.event_date or "tbd"])


@dataclass
class Lead:
    """One output row. Field names match COLUMNS exactly, plus provenance metadata."""

    event_key: str
    week_found: str
    segment: str
    event: str
    artist_or_headliner: str
    city: str = ""
    venue: str = ""
    event_date: str = ""
    on_sale_date: str = ""
    ticket_seller: str = ""
    agency: str = ""
    promoter: str = ""
    organizer: str = ""
    competitor_vendor: str = ""
    competitor_status: str = ""
    competitor_evidence_url: str = ""
    scalping_evidence_summary: str = ""
    scalping_evidence_url: str = ""
    score: int = 0
    score_breakdown: str = ""
    sources: str = ""  # space-separated source URLs
    # Provenance, not output columns:
    field_sources: dict[str, str] = field(default_factory=dict)  # field -> source URL
    low_confidence: list[str] = field(default_factory=list)  # fields filled with low confidence
    ticket_url: str = ""
    excluded_reason: str = ""  # set when a rule excludes the row from output

    def row(self) -> list[Any]:
        d = asdict(self)
        return [d[c] for c in COLUMNS]
