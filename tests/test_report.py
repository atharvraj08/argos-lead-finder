"""report.validation_report and report.write_csv."""
from __future__ import annotations

import csv
from datetime import date

from argos_leads import report
from argos_leads.config import Config, EmailConfig, TargetsConfig
from argos_leads.context import RunContext
from argos_leads.db import Database
from argos_leads.models import COLUMNS, Lead
from argos_leads.output import store


def mk(key, seller="AXS", low=None, excluded="", **kw):
    return Lead(event_key=key, week_found="2026-W43", segment="kpop", event=f"Tour {key}",
                artist_or_headliner=f"Artist {key}", city="Austin, TX", event_date="2026-12-01",
                ticket_seller=seller, low_confidence=list(low or []), excluded_reason=excluded,
                sources=f"https://src.example.com/{key}", **kw)


LEADS = [
    mk("ok"),
    mk("noseller", seller=""),
    mk("lowconf", low=["ticket_seller", "agency"], seller="thewarfield.com"),
    mk("excl_noseller", seller="", excluded="competitor confirmed: Persona"),
    mk("excl_low", seller="Ticketmaster", low=["agency"], excluded="excluded seller"),
]


def test_validation_counts_exclude_excluded_rows():
    out = report.validation_report(LEADS)
    first = out.splitlines()[0]
    assert first == ("Validation: 3 leads kept, 2 excluded, 1 missing ticket seller, "
                     "1 with low-confidence fields")


def test_validation_lists_missing_and_low_confidence_rows():
    lines = report.validation_report(LEADS).splitlines()[1:]
    missing = [l for l in lines if "MISSING SELLER" in l]
    low = [l for l in lines if "LOW CONFIDENCE" in l]
    assert len(missing) == 1 and "Artist noseller" in missing[0] and "https://src.example.com/noseller" in missing[0]
    assert len(low) == 1 and "Artist lowconf" in low[0] and "ticket_seller, agency" in low[0]
    assert not any("excl_" in l for l in lines)


def test_validation_empty():
    assert report.validation_report([]).startswith("Validation: 0 leads kept, 0 excluded")


def test_write_csv(tmp_path):
    path = report.write_csv(LEADS, tmp_path / "out.csv")
    with path.open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == COLUMNS + ["excluded_reason"]
    assert len(rows) == 1 + len(LEADS)
    assert all(len(r) == len(COLUMNS) + 1 for r in rows)
    by_key = {r[COLUMNS.index("event")]: r for r in rows[1:]}
    assert by_key["Tour excl_low"][-1] == "excluded seller"
    assert by_key["Tour ok"][-1] == ""
    assert by_key["Tour ok"][COLUMNS.index("ticket_seller")] == "AXS"


def test_week_leads_reads_only_this_week():
    cfg = Config(targets=TargetsConfig(active_segments=[], segments={}), email=EmailConfig(recipients=["a@x.com"]))
    ctx = RunContext(config=cfg, db=Database(":memory:"), http=None, llm=None, today=date(2026, 10, 19))
    old = mk("old")
    old.week_found = "2026-W40"
    store.save_leads(ctx, LEADS + [old])
    keys = [l.event_key for l in report.week_leads(ctx)]
    assert sorted(keys) == sorted(l.event_key for l in LEADS)
