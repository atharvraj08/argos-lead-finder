"""GDELT DOC 2.0 API: U.S. news articles from the last 7 days matching the segment query.

GDELT asks for >= 6 s between calls; that spacing comes from
`http.host_min_interval_s: {api.gdeltproject.org: 6}` in targets.yaml (one call per source).
GDELT returns only title/url/date, so extraction works on the title unless the source sets
`fetch_article: true`. Cite "GDELT Project" (gdeltproject.org) in output.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ...models import EventRecord, RawItem
from ..extract import DEFAULT_EVENT_TERMS, extract_records, news_text
from ..util import matches, term_pattern
from .base import Source, register

if TYPE_CHECKING:
    from ...context import RunContext

API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _seendate(s: str) -> str | None:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


@register("gdelt")
class GdeltSource(Source):
    def collect(self, ctx: "RunContext") -> list[RawItem]:
        params = {
            "query": self.params["query"],
            "mode": "ArtList",
            "format": "json",
            "maxrecords": int(self.params.get("maxrecords", 250)),
            "timespan": self.params.get("timespan", "7d"),
            "sort": "DateDesc",
        }
        resp = ctx.http.get(API_URL, params=params, use_cache=False)
        text = resp.text.strip()
        if not text:
            return []
        try:
            data = json.loads(text, strict=False)
        except json.JSONDecodeError:
            # GDELT answers throttling and query errors with a plain-text message.
            raise RuntimeError(f"gdelt returned non-JSON: {text[:200]}")
        items = []
        for a in data.get("articles", []) or []:
            url = a.get("url") or ""
            if not url:
                continue
            items.append(RawItem(
                source=self.slug, item_id=url, url=url, title=a.get("title", "") or "",
                published=_seendate(a.get("seendate", "")), text="",
                payload={k: a.get(k) for k in ("domain", "language", "sourcecountry", "seendate")},
            ))
        return items

    def wanted(self, item: RawItem) -> bool:
        terms = self.params.get("include_terms") or DEFAULT_EVENT_TERMS
        return matches(item.title, term_pattern(terms))

    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        if not self.wanted(item):
            return []
        text, url = news_text(ctx, self, item)
        return extract_records(ctx, self, item, text, url)
