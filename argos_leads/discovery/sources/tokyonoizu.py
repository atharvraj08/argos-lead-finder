"""Tokyo Noizu "Japanese band tours" tracker (single page, one fetch a week).

The page is grouped by region (<h2> "... TOURS"); each band block has a headline, a
"Dates:" list ("12 October US, Los Angeles, CA, The Wiltern" or "-10 November USA, POMONA,
The Glasshouse") and a "Ticket Info" button. Rows rarely carry a year, so it is inferred
from the headline (e.g. "TOUR 2027") or the page's "last updated" date.
collect is skipped when the "last updated" string hasn't changed since the last run.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from ...db import utcnow
from ...models import EventRecord, RawItem, norm
from ..util import US_STATES, is_country, iso_date, make_record, stable_id, state_code, tidy_place
from .base import Source, register

if TYPE_CHECKING:
    from ...context import RunContext

STATE_SQL = """
CREATE TABLE IF NOT EXISTS discovery_state (     -- per-source small state (owned by discovery)
    source TEXT NOT NULL, key TEXT NOT NULL, value TEXT, updated_at TEXT,
    PRIMARY KEY (source, key)
);
"""
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
ROW_RE = re.compile(r"^[-–—•\s]*(\d{1,2})(?:\s*[–-]\s*\d{1,2})?(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s+(.+)$")
UPDATED_RE = re.compile(r"last updated:?\s*([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(20\d{2})\b")
ANNOTATIONS = re.compile(r"^(sold out|new show|low ticket alert|cancelled|canceled|postponed|rescheduled|"
                         r"moved to .*|upgraded venue|support.*)$", re.IGNORECASE)
HEADLINE_STOP = {"north", "world", "us", "usa", "u.s.", "america", "american", "europe", "european", "tour", "live",
                 "asia", "latin", "japan", "uk", "australia", "concert", "headline"}


def get_state(ctx: "RunContext", source: str, key: str) -> str | None:
    ctx.db.ensure(STATE_SQL)
    row = ctx.db.one("SELECT value FROM discovery_state WHERE source = ? AND key = ?", (source, key))
    return row["value"] if row else None


def set_state(ctx: "RunContext", source: str, key: str, value: str) -> None:
    ctx.db.ensure(STATE_SQL)
    ctx.db.execute("INSERT OR REPLACE INTO discovery_state (source, key, value, updated_at) VALUES (?, ?, ?, ?)",
                   (source, key, value, utcnow()))


def last_updated(soup: BeautifulSoup) -> str:
    m = UPDATED_RE.search(soup.get_text(" ", strip=True))
    return m.group(1).strip() if m else ""


def _aid(prefix: str):
    return lambda v: bool(v) and v.startswith(prefix)


def parse_blocks(html: str) -> list[dict]:
    """Band blocks under each '... TOURS' region heading."""
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for sec in soup.find_all("section"):
        h2 = sec.find("h2")
        region = h2.get_text(" ", strip=True) if h2 else ""
        if "TOUR" not in region.upper():
            continue
        for b in sec.find_all("div", attrs={"data-ux": "ContentBasic"}):
            hl = b.find(attrs={"data-aid": _aid("ABOUT_HEADLINE")})
            desc = b.find(attrs={"data-aid": _aid("ABOUT_DESCRIPTION")})
            btn = b.find("a", attrs={"data-aid": _aid("ABOUT_CTA_BTN")})
            if not hl or not desc:
                continue
            paras = desc.find_all("p") or [desc]
            lines = [p.get_text(" ", strip=True) for p in paras]
            lines = [l for l in lines if l]
            blocks.append({
                "headline": hl.get_text(" ", strip=True), "region": region, "lines": lines,
                "ticket_url": (btn.get("href") or "").strip() if btn else "",
            })
    return blocks


LEAD_IN = re.compile(r"^(?:what\s+is|who\s+is|this\s+is|meet|presenting|introducing)\s+", re.IGNORECASE)


def _compact(text: str) -> str:
    return norm(text).replace(" ", "")


def artist_from_headline(headline: str, watchlist: list[str], ticket_url: str = "", lines: list[str] | None = None) -> str:
    """Artist name from a band block. Order: a watchlist name in the headline; else the
    headline text before the tour title, with lead-in words removed ("What is Paledusk??"
    -> "Paledusk") using the block's own evidence (ticket link, block text)."""
    h = headline.strip()
    hn = f" {norm(h)} "
    for name in sorted(watchlist, key=len, reverse=True):
        if norm(name) and f" {norm(name)} " in hn:
            return name
    parts = re.split(r"\s+[-–—]\s*|:\s+", h, maxsplit=1)
    delimited = len(parts) == 2 and bool(parts[0].strip())
    if delimited:
        cand = parts[0]  # "BURNOUT SYNDROMES - Tour name": the name is explicit, keep it whole
    else:
        out = []
        for w in h.split():
            if w.lower().strip("\"“”'") in HEADLINE_STOP or YEAR_RE.fullmatch(w.strip("\"“”'")) or w[:1] in "\"“”'″":
                break
            out.append(w)
        cand = " ".join(out) or h
    cand = cand.strip(" \"“”'?").strip()
    words = cand.split()
    if len(words) > 1 and not delimited:
        # Evidence: the ticket link (strongest), then block lines that don't just repeat the headline.
        url_ev = _compact(ticket_url)
        text_ev = " ".join(_compact(l) for l in (lines or []) if _compact(h) not in _compact(l))
        if _compact(cand) not in url_ev + " " + text_ev:
            prefixes = [" ".join(words[:i]) for i in range(len(words) - 1, 0, -1)]
            suffixes = [" ".join(words[i:]) for i in range(1, len(words))]
            for options, ev in ((prefixes, url_ev), (suffixes, url_ev), (suffixes, text_ev)):
                for o in options:
                    if len(_compact(o)) >= 4 and _compact(o) in ev:
                        return o.strip(" \"“”'?")
            stripped = LEAD_IN.sub("", cand)
            if stripped and stripped != cand:
                return stripped.strip(" \"“”'?")
    return cand


