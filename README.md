# Argos Lead Finder

A weekly pipeline for ARGOS Identity. It finds newly announced U.S. K-pop and J-pop
events, identifies each event's **ticket seller** (the lead), checks whether that seller
already uses a competing ID-verification vendor, looks for past scalping/bot incidents,
scores the lead, appends it to a Google Sheet, and emails a weekly summary.

The tool does not research or adapt. Every run executes the same fixed steps over the
sources listed in `config/targets.yaml` (chosen in `SOURCES.md`).

```
collect sources ─► discover new U.S. events ─► enrich (seller, venue, dates, agency/promoter/organizer)
   ─► competitor check ─► scalping history ─► score ─► SQLite ─► Google Sheet "Leads" ─► email
```

- **SQLite (`data/argos.db`) is the source of truth.** GitHub Actions commits it back after
  every run, so lookup tables, caches and history persist.
- **Every data point keeps its source URL** (`sources` column; per-field provenance in the DB).
- Agency / promoter / organizer are filled only when a page states them. Never guessed.

## Commands

```bash
python -m argos_leads run                      # full weekly run (all active segments)
python -m argos_leads run --segment kpop       # one segment
python -m argos_leads run --dry-run            # no sheet writes, no email; uses a throwaway DB copy
                                               # and prints the leads, a validation report and the email
python -m argos_leads run --send-test-email    # send a test email and exit
python -m argos_leads collect                  # daily: save new items from shallow feeds
python -m argos_leads notify-failure --message "..."   # failure notice (standard library only)
```

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # fill in values, then: set -a; source .env; set +a
.venv/bin/python -m pytest -q
.venv/bin/python -m argos_leads run --dry-run
```

Without `ANTHROPIC_API_KEY` the run still works, but news sources yield nothing and
enrichment can only use structured data (the run logs a warning). Without
`SEATGEEK_CLIENT_ID` the SeatGeek sources are skipped.

## Environment variables / GitHub Secrets

Add these in GitHub under **Settings → Secrets and variables → Actions**. For local runs,
put them in `.env` (see `.env.example`).

| Secret | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Extracting fields from page text, scalping and competitor searches |
| `SEATGEEK_CLIENT_ID` | SeatGeek event sources (free: seatgeek.com/account/develop) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Sheets: the full JSON key of the service account (locally you can use `GOOGLE_APPLICATION_CREDENTIALS=/path/key.json` instead) |
| `GOOGLE_SHEET_ID` | The spreadsheet id: the long string in the sheet URL between `/d/` and `/edit` |
| `GMAIL_SENDER`, `GMAIL_APP_PASSWORD` | Email via Gmail SMTP |
| `RESEND_API_KEY`, `RESEND_FROM` | Email via Resend (alternative to Gmail) |

Only the pair for the email method chosen in `config/email.yaml` is required.

## Google service account (one time)

1. In Google Cloud Console, create (or pick) a project.
2. **APIs & Services → Library**: enable **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create service account** (no roles needed).
4. Open the account → **Keys → Add key → Create new key → JSON**. Download it.
5. Create a Google Sheet. Click **Share** and add the service account's email
   (`...@...iam.gserviceaccount.com`) as **Editor**.
6. Put the whole JSON file content in the `GOOGLE_SERVICE_ACCOUNT_JSON` secret and the
   sheet id in `GOOGLE_SHEET_ID`.

The first run creates two tabs:
- **Leads**: new rows are appended every run with the fixed columns. Past rows are never
  changed or deleted, and events already present are skipped.
- **Outreach**: created once with one row per ticket seller (seller, contact path, date
  contacted, contacted by, status, notes). The tool never writes to it again; your team
  maintains it.

## Email setup

`config/email.yaml`:

```yaml
recipients: [atharvra@andrew.cmu.edu]
method: gmail_smtp          # gmail_smtp | resend
```

**Gmail SMTP:** use a sender Gmail account with 2-Step Verification on. Create an app
password at myaccount.google.com/apppasswords. Set `GMAIL_SENDER` (the address) and
`GMAIL_APP_PASSWORD` (the 16-character password).

**Resend:** create an API key at resend.com and verify a sending domain. Set
`RESEND_API_KEY` and `RESEND_FROM` (e.g. `Argos Leads <leads@yourdomain.com>`), then set
`method: resend`.

The weekly email contains the new-event count per segment, the top 3 leads per segment
(by score, ties broken by soonest on-sale date) with a one-line "why now", and a link to
the sheet. A failed run sends a short failure notice to the same recipients.

## GitHub Actions

- `.github/workflows/weekly.yml`: Monday 8:00 AM America/New_York. Cron is UTC-only, so
  it is scheduled at both 12:00 and 13:00 UTC and a gate step runs only the one that
  matches New York's current daylight-saving offset. Can also be started manually
  (**Actions → Weekly lead run → Run workflow**) with `segment`, `dry_run` and
  `send_test_email` inputs.
- `.github/workflows/collect.yml`: daily, saves new posts from feeds that only keep a day
  or two of items (Pollstar, Anime News Network), so the weekly run misses nothing.

One-time GitHub setup:
1. Push this folder to a GitHub repo. Public is fine while it's a template (there are no
   secrets in the code), but see the note below before running it for real.
2. Add the secrets above.
3. **Settings → Actions → General → Workflow permissions → Read and write permissions**
   (lets the workflow commit `data/argos.db`). If the default branch is protected, allow
   `github-actions[bot]` to push.
4. Run **Weekly lead run** manually once with `dry_run` checked, then once without.
5. Turn on the schedules: **Settings → Secrets and variables → Actions → Variables → New
   repository variable** `ARGOS_ENABLED` = `true`. Until then, scheduled runs are skipped
   (manual runs always work).

**Public vs private:** the workflows commit `data/argos.db` (all leads, competitor findings
and scalping notes) back to the repo. In a public repo anyone can download it. Make the repo
private before real runs (**Settings → General → Danger Zone → Change visibility**), or share
it with specific people as collaborators instead.

## Editing `config/targets.yaml`

Everything is editable without touching code:

- `active_segments`: which segments run.
- `segments.<name>.keywords`, `seed_agencies` (search hints only), `artist_watchlist`,
  and `sources` (each with an `adapter` and its settings, e.g. an RSS `url` and
  `include_terms` filter).
- `competitor_vendors`: vendors to detect.
- `exclude_if_competitor`: `confirmed` (drop sellers with confirmed competitor use),
  `likely` (also drop likely), or `never`.
- `exclude_sellers`: sellers ARGOS already works with or has ruled out (currently Ticketmaster
  and its subsidiaries Universe, TicketWeb and Front Gate Tickets).
- `fetch_article: true` on a news source: read the full article page (robots.txt permitting)
  instead of only the feed headline and excerpt. On by default for every RSS and GDELT source.
- `scoring`: the point values.

```yaml
scoring:
  scalping_evidence: 3          # past scalping/bot/resale incident found
  on_sale_within_30_days: 2     # ON-SALE date (not event date) within 30 days
  per_intro_role_known: 1       # each of agency, promoter, organizer that is known
  large_venue: 1                # venue name contains a word below
  large_venue_keywords: [Arena, Stadium, Coliseum, Dome, Garden, Forum, Field, Bowl]
