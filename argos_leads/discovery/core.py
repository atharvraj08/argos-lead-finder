"""Discovery orchestration: collect raw items from sources, then turn them into new,
deduplicated U.S. EventRecords per segment."""
from __future__ import annotations

import copy
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Iterable

from ..db import Database, utcnow
from ..http import RobotsDisallowed
from ..models import EventRecord, RawItem
from .sources import ItemFailed, Source, SourceSkipped, get_adapter
from .util import RECORD_FIELDS

if TYPE_CHECKING:
    from ..context import RunContext

log = logging.getLogger("argos_leads.discovery")


# --- helpers ------------------------------------------------------------------
def build_sources(ctx: "RunContext", segments: Iterable[str] | None = None, daily_only: bool = False) -> list[Source]:
    targets = ctx.config.targets
    active = targets.active()
    names = list(segments) if segments else list(active)
    out: list[Source] = []
    for seg in names:
        seg_cfg = targets.segments.get(seg)
        if seg_cfg is None:
            raise ValueError(f"unknown segment {seg!r}")
        for scfg in seg_cfg.sources:
            if daily_only and not scfg.daily:
                continue
            try:
                cls = get_adapter(scfg.adapter)
            except KeyError as e:
                log.error(str(e), extra={"source": scfg.slug, "segment": seg})
                _log_source(ctx, scfg.slug, "error", 0, str(e))
                continue
            out.append(cls(scfg, seg, seg_cfg))
    return out


def _log_source(ctx: "RunContext", source: str, status: str, items: int, message: str = "") -> None:
    ctx.db.execute("INSERT INTO source_log (run_id, source, status, items, message, at) VALUES (?, ?, ?, ?, ?, ?)",
                   (ctx.run_id, source, status, items, message[:1000], utcnow()))


def upsert_raw(ctx: "RunContext", items: list[RawItem]) -> int:
    """Insert new raw items; existing (source, item_id) rows are left untouched (so
    processed_at is never overwritten). Returns the number of new rows."""
    new = 0
    now = utcnow()
    for it in items:
        cur = ctx.db.conn.execute(
            "INSERT OR IGNORE INTO raw_items (source, item_id, url, title, published, text, payload_json, collected_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (it.source, it.item_id, it.url, it.title, it.published, it.text, ctx.db.dumps(it.payload), now),
        )
        new += cur.rowcount
    ctx.db.conn.commit()
    return new


def _row_to_item(row) -> RawItem:
    return RawItem(source=row["source"], item_id=row["item_id"], url=row["url"] or "", title=row["title"] or "",
                   published=row["published"], text=row["text"] or "", payload=Database.loads(row["payload_json"]) or {})


# --- collect --------------------------------------------------------------------
def collect(ctx: "RunContext", segments: Iterable[str] | None = None, daily_only: bool = False) -> dict[str, int]:
    """Run each source's collect() and store new raw items. Returns {source slug: new items}.
    A failing or skipped source is logged to source_log and never stops the others."""
    counts: dict[str, int] = {}
    for src in build_sources(ctx, segments, daily_only):
        extra = {"source": src.slug, "segment": src.segment}
        try:
            items = src.collect(ctx)
            n = upsert_raw(ctx, items)
            src.after_collect(ctx)
        except SourceSkipped as e:
            log.warning("source skipped: %s", e, extra=extra)
            _log_source(ctx, src.slug, "skipped", 0, str(e))
            counts[src.slug] = 0
            continue
        except RobotsDisallowed as e:
            log.warning("source skipped: robots.txt disallows %s", e, extra=extra)
            _log_source(ctx, src.slug, "skipped", 0, f"robots.txt disallows {e}")
            counts[src.slug] = 0
            continue
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the run
            log.exception("source failed: %s", e, extra=extra)
            _log_source(ctx, src.slug, "error", 0, f"{type(e).__name__}: {e}")
            counts[src.slug] = 0
            continue
        counts[src.slug] = n
        log.info("collected %d items (%d new)", len(items), n, extra=dict(extra, count=n))
        _log_source(ctx, src.slug, "ok", n, f"{len(items)} fetched, {n} new")
    return counts