def parse_row(line: str) -> dict | None:
    m = ROW_RE.match(line)
    if not m or m[2].lower() not in MONTHS:
        return None
    parts = [p.strip() for p in m[3].split(",") if p.strip()]
    if len(parts) < 2:
        return None
    row = {"day": int(m[1]), "month": MONTHS[m[2].lower()], "country": "", "city": "", "state": "", "venue": ""}
    if not is_country(parts[0]) and len(parts[1]) == 2 and state_code(parts[1]):
        # "San Francisco, CA, August Hall": no country column, but a stated U.S. state code.
        row.update(country="US", city=parts[0], state=parts[1])
        rest = parts[2:]
    else:
        row.update(country=parts[0], city=parts[1])
        rest = parts[2:]
        if rest and state_code(rest[0]) and (len(rest[0]) == 2 or norm(rest[0]) in US_STATES):
            row["state"] = rest.pop(0)  # "USA, Brooklyn, NY, Meadows" / "..., New York, ..."
    if not row["state"] and is_country(row["country"]):
        m2 = re.match(r"^(.*\S)\s+([A-Z]{2})$", row["city"])
        if m2 and state_code(m2.group(2)):
            row["city"], row["state"] = m2.group(1), m2.group(2)  # "BROOKLYN NY"
    # No state on the page -> left blank (never guessed from the city).
    rest = [p for p in rest if not ANNOTATIONS.match(p)]
    row["venue"] = re.sub(r"\s*\(.*?\)\s*$", "", ", ".join(rest)).strip()
    row["city"] = tidy_place(row["city"])
    # "Brooklyn Bowl | Philadelphia", "House of Blues | Chicago": drop the repeated city.
    row["venue"] = re.sub(r"\s*\|\s*" + re.escape(row["city"]) + r"$", "", row["venue"], flags=re.IGNORECASE)
    return row


def assign_years(rows: list[dict], headline: str, updated: date) -> None:
    hy = YEAR_RE.search(headline)
    year = int(hy.group(1)) if hy else updated.year
    prev = None
    for r in rows:
        if prev is not None and r["month"] < prev:
            year += 1
        elif prev is None and not hy:
            try:
                if date(year, r["month"], r["day"]) < updated - timedelta(days=120):
                    year += 1
            except ValueError:
                pass
        prev = r["month"]
        r["year"] = year


@register("tokyonoizu")
class TokyoNoizuSource(Source):
    structured = True

    def collect(self, ctx: "RunContext") -> list[RawItem]:
        resp = ctx.http.get(self.params["url"])
        soup = BeautifulSoup(resp.text, "html.parser")
        updated = last_updated(soup)
        if updated and updated == get_state(ctx, self.slug, "last_updated"):
            self._pending_state = None
            return []
        self._pending_state = updated
        items = []
        for b in parse_blocks(resp.text):
            b["last_updated"] = updated
            items.append(RawItem(
                source=self.slug, item_id=stable_id(b["headline"], "\n".join(b["lines"]), b["ticket_url"]),
                url=self.params["url"], title=b["headline"], published=iso_date(updated),
                text="\n".join(b["lines"])[:8000], payload=b,
            ))
        return items

    def after_collect(self, ctx: "RunContext") -> None:
        if getattr(self, "_pending_state", None):
            set_state(ctx, self.slug, "last_updated", self._pending_state)

    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        b = item.payload
        updated = date.fromisoformat(iso_date(b.get("last_updated")) or ctx.today.isoformat())
        rows = [r for r in (parse_row(l) for l in b.get("lines", [])) if r]
        assign_years(rows, b.get("headline", ""), updated)
        artist = artist_from_headline(b.get("headline", ""), self.segment_cfg.artist_watchlist,
                                      b.get("ticket_url", ""), b.get("lines", []))
        region = self.region(ctx)
        out = []
        for r in rows:
            if not is_country(r["country"], region):
                continue
            try:
                d = date(r["year"], r["month"], r["day"]).isoformat()
            except ValueError:
                d = None
            out.append(make_record(
                self.slug, self.segment, item.url,
                event=b.get("headline", ""), artist=artist, city=r["city"], state=r["state"], venue=r["venue"],
                event_date=d, ticket_url=b.get("ticket_url", ""), source_event_id=f"tokyonoizu:{item.item_id}",
                raw_text=item.text,
            ))
        return out