```

Set a weight to 0 to turn a signal off. Blank roles are never penalized. The
`score_breakdown` column shows how each score was reached, e.g.
`scalping +3, on-sale soon +2, agency +1 = 6`.

## Adding a new segment

1. Run the Research agent alone for the new segment (Phase 1 only) and append its results
   to `SOURCES.md`.
2. Add the segment block to `targets.yaml` (see the commented `anime_conventions` example)
   and list it in `active_segments`.
3. Write adapters only for source types not already supported. Supported adapter types:
   `rss`, `gdelt`, `seatgeek`, `wp_events` (WordPress event REST API), `tokyonoizu`
   (single tour page). New adapters go in `argos_leads/discovery/sources/` and register
   themselves with the adapter registry.

## Project layout

```
config/            targets.yaml, email.yaml
argos_leads/
  discovery/       source adapters (sources/), collect + discover
  enrich/          seller / venue / dates / agency / promoter / organizer
  competitors/     KYC vendor detection
  history/         scalping evidence
  scoring/         additive points
  output/          SQLite store, Google Sheets, email
  pipeline.py      the fixed weekly pipeline
data/argos.db      SQLite database (committed by the workflow)
tests/             unit, adapter, scoring, end-to-end tests
SOURCES.md         verified sources and why each was chosen or excluded
PLAN.md            build plan, status, skipped sources, known gaps, next steps
```
