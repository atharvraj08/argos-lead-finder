"""Persist leads in SQLite (source of truth) and track which rows reached the sheet.

Never deletes. Re-saving a lead updates its data but keeps week_found and
appended_to_sheet_at from the first save.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Iterable

from ..db import Database, utcnow
from ..models import Lead

log = logging.getLogger(__name__)

_LEAD_FIELDS = {f.name for f in dataclasses.fields(Lead)}


def lead_to_json(lead: Lead) -> str:
    return Database.dumps(dataclasses.asdict(lead))


def lead_from_json(data_json: str) -> Lead:
    d = json.loads(data_json)
    return Lead(**{k: v for k, v in d.items() if k in _LEAD_FIELDS})


def save_leads(ctx, leads: Iterable[Lead]) -> int:
    """Upsert leads by event_key. Returns the number of leads written."""
    n = 0
    for lead in leads:
        ctx.db.conn.execute(
            """
            INSERT INTO leads (event_key, week_found, data_json, excluded_reason, appended_to_sheet_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(event_key) DO UPDATE SET
                data_json = excluded.data_json,
                excluded_reason = excluded.excluded_reason
            """,
            (lead.event_key, lead.week_found, lead_to_json(lead), lead.excluded_reason or None),
        )
        n += 1
    ctx.db.conn.commit()
    log.info("saved leads", extra={"count": n})
    return n


def pending_sheet_rows(ctx) -> list[Lead]:
    """Non-excluded leads that have not been appended to the sheet yet (oldest first)."""
    rows = ctx.db.query(
        "SELECT data_json FROM leads WHERE appended_to_sheet_at IS NULL "
        "AND (excluded_reason IS NULL OR excluded_reason = '') ORDER BY week_found, event_key"
    )
    return [lead_from_json(r["data_json"]) for r in rows]


def appended_keys(ctx, keys: Iterable[str]) -> set[str]:
    """Subset of `keys` already marked as appended in the DB."""
    keys = list(keys)
    out: set[str] = set()
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        q = ",".join("?" * len(chunk))
        out.update(r["event_key"] for r in ctx.db.query(
            f"SELECT event_key FROM leads WHERE appended_to_sheet_at IS NOT NULL AND event_key IN ({q})", chunk))
    return out


def mark_appended(ctx, keys: Iterable[str]) -> None:
    now = utcnow()
    ctx.db.conn.executemany(
        "UPDATE leads SET appended_to_sheet_at = ? WHERE event_key = ? AND appended_to_sheet_at IS NULL",
        [(now, k) for k in keys],
    )
    ctx.db.conn.commit()
