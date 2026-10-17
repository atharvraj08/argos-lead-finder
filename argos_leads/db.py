"""SQLite: the source of truth. Committed back to the repo after each run.

Modules may add their own tables with `Database.ensure(sql)` (CREATE TABLE IF NOT EXISTS)
but must not change the tables defined here without updating this file.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (          -- everything collected from sources (daily + weekly)
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url TEXT,
    title TEXT,
    published TEXT,
    text TEXT,
    payload_json TEXT,
    collected_at TEXT NOT NULL,
    processed_at TEXT,                          -- set once discovery has turned it into events
    PRIMARY KEY (source, item_id)
);
CREATE TABLE IF NOT EXISTS events (             -- every event ever discovered (dedup against past runs)
    event_key TEXT PRIMARY KEY,
    segment TEXT NOT NULL,
    first_seen_week TEXT NOT NULL,
    data_json TEXT NOT NULL,                    -- merged EventRecord
    sources_json TEXT NOT NULL                  -- list of source slugs that reported it
);
CREATE TABLE IF NOT EXISTS leads (              -- one row per output lead (fixed columns + provenance)
    event_key TEXT PRIMARY KEY,
    week_found TEXT NOT NULL,
    data_json TEXT NOT NULL,                    -- full Lead as JSON
    excluded_reason TEXT,
    appended_to_sheet_at TEXT
);
CREATE TABLE IF NOT EXISTS artist_agency (      -- lookup: artist -> agency
    artist_norm TEXT PRIMARY KEY,
    artist TEXT, agency TEXT, source_url TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS venue_seller (       -- lookup: venue -> primary ticket seller
    venue_norm TEXT NOT NULL,
    city_norm TEXT NOT NULL,
    venue TEXT, city TEXT, seller TEXT, source_url TEXT, updated_at TEXT,
    PRIMARY KEY (venue_norm, city_norm)
);
CREATE TABLE IF NOT EXISTS competitor_cache (   -- per seller, rechecked monthly
    seller_norm TEXT PRIMARY KEY,
    seller TEXT, vendor TEXT, status TEXT, evidence_url TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS scalping_cache (     -- per artist / per seller
    kind TEXT NOT NULL,                         -- 'artist' | 'seller'
    key_norm TEXT NOT NULL,
    summary TEXT, url TEXT, checked_at TEXT,
    PRIMARY KEY (kind, key_norm)
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    status INTEGER, etag TEXT, last_modified TEXT, body TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                         -- 'weekly' | 'collect'
    started_at TEXT NOT NULL, finished_at TEXT, status TEXT, stats_json TEXT
);
CREATE TABLE IF NOT EXISTS source_log (         -- per-run per-source status (skipped/unusable sources)
    run_id INTEGER, source TEXT, status TEXT, items INTEGER, message TEXT, at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def ensure(self, sql: str) -> None:
        self.conn.executescript(sql)
        self.conn.commit()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        self.conn.close()

    # Small JSON helpers used by several modules.
    @staticmethod
    def dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def loads(s: str | None) -> Any:
        return json.loads(s) if s else None
