"""Run reports: the dry-run lead dump and the data-quality validation report."""
from __future__ import annotations

import csv
from pathlib import Path

from .context import RunContext
from .models import COLUMNS, Lead
from .output.store import lead_from_json


def week_leads(ctx: RunContext) -> list[Lead]:
    rows = ctx.db.query("SELECT data_json FROM leads WHERE week_found = ? ORDER BY event_key", (ctx.week,))
    return [lead_from_json(r["data_json"]) for r in rows]


def validation_report(leads: list[Lead]) -> str:
    """Rows missing a ticket seller, and rows with low-confidence fields."""
    kept = [lead for lead in leads if not lead.excluded_reason]
    missing = [lead for lead in kept if not lead.ticket_seller]
    low = [lead for lead in kept if lead.low_confidence]
    lines = [f"Validation: {len(kept)} leads kept, {len(leads) - len(kept)} excluded, "
             f"{len(missing)} missing ticket seller, {len(low)} with low-confidence fields"]
    for lead in missing:
        lines.append(f"  MISSING SELLER  {lead.artist_or_headliner} | {lead.city} | {lead.event_date} | {lead.sources}")
    for lead in low:
        lines.append(f"  LOW CONFIDENCE  {lead.artist_or_headliner} | {lead.city} | fields: {', '.join(lead.low_confidence)}")
    return "\n".join(lines)


def write_csv(leads: list[Lead], path: Path) -> Path:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS + ["excluded_reason"])
        for lead in leads:
            w.writerow(lead.row() + [lead.excluded_reason])
    return path


def print_dry_run(ctx: RunContext, stats: dict) -> None:
    from .output.email import build_summary

    leads = week_leads(ctx)
    out = write_csv(leads, Path(ctx.db.path).with_name("dry_run_leads.csv"))
    new_counts = {seg: s["new_events"] for seg, s in stats.get("segments", {}).items()}
    _, text, _ = build_summary(ctx, leads, new_counts)
    print("\n=== DRY RUN (nothing written to the sheet, no email sent) ===")
    print(f"Run stats: {ctx.db.dumps(stats)}")
    print(f"All {len(leads)} rows with the fixed columns: {out}\n")
    print(validation_report(leads))
    print("\n--- Email that would be sent ---")
    print(text)
