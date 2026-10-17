"""Append new leads to the Google Sheet (append-only) via a gspread service account.

Credentials (env only): GOOGLE_SERVICE_ACCOUNT_JSON (JSON content) or
GOOGLE_APPLICATION_CREDENTIALS (path to the JSON key file).
Sheet id: env var named by config sheet.spreadsheet_id_env (default GOOGLE_SHEET_ID).

Safety rules: the tool never clears, updates or deletes existing cells. It only
creates missing tabs and appends rows. The Outreach tab is created once and never
written again.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Iterable

from ..models import COLUMNS, Lead, norm
from . import store

log = logging.getLogger(__name__)

OUTREACH_HEADER = ["seller", "contact path", "date contacted", "contacted by", "status", "notes"]
BATCH_SIZE = 500
MAX_RETRIES = 5
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Test hook: replaced in tests to avoid real sleeps.
_sleep: Callable[[float], None] = time.sleep


class SheetsConfigError(RuntimeError):
    """Missing credentials or sheet id."""


class SheetHeaderError(RuntimeError):
    """The Leads tab header does not match COLUMNS; writing would misalign data."""


# ---------------------------------------------------------------- client / ids

def spreadsheet_id(ctx) -> str:
    env_name = ctx.config.targets.sheet.spreadsheet_id_env
    sid = os.environ.get(env_name, "").strip()
    if not sid:
        raise SheetsConfigError(f"Environment variable {env_name} (Google Sheet id) is not set.")
    return sid


def sheet_url(ctx) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id(ctx)}"


def get_client():
    """Authorized gspread client from env credentials."""
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SheetsConfigError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {e}") from e
        return gspread.service_account_from_dict(info, scopes=SCOPES)
    path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if path:
        if not os.path.exists(path):
            raise SheetsConfigError(f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {path}")
        return gspread.service_account(filename=path, scopes=SCOPES)
    raise SheetsConfigError(
        "No Google credentials: set GOOGLE_SERVICE_ACCOUNT_JSON (key JSON content) "
        "or GOOGLE_APPLICATION_CREDENTIALS (path to key file).")


# ---------------------------------------------------------------- retry

def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _with_retry(fn: Callable[[], Any], what: str) -> Any:
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:  # gspread.exceptions.APIError or requests errors
            code = _status_of(e)
            retryable = code == 429 or (code is not None and 500 <= code < 600)
            if not retryable or attempt == MAX_RETRIES:
                raise
            log.warning("sheets %s failed with %s; retry %d/%d in %.0fs", what, code, attempt, MAX_RETRIES - 1, delay)
            _sleep(delay)
            delay = min(delay * 2, 60.0)


# ---------------------------------------------------------------- helpers

def sheet_key(artist: str, city: str, event_date: str) -> tuple[str, str, str]:
    return (norm(artist), norm(city), (event_date or "").strip())


def _lead_key(lead: Lead) -> tuple[str, str, str]:
    return sheet_key(lead.artist_or_headliner, lead.city, lead.event_date)


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (int, float, str)):
        return v
    return str(v)


def _titles(spreadsheet) -> list[str]:
    return [ws.title for ws in _with_retry(spreadsheet.worksheets, "list tabs")]


def _existing_keys(values: list[list[str]]) -> set[tuple[str, str, str]]:
    header = values[0]
    ia, ic, idt = (header.index(c) for c in ("artist_or_headliner", "city", "event_date"))
    keys = set()
    for row in values[1:]:
        get = lambda i: row[i] if i < len(row) else ""  # noqa: E731
        if any(cell.strip() for cell in row):
            keys.add(sheet_key(get(ia), get(ic), get(idt)))
    return keys


def _check_header(header: list[str], tab: str) -> None:
    trimmed = list(header)
    while trimmed and not str(trimmed[-1]).strip():
        trimmed.pop()
    if [str(h).strip() for h in trimmed] != COLUMNS:
        raise SheetHeaderError(
            f"'{tab}' tab header does not match the expected columns; refusing to write.\n"
            f"  expected: {COLUMNS}\n  found:    {trimmed}\n"
            "Fix the header row in the sheet (or point to a new tab) and re-run.")


def _candidates(ctx, leads: Iterable[Lead], include_pending: bool) -> list[Lead]:
    seen: dict[str, Lead] = {}
    for lead in list(leads) + (store.pending_sheet_rows(ctx) if include_pending else []):
        if lead.excluded_reason or lead.event_key in seen:
            continue
        seen[lead.event_key] = lead
    done = store.appended_keys(ctx, seen.keys())
    return [l for k, l in seen.items() if k not in done]


# ---------------------------------------------------------------- public API

def append_new(ctx, leads: Iterable[Lead], client=None, include_pending: bool = True) -> int:
    """Append non-excluded leads not yet in the sheet. Returns rows appended.

    `include_pending` also retries leads saved earlier whose append never happened
    (e.g. a previous run failed at the sheet step).
    """
    cfg = ctx.config.targets.sheet
    todo = _candidates(ctx, leads, include_pending)

    if ctx.dry_run:
        log.info("[dry-run] would append up to %d rows to '%s' (no sheet access)", len(todo), cfg.leads_tab,
                 extra={"count": len(todo)})
        for lead in todo:
            log.info("[dry-run] row: %s | %s | %s | %s", lead.ticket_seller, lead.artist_or_headliner,
                     lead.city, lead.event_date, extra={"event_key": lead.event_key})
        return 0

    sid = spreadsheet_id(ctx)
    client = client or get_client()
    sh = _with_retry(lambda: client.open_by_key(sid), "open spreadsheet")
    titles = _titles(sh)

    # Leads tab: create if missing, verify header, read existing keys.
    if cfg.leads_tab not in titles:
        ws = _with_retry(lambda: sh.add_worksheet(title=cfg.leads_tab, rows=1000, cols=len(COLUMNS)), "create Leads")
        _with_retry(lambda: ws.append_rows([COLUMNS], value_input_option="RAW", insert_data_option="INSERT_ROWS"),
                    "write Leads header")
        existing: set = set()
        log.info("created '%s' tab", cfg.leads_tab)
    else:
        ws = _with_retry(lambda: sh.worksheet(cfg.leads_tab), "open Leads")
        values = _with_retry(ws.get_all_values, "read Leads")
        if not values or not any(str(c).strip() for c in values[0]):
            if values and any(any(str(c).strip() for c in r) for r in values):
                raise SheetHeaderError(f"'{cfg.leads_tab}' tab has data but an empty first row; refusing to write.")
            _with_retry(lambda: ws.append_rows([COLUMNS], value_input_option="RAW",
                                               insert_data_option="INSERT_ROWS"), "write Leads header")
            existing = set()
        else:
            _check_header(values[0], cfg.leads_tab)
            existing = _existing_keys(values)

    new: list[Lead] = []
    already: list[str] = []
    batch_keys: set = set()
    for lead in todo:
        k = _lead_key(lead)
        if k in existing:
            already.append(lead.event_key)  # someone/something already put it there
        elif k not in batch_keys:
            batch_keys.add(k)
            new.append(lead)
    if already:
        store.mark_appended(ctx, already)

    # Outreach tab: created once, seeded with this run's sellers, never touched again.
    if cfg.outreach_tab not in titles:
        sellers = sorted({l.ticket_seller.strip() for l in new if l.ticket_seller.strip()}, key=str.lower)
        rows = [OUTREACH_HEADER] + [[s, "", "", "", "", ""] for s in sellers]
        ows = _with_retry(lambda: sh.add_worksheet(title=cfg.outreach_tab, rows=max(100, len(rows) + 50),
                                                   cols=len(OUTREACH_HEADER)), "create Outreach")
        _with_retry(lambda: ows.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS"),
                    "seed Outreach")
        log.info("created '%s' tab with %d sellers", cfg.outreach_tab, len(sellers))

    appended = 0
    for i in range(0, len(new), BATCH_SIZE):
        chunk = new[i:i + BATCH_SIZE]
        rows = [[_cell(v) for v in l.row()] for l in chunk]
        _with_retry(lambda: ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS"),
                    "append rows")
        store.mark_appended(ctx, [l.event_key for l in chunk])
        appended += len(chunk)
    log.info("appended rows to '%s'", cfg.leads_tab, extra={"count": appended})
    return appended