# --- discover -------------------------------------------------------------------
def merge_records(records: list[EventRecord], structured: dict[str, bool]) -> EventRecord:
    """Merge records describing the same event (same key). Structured sources win;
    empty fields are filled from the others, each with its own provenance URL.
    field_sources['sources'] holds every reporting URL, space-separated."""
    ordered = sorted(records, key=lambda r: (not structured.get(r.source, False)))
    base = copy.deepcopy(ordered[0])
    fs = dict(base.field_sources)
    for other in ordered[1:]:
        for f in RECORD_FIELDS:
            if not getattr(base, f) and getattr(other, f):
                setattr(base, f, getattr(other, f))
                fs[f] = other.field_sources.get(f, other.source_url)
        if not base.raw_text and other.raw_text:
            base.raw_text = other.raw_text
    urls: list[str] = []
    for r in ordered:
        for u in [r.source_url] + r.field_sources.get("sources", "").split():
            if u and u not in urls:
                urls.append(u)
    fs["sources"] = " ".join(urls)
    base.field_sources = fs
    return base


def _keep(rec: EventRecord, ctx: "RunContext") -> bool:
    if not rec.artist or not (rec.city or rec.venue):
        return False
    if rec.event_date and rec.event_date < ctx.today.isoformat():
        return False
    return True


def discover(ctx: "RunContext", segment: str) -> list[EventRecord]:
    """Parse this segment's unprocessed raw items into new U.S. events (deduped across
    sources and past runs), store them in `events`, mark items processed, return them."""
    sources = build_sources(ctx, [segment])
    structured = {s.slug: s.structured for s in sources}
    records: list[EventRecord] = []
    done: list[tuple[str, str]] = []
    for src in sources:
        rows = ctx.db.query("SELECT * FROM raw_items WHERE source = ? AND processed_at IS NULL", (src.slug,))
        ok = failed = 0
        for row in rows:
            item = _row_to_item(row)
            try:
                recs = src.parse_item(ctx, item)
            except ItemFailed as e:
                failed += 1
                log.warning("item left for retry: %s", e, extra={"source": src.slug, "url": item.url})
                continue
            except Exception as e:  # noqa: BLE001 - bad item must not stop discovery
                failed += 1
                log.exception("item parse error: %s", e, extra={"source": src.slug, "url": item.url})
                continue
            ok += 1
            records.extend(recs)
            done.append((item.source, item.item_id))
        if rows:
            log.info("parsed %d items (%d failed)", ok, failed, extra={"source": src.slug, "segment": segment})

    groups: dict[str, list[EventRecord]] = {}
    for rec in records:
        if _keep(rec, ctx):
            groups.setdefault(rec.key(), []).append(rec)

    new: list[EventRecord] = []
    for key, recs in groups.items():
        if ctx.db.one("SELECT 1 FROM events WHERE event_key = ?", (key,)):
            continue
        merged = merge_records(recs, structured)
        slugs = sorted({r.source for r in recs})
        new.append(merged)
        if not ctx.dry_run:
            ctx.db.conn.execute(
                "INSERT OR IGNORE INTO events (event_key, segment, first_seen_week, data_json, sources_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, segment, ctx.week, ctx.db.dumps(asdict(merged)), ctx.db.dumps(slugs)),
            )
    if not ctx.dry_run:
        now = utcnow()
        ctx.db.conn.executemany("UPDATE raw_items SET processed_at = ? WHERE source = ? AND item_id = ?",
                                [(now, s, i) for s, i in done])
        ctx.db.conn.commit()
    log.info("discovered %d new events", len(new), extra={"segment": segment, "count": len(new)})
    return new
