# Argos Lead Finder Overview

As of 2026-10-17

Every Monday, Argos Lead Finder finds newly announced U.S. K-pop and J-pop events, names the
ticket seller for each one as a sales lead for ARGOS, and adds the scored leads to a Google
Sheet. It is a working template: built and tested, not yet running with real keys.

## What it does

The tool runs the same six steps every week over a fixed list of 15 vetted sources. It does
not research new sources or change its own rules.

1. **Find new events.** It reads fan tour trackers, music news sites, SeatGeek and a news
   search. Each source was checked to confirm it allows automated access. News sources are
   read in full, not just their headlines, so show dates in articles are caught.
2. **Find the ticket seller for each city.** One tour can use different sellers in different
   cities. The seller is always the lead. It also records the agency, promoter or organizer,
   but only when a page names them; these are possible warm introductions.
3. **Check for competitors.** It looks for signs that the seller already uses Persona,
   Onfido, Jumio or another ID-verification vendor: code on the seller's website, their
   privacy policy, and press releases. Each seller is marked confirmed, likely or unknown,
   with a link to the evidence.
4. **Look for scalping history.** It searches for past scalping, bot or resale-price
   problems for the artist or seller, and saves a one-line summary with its link.
5. **Score the lead** with the points system below.
6. **Add new rows to the Google Sheet** and email a summary.

Every value in the sheet keeps the link it came from. Nothing is guessed: a field the tool
can't find stays blank.

## How scoring works

Each lead starts at 0 and earns points for signals that the seller needs ID verification
now. A higher score means a better lead this week.

| Signal | Points | Why it matters |
| --- | --- | --- |
| Past scalping or bot problems found | +3 | The seller has the problem ARGOS solves |
| Tickets go on sale within 30 days | +2 | They need a fix soon. Uses the on-sale date, not the concert date |
| Agency known | +1 | A possible warm introduction |
| Promoter known | +1 | A possible warm introduction |
| Organizer known | +1 | A possible warm introduction |
| Arena or stadium venue | +1 | Bigger shows draw more scalpers |

- **Missing information never costs points.** A blank agency simply adds nothing.
- **Some leads are dropped instead of scored:** sellers confirmed to use a competitor, and
  sellers on the exclude list. The list holds Ticketmaster and its subsidiaries Universe,
  TicketWeb and Front Gate Tickets, because Ticketmaster uses Persona.
- **Every row explains its score**, for example "scalping +3, on-sale soon +2, agency +1 = 6".
- **Anyone can change the points** in the settings file `config/targets.yaml`, without
  touching code. Setting a value to 0 turns that signal off. The same file holds the exclude
  list and the competitor list.

## What the team sees

The team works from one Google Sheet with two tabs, plus a Monday email.

| Output | What's in it | Who writes to it |
| --- | --- | --- |
| **Leads tab** | One row per event date, with 20 fixed columns: event, artist, city, venue, dates, ticket seller, agency, promoter, organizer, competitor status with evidence link, scalping summary with link, score, score breakdown, sources | The tool adds new rows each week. It never changes or deletes old rows, and never adds the same event twice |
| **Outreach tab** | One row per ticket seller: seller, contact path, date contacted, contacted by, status, notes | The tool creates it once. After that only the team edits it |
| **Monday email** | New-event count per genre, the top 3 leads per genre with a one-line "why now", and a link to the sheet | The tool sends it to the addresses in `config/email.yaml` |

Ties between leads with the same score go to the one whose tickets go on sale soonest.

## How it runs on GitHub

The tool runs on GitHub Actions inside a private GitHub repo, so no server or always-on
computer is needed.

- **Weekly run:** every Monday at 8 AM Eastern, adjusting for daylight saving time. It runs
  the full six steps.
- **Daily run:** a few seconds each day to save posts from two news feeds (Pollstar and
  Anime News Network) that delete old posts within a day or two.
- **Memory between runs:** after each run, GitHub saves the tool's database back into the
  repo. That's how it remembers which events it has already seen and reuses what it has
  already looked up, such as competitor checks for 30 days and scalping results for 90 days.
- **Failures:** if a run fails, a short failure email goes to the same recipients.
- **Manual runs:** anyone with repo access can start a run from the Actions tab, including a
  dry run that writes nothing to the sheet and sends no email.

Setup takes about 30 minutes, and the README has step-by-step instructions:

1. Push the project folder to a private GitHub repo.
2. Add six secrets in the repo settings: Anthropic API key, SeatGeek client ID, Google
   service account key, Google Sheet ID, Gmail sender address, Gmail app password.
3. In Settings, then Actions, then General, set Workflow permissions to "Read and write" so
   the tool can save its database.
4. Start "Weekly lead run" once by hand to check it works.

## Cost and limits

The only paid part is the Anthropic API, which reads articles and runs the scalping and
competitor searches. Every event source is free, and GitHub Actions is free for this amount
of use on most plans.

What it can miss:

- **Some sellers will be blank or uncertain.** This happens when a show has no ticket link
  yet, or the link points to the band's own website. Uncertain sellers are flagged in the
  run's validation report.
- **The competitor check can miss vendors** that load only after a page opens or sit behind
  a login. Those sellers show as "unknown", not as competitor-free.
- **Fan meetings and conventions have the thinnest coverage.** Few free sources post them
  consistently.
- **The same show can appear twice** if two sources name the city differently, for example
  New York and Brooklyn.
