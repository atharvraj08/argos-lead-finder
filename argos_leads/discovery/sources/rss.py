"""RSS/Atom news feeds (Billboard, kpopconcerts, Pollstar, ANN, JRockNews, Mama's Geeky).

collect: fetch the feed through ctx.http, store every entry (filtering happens in parse so
config changes apply to stored items). parse: keep entries matching include_terms (and the
segment's artist_watchlist when include_watchlist: true), then LLM-extract U.S. dates.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import feedparser

from ...models import EventRecord, RawItem
from ..extract import extract_records, news_text
from ..util import html_to_text, matches, stable_id, term_pattern
from .base import Source, register

if TYPE_CHECKING:
    from ...context import RunContext

SUMMARY_LIMIT = 20_000


def _published(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc).isoformat()


@register("rss")
class RssSource(Source):
    def collect(self, ctx: "RunContext") -> list[RawItem]:
        resp = ctx.http.get(self.params["url"])
        feed = feedparser.parse(resp.text)
        items: list[RawItem] = []
        for e in feed.entries:
            link = e.get("link") or ""
            guid = e.get("id") or link or stable_id(e.get("title", ""))
            body = ""
            if e.get("content"):
                body = e["content"][0].get("value", "")
            text = html_to_text(body or e.get("summary", ""))[:SUMMARY_LIMIT]
            items.append(RawItem(
                source=self.slug, item_id=guid, url=link, title=html_to_text(e.get("title", "")),
                published=_published(e), text=text,
                payload={"tags": [t.get("term") for t in e.get("tags", []) if t.get("term")]},
            ))
        return items

    def wanted(self, item: RawItem) -> bool:
        """Cheap pre-LLM filter on title + summary + tags."""
        hay = " ".join([item.title, item.text, " ".join(item.payload.get("tags", []))])
        terms = list(self.params.get("include_terms") or [])
        watch = list(self.segment_cfg.artist_watchlist) if self.params.get("include_watchlist") else []
        if not terms and not watch:
            return True
        return matches(hay, term_pattern(terms + watch))

    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        if not self.wanted(item):
            return []
        text, url = news_text(ctx, self, item)
        return extract_records(ctx, self, item, text, url)
