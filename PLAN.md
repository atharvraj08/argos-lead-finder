# Argos Lead Finder — Build Plan

Weekly GitHub Actions pipeline for ARGOS Identity that finds newly announced U.S.
K-pop / J-pop events, identifies the ticket seller (the lead), checks for competing
KYC vendors, finds scalping history, scores leads, appends them to a Google Sheet,
and emails a weekly summary.

## Status

| Phase | Item | Owner | Status |
|---|---|---|---|
| 1 | Source research → `SOURCES.md` | Research agent | Done (2026-10-17) |
| 1b | Expansion pass: fan events, conventions, promoters, agencies, venues (no cap; must be high quality, posting consistently, free with automated access allowed) | Research agent | Done (2026-10-17): +3 sources, Billboard flagged |
| 1 | User review of `SOURCES.md` | User | Approved 2026-10-17 (keep Billboard; add daily collection for shallow feeds) |
| 2 | Shared config (`config/targets.yaml`, `config/email.yaml`), package skeleton, shared models/DB schema | Orchestrator | Done |
| 2 | A: Event discovery (`discovery/`) | Agent A | Done (19 tests) |
| 2 | B: Enrichment (`enrich/`) | Agent B | Done (50 tests; not yet run against live pages) |
| 2 | C: Competitor detection (`competitors/`) | Agent C | Done (11 tests; not yet run against real sites) |
| 2 | D: Scalping history (`history/`) | Agent D | Done (19 tests) |
| 2 | E: Scoring (`scoring/`) | Agent E | Done (48 tests) |
| 2 | F: Output — SQLite, Sheets, email (`output/`) | Agent F | Done (22 tests; not yet run against real Sheet/email) |
| 2 | G: GitHub Actions workflow + secrets (`.github/workflows/`) | Agent G | Done (15 tests; not yet run on GitHub) |
| 2 | H: QA (`tests/`) | Agent H | Done (233 tests total, incl. e2e, CLI, report, shared) |
| 3 | Integration: `python -m argos_leads run [--segment] [--dry-run] [--send-test-email]` | Orchestrator | Done; live dry run without keys produced 67 leads |
| 3 | README (setup, service account, email, secrets, config, adding a segment) | Orchestrator | Done |
| 3 | Dry run, manual Actions run, email received, tests green | Orchestrator + User | Tests green (233); keyless dry run OK (67 leads); keyed dry run + Actions run need user's keys/repo |

## Module contracts (Phase 2)

Shared, owned by orchestrator (agents must not edit; request changes in their report):
`argos_leads/config.py`, `models.py` (COLUMNS, RawItem, EventRecord, Lead, norm), `db.py` (schema),
`http.py` (HttpClient: robots, rate limit, retry, cache), `llm.py` (LLM.extract / LLM.search),
`context.py` (RunContext: config, db, http, llm, today, week, dry_run, run_id).

CLI (Phase 3, orchestrator): `python -m argos_leads run [--segment S] [--dry-run] [--send-test-email]`,
`python -m argos_leads collect` (daily: shallow feeds only), `python -m argos_leads notify-failure --message TEXT`.

Weekly pipeline: `collect(all)` → per segment `discover` → `enrich` → `competitors.apply` → `history.apply`
→ `scoring.apply` → `output.store.save_leads` → `output.sheets.append_new` → `output.email.send_summary`.

| Module | Public API |
|---|---|
| discovery | `collect(ctx, segments=None, daily_only=False) -> dict[slug, int]`; `discover(ctx, segment) -> list[EventRecord]` (new U.S. events only, deduped across sources and past runs, stored in `events`) |
| enrich | `enrich(ctx, events) -> list[Lead]`; `sellers.seller_from_url(url) -> str`; `sellers.seller_homepage(name) -> str or None` |
| competitors | `check_seller(ctx, seller) -> CompetitorResult(vendor, status, evidence_url)`; `apply(ctx, leads) -> None` |
| history | `apply(ctx, leads) -> None` (this week's new leads only) |
| scoring | `score_lead(lead, scoring_cfg, today) -> (int, str)`; `apply(ctx, leads) -> None` (sets score, score_breakdown, excluded_reason) |
| output | `store.save_leads(ctx, leads)`; `sheets.append_new(ctx, leads) -> int`; `sheets.sheet_url(ctx) -> str`; `email.send_summary(ctx, leads, new_counts) -> None`; `email.send_failure(ctx, message) -> None`; `email.send_test(ctx) -> None` |

## Key decisions

- 2026-10-17 (user): exclude all Ticketmaster subsidiaries (Universe, TicketWeb, Front Gate Tickets) via `exclude_sellers`.
- 2026-10-17 (user): news sources read the full article page (`fetch_article: true` on every RSS/GDELT source), still respecting robots.txt; falls back to feed fields when blocked.
- 2026-10-17 (user): deliver as a template. Not run with real keys yet.
- 2026-10-20 (user): repo is public (github.com/atharvraj08/argos-lead-finder) to show the startup owner. Scheduled runs are off until repo variable `ARGOS_ENABLED=true`. Make private before real runs (committed DB holds leads).

- Ticket seller is always the lead; agency/promoter/organizer are optional intro paths, never guessed.
- Phase 2 agents use only the v1 sources in `SOURCES.md`; no new sources added during build.
- SQLite is the source of truth; committed back to the repo after each run (repo should be private once real runs start).
- Cron: Monday 08:00 America/New_York → `0 12 * * 1` UTC (EDT) / `0 13 * * 1` (EST). Plan: schedule both and gate on local hour in the job, so the run is always 8 AM ET.
- LLM (Anthropic API) used only to extract fields from fetched page text.

## Skipped sources

- `ticketmaster_discovery_kpop`, `ticketmaster_discovery_jpop`: removed after user review (2026-10-17). They return only Ticketmaster-family events, and Ticketmaster uses Persona. Ticketmaster goes in `exclude_sellers`.

## Known gaps

- Unknown ticket domains become the seller name at low confidence; this can be an artist's own site (e.g. acidmothers.com). The validation report flags these rows.
- GDELT rate-limits (429) after repeated runs; it's logged and the other sources continue.
- The email's per-segment "new events" count includes excluded events.
- Dry runs leave their temp DB/CSV in the system temp folder.
- No ANTHROPIC_API_KEY / SEATGEEK_CLIENT_ID locally: LLM extraction, scalping/competitor search and SeatGeek untested live.
- Competitor detection reads static HTML only (misses tag-manager/bundled scripts).
- Dedupe key artist+city+date: "New York" vs "Brooklyn" won't merge.
- Action versions `actions/checkout@v7`, `actions/setup-python@v7` need confirming before first GitHub run.

## Next steps

1. Add keys, run `python -m argos_leads run --dry-run` with LLM enabled.
2. Make repo private, add secrets, set `ARGOS_ENABLED=true`, manual Actions run → rows in Leads tab + email received.
