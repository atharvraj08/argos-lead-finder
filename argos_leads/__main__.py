"""Entry point: python -m argos_leads {run,collect,notify-failure}.

Only standard-library imports at module level, so `notify-failure` works even
when dependency installation was the step that failed."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("argos_leads")


def _dry_run_db(db_path: Path) -> Path:
    """Dry runs work on a throwaway copy so they never mark events as seen."""
    tmp = Path(tempfile.mkdtemp(prefix="argos-dry-run-")) / "argos.db"
    if db_path.exists():
        shutil.copy2(db_path, tmp)
    return tmp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argos_leads", description="Argos Lead Finder")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--db", default=None, help="SQLite path (default data/argos.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="weekly pipeline")
    run.add_argument("--segment", help="run one segment only (default: all active_segments)")
    run.add_argument("--dry-run", action="store_true",
                     help="no sheet writes, no email; uses a throwaway copy of the database")
    run.add_argument("--send-test-email", action="store_true", help="send a test email and exit")

    sub.add_parser("collect", help="daily: save new items from shallow feeds")

    fail = sub.add_parser("notify-failure", help="email a short failure notice")
    fail.add_argument("--message", required=True)

    args = parser.parse_args(argv)

    if args.command == "notify-failure":
        from .notify_failure import send

        send(args.message)
        return 0

    from . import pipeline
    from .config import DEFAULT_DB_PATH
    from .context import build_context, setup_logging
    from .output import email

    setup_logging(args.log_level)
    db_path = Path(args.db) if args.db else DEFAULT_DB_PATH

    if args.command == "run":
        if args.dry_run:
            db_path = _dry_run_db(db_path)
            log.info("dry run: using database copy at %s", db_path)
        ctx = build_context(db_path=db_path, dry_run=args.dry_run)
        if args.send_test_email:
            email.send_test(ctx)
            log.info("test email sent")
            return 0
        stats = pipeline.run_weekly(ctx, segment=args.segment)
        if args.dry_run:
            from .report import print_dry_run

            print_dry_run(ctx, stats)
    elif args.command == "collect":
        pipeline.collect_daily(build_context(db_path=db_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
