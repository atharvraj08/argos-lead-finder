"""RunContext: everything a pipeline stage needs, passed explicitly."""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .config import DEFAULT_DB_PATH, Config, load_config
from .db import Database
from .http import HttpClient
from .llm import LLM


@dataclass
class RunContext:
    config: Config
    db: Database
    http: HttpClient
    llm: LLM
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    dry_run: bool = False
    run_id: int | None = None

    @property
    def week(self) -> str:
        """ISO week label used in week_found, e.g. 2026-W42."""
        y, w, _ = self.today.isocalendar()
        return f"{y}-W{w:02d}"


def build_context(config_dir: Path | str | None = None, db_path: Path | str = DEFAULT_DB_PATH, dry_run: bool = False) -> RunContext:
    cfg = load_config(config_dir) if config_dir else load_config()
    db = Database(db_path)
    t = cfg.targets
    http = HttpClient(
        db=db,
        contact_email=t.http.contact_email,
        default_min_interval_s=t.http.default_min_interval_s,
        host_min_interval_s=t.http.host_min_interval_s,
    )
    return RunContext(config=cfg, db=db, http=http, llm=LLM(t.llm.model, t.llm.effort), dry_run=dry_run)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        out = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        for key in ("source", "segment", "event_key", "url", "count"):
            if hasattr(record, key):
                out[key] = getattr(record, key)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
