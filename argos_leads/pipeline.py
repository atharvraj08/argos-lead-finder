"""The fixed weekly pipeline and the daily collector. No research, no adaptation:
the same steps over the sources listed in config/targets.yaml every run."""
from __future__ import annotations

import logging

from . import competitors, discovery, enrich, history, scoring
from .context import RunContext
from .db import utcnow
from .models import Lead
from .output import email, sheets, store

log = logging.getLogger(__name__)


def _start_run(ctx: RunContext, kind: str) -> None:
    cur = ctx.db.execute("INSERT INTO runs (kind, started_at, status) VALUES (?, ?, 'running')", (kind, utcnow()))
    ctx.run_id = cur.lastrowid


def _finish_run(ctx: RunContext, status: str, stats: dict) -> None:
    ctx.db.execute(
        "UPDATE runs SET finished_at = ?, status = ?, stats_json = ? WHERE id = ?",
        (utcnow(), status, ctx.db.dumps(stats), ctx.run_id),
    )


def collect_daily(ctx: RunContext) -> dict[str, int]:
    """Daily job: save new items from shallow feeds so the weekly run misses nothing."""
    _start_run(ctx, "collect")
    try:
        counts = discovery.collect(ctx, daily_only=True)
    except Exception:
        _finish_run(ctx, "failed", {})
        raise
    _finish_run(ctx, "ok", {"collected": counts})
    log.info("daily collect done", extra={"count": sum(counts.values())})
    return counts


def run_weekly(ctx: RunContext, segment: str | None = None) -> dict:
    """Weekly job. Returns run stats. Raises on unrecoverable errors (the workflow emails a notice)."""
    _start_run(ctx, "weekly")
    targets = ctx.config.targets
    segments = [segment] if segment else list(targets.active())
    unknown = [s for s in segments if s not in targets.segments]
    if unknown:
        raise ValueError(f"unknown segment(s): {unknown}")

    stats: dict = {"segments": {}, "dry_run": ctx.dry_run}
    try:
        stats["collected"] = discovery.collect(ctx, segments=segments)
        all_leads: list[Lead] = []
        new_counts: dict[str, int] = {}
        for seg in segments:
            events = discovery.discover(ctx, seg)
            log.info("discovered new events", extra={"segment": seg, "count": len(events)})
            leads = enrich.enrich(ctx, events)
            competitors.apply(ctx, leads)
            # Don't spend scalping searches on rows the exclude rules will drop anyway.
            history.apply(ctx, [lead for lead in leads if not scoring.excluded_reason(lead, targets)])
            scoring.apply(ctx, leads)
            new_counts[seg] = len(events)
            stats["segments"][seg] = {
                "new_events": len(events),
                "leads": len(leads),
                "excluded": sum(1 for lead in leads if lead.excluded_reason),
                "missing_seller": sum(1 for lead in leads if not lead.ticket_seller),
            }
            all_leads.extend(leads)

        store.save_leads(ctx, all_leads)
        stats["appended"] = sheets.append_new(ctx, all_leads)
        email.send_summary(ctx, all_leads, new_counts)
    except Exception as e:
        stats["error"] = repr(e)
        _finish_run(ctx, "failed", stats)
        raise
    _finish_run(ctx, "ok", stats)
    log.info("weekly run done: %s", ctx.db.dumps(stats))
    return stats
