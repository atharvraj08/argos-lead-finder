"""SeatGeek Platform API (structured: performer, venue, city, date, url).

SeatGeek is largely a resale marketplace, so ticket_seller is never set from it; the
listing URL is kept as source_url and enrichment decides the primary seller.
Needs SEATGEEK_CLIENT_ID; without it the source is skipped with a warning.
Responses are not written to the HTTP cache because the URL carries the client_id
(the SQLite DB is committed to the repo).
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from ...models import EventRecord, RawItem, norm
from ..util import is_in_region, iso_date, make_record
from .base import Source, SourceSkipped, register

if TYPE_CHECKING:
    from ...context import RunContext

API_URL = "https://api.seatgeek.com/2/events"
ENV_KEY = "SEATGEEK_CLIENT_ID"
MAX_PAGES = 5


@register("seatgeek")
class SeatGeekSource(Source):
    structured = True

    def queries(self) -> list[tuple[str, str]]:
        """(kind, query) pairs: configured genre queries, plus watchlist artists if enabled."""
        qs = [("query", q) for q in self.params.get("queries", [])]
        if self.params.get("include_watchlist"):
            qs += [("artist", a) for a in self.segment_cfg.artist_watchlist]
        return qs

    def collect(self, ctx: "RunContext") -> list[RawItem]:
        client_id = os.environ.get(ENV_KEY, "").strip()
        if not client_id:
            raise SourceSkipped(f"{ENV_KEY} not set")
        seen: dict[str, RawItem] = {}
        for kind, q in self.queries():
            for page in range(1, MAX_PAGES + 1):
                params = {
                    "client_id": client_id, "q": q, "venue.country": "US", "per_page": 100, "page": page,
                    "datetime_utc.gte": ctx.today.isoformat(),
                }
                data = ctx.http.get(API_URL, params=params, use_cache=False).json()
                events = data.get("events", []) or []
                for ev in events:
                    eid = str(ev.get("id", ""))
                    if not eid or eid in seen:
                        continue
                    if not self.relevant(ev, kind, q):
                        continue
                    seen[eid] = RawItem(
                        source=self.slug, item_id=eid, url=ev.get("url", "") or "",
                        title=ev.get("title", "") or "", published=ev.get("announce_date") or ev.get("created_at"),
                        payload=ev,
                    )
                meta = data.get("meta", {}) or {}
                if len(events) < 100 or page * 100 >= int(meta.get("total", 0) or 0):
                    break
        return list(seen.values())

    def relevant(self, ev: dict, kind: str, q: str) -> bool:
        """Full-text search is loose: keep events whose performer genre matches the configured
        genre slug, or whose title/performers contain the query (or watchlist artist)."""
        performers = ev.get("performers", []) or []
        genre = self.params.get("genre_slug")
        if genre and any(g.get("slug") == genre for p in performers for g in (p.get("genres") or [])):
            return True
        nq = norm(q)
        names = [norm(p.get("name")) for p in performers]
        if kind == "artist":
            return any(n == nq or f" {nq} " in f" {n} " for n in names)
        hay = " ".join([norm(ev.get("title"))] + names)
        return f" {nq} " in f" {hay} " or nq.replace(" ", "") in hay.replace(" ", "")

    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        ev = item.payload
        venue = ev.get("venue", {}) or {}
        if not is_in_region(venue.get("country"), venue.get("state"), self.region(ctx)):
            return []
        performers = ev.get("performers", []) or []
        primary = next((p for p in performers if p.get("primary")), performers[0] if performers else {})
        artist = (primary.get("name") or ev.get("short_title") or ev.get("title") or "").strip()
        event_date = None if ev.get("datetime_tbd") or ev.get("date_tbd") else iso_date(ev.get("datetime_local"))
        url = ev.get("url") or item.url
        return [make_record(
            self.slug, self.segment, url,
            event=(ev.get("title") or artist).strip(), artist=artist,
            city=venue.get("city", "") or "", state=venue.get("state", "") or "", venue=venue.get("name", "") or "",
            # ticket_url left empty on purpose: a SeatGeek link would make enrichment name
            # SeatGeek (a resale marketplace) as the seller. The listing stays in source_url.
            event_date=event_date, source_event_id=f"seatgeek:{item.item_id}",
        )]
