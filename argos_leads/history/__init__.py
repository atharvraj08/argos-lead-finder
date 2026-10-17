"""Scalping history: past scalping / bot / resale incidents for this week's new leads.

Evidence that ID verification at purchase would help. For each distinct artist among the
leads passed in (never a backfill), one targeted web search; if that finds nothing, one
search on the lead's ticket seller. Results (including negatives) are cached per artist
and per seller in `scalping_cache` for `caching.scalping_recheck_days`, so repeat tours
reuse evidence and misses are not re-searched every week.

Only a one-line paraphrased summary (<= SUMMARY_MAX chars) and a source URL are stored,
never page text.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urlparse

from pydantic import BaseModel

from ..models import Lead, norm

log = logging.getLogger(__name__)

SUMMARY_MAX = 160
SEARCHES_PER_QUERY = 1  # web-search tool uses per LLM call; 2 calls max per event


class ScalpingFinding(BaseModel):
    found: bool = False
    summary: str = ""  # one short paraphrased line
    url: str = ""  # page backing the summary


ARTIST_QUESTION = (
    "Find a past news report of ticket scalping, bots, or inflated resale prices affecting "
    "concerts or ticket sales by the artist \"{key}\" (any country; U.S. preferred). "
    "Search once, e.g. '{key} tickets scalping bots resale prices'. If a relevant report "
    "exists, set found=true, summary to ONE short line in your own words (max 150 characters, "
    "no quotes from the article), and url to that report's URL. Otherwise found=false and "
    "leave summary and url empty."
)

SELLER_QUESTION = (
    "Find a past news report of ticket bots, scalpers, or resale abuse affecting tickets "
    "sold by the ticketing company \"{key}\". Search once, e.g. '{key} ticket bots scalpers'. "
    "If a relevant report exists, set found=true, summary to ONE short line in your own words "
    "(max 150 characters, no quotes from the article), and url to that report's URL. "
    "Otherwise found=false and leave summary and url empty."
)


def clean_summary(text: str | None, limit: int = SUMMARY_MAX) -> str:
    """Force a single short line: collapse whitespace, drop quoted passages, keep the first
    sentence, and trim at a word boundary to `limit` chars."""
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    # Long quoted spans are likely copied text; drop them.
    s = re.sub(r"[\"“][^\"”]{40,}[\"”]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" \"'“”")
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    if m and len(m.group(1)) >= 20:
        s = m.group(1)
    if len(s) > limit:
        cut = s[: limit - 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        s = cut.rstrip(" ,;:-") + "…"
    return s


def valid_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    host = p.hostname or ""
    return p.scheme in ("http", "https") and "." in host and " " not in url.strip()


def _parse_day(ts: str | None) -> date | None:
    try:
        return date.fromisoformat((ts or "")[:10])
    except ValueError:
        return None


def _cache_get(ctx, kind: str, key_norm: str) -> tuple[str, str] | None:
    """Fresh cached (summary, url) or None if missing/stale. ("", "") is a cached miss."""
    row = ctx.db.one("SELECT summary, url, checked_at FROM scalping_cache WHERE kind=? AND key_norm=?",
                     (kind, key_norm))
    if row is None:
        return None
    checked = _parse_day(row["checked_at"])
    max_age = ctx.config.targets.caching.scalping_recheck_days
    if checked is None or (ctx.today - checked).days > max_age:
        return None
    return row["summary"] or "", row["url"] or ""


def _cache_put(ctx, kind: str, key_norm: str, summary: str, url: str) -> None:
    ctx.db.execute(
        "INSERT INTO scalping_cache (kind, key_norm, summary, url, checked_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(kind, key_norm) DO UPDATE SET summary=excluded.summary, url=excluded.url, "
        "checked_at=excluded.checked_at",
        (kind, key_norm, summary, url, ctx.today.isoformat()),
    )


def _search(ctx, question: str) -> tuple[str, str] | None:
    """Run one search. Returns (summary, url) — ("", "") when nothing usable — or None when
    the call itself failed (not cached, retried next run)."""
    try:
        res = ctx.llm.search(ScalpingFinding, question, max_searches=SEARCHES_PER_QUERY)
    except Exception as e:  # never let one lookup break the pipeline
        log.warning("scalping search failed: %s", e)
        return None
    if res is None:
        return None
    summary = clean_summary(res.summary)
    url = (res.url or "").strip()
    if not res.found or not summary or not valid_url(url):
        return "", ""
    return summary, url


def lookup(ctx, kind: str, name: str) -> tuple[str, str]:
    """Cached lookup for one artist or seller. Returns (summary, url), blank if none."""
    key = norm(name)
    if not key:
        return "", ""
    cached = _cache_get(ctx, kind, key)
    if cached is not None:
        return cached
    template = ARTIST_QUESTION if kind == "artist" else SELLER_QUESTION
    result = _search(ctx, template.format(key=name.strip()))
    if result is None:
        return "", ""
    _cache_put(ctx, kind, key, *result)
    return result


def seller_summary(seller: str, summary: str) -> str:
    """Prefix seller evidence so it is clearly about the seller, not the artist."""
    prefix = f"Seller {seller.strip()}: "
    return prefix + clean_summary(summary, SUMMARY_MAX - len(prefix) if len(prefix) < 80 else 80)


def apply(ctx, leads: list[Lead]) -> None:
    """Set scalping_evidence_summary/url on this week's new leads (artist evidence first,
    seller evidence as fallback). Same artist/seller across rows -> one lookup."""
    memo: dict[tuple[str, str], tuple[str, str]] = {}

    def get(kind: str, name: str) -> tuple[str, str]:
        k = (kind, norm(name))
        if k not in memo:
            memo[k] = lookup(ctx, kind, name)
        return memo[k]

    for lead in leads:
        summary, url = get("artist", lead.artist_or_headliner)
        if summary and url:
            lead.scalping_evidence_summary, lead.scalping_evidence_url = summary, url
            continue
        seller = lead.ticket_seller
        s_summary, s_url = get("seller", seller) if norm(seller) else ("", "")
        if s_summary and s_url:
            lead.scalping_evidence_summary = seller_summary(seller, s_summary)
            lead.scalping_evidence_url = s_url
        else:
            lead.scalping_evidence_summary, lead.scalping_evidence_url = "", ""
    log.info("scalping history applied", extra={"count": sum(1 for l in leads if l.scalping_evidence_url)})
