"""Shared polite HTTP client: robots.txt, per-host rate limits, retries with backoff,
conditional GET caching in SQLite. Every module fetches through this client."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib import robotparser
from urllib.parse import urlencode, urlsplit

import requests

from .db import Database, utcnow

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
SECRET_PARAM = re.compile(r"((?:client_id|client_secret|apikey|api_key|key|token)=)[^&]+", re.I)


def redact(url: str) -> str:
    """Strip credentials from a URL before logging it."""
    return SECRET_PARAM.sub(r"\1***", url)


class RobotsDisallowed(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    from_cache: bool = False

    def json(self) -> Any:
        import json

        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpClient:
    def __init__(
        self,
        db: Database | None = None,
        contact_email: str = "",
        default_min_interval_s: float = 1.0,
        host_min_interval_s: dict[str, float] | None = None,
        max_retries: int = 4,
        timeout_s: float = 30.0,
        check_robots: bool = True,
        session: requests.Session | None = None,
    ):
        self.db = db
        self.user_agent = "ArgosLeadFinder/1.0" + (f" (+mailto:{contact_email})" if contact_email else "")
        self.default_min_interval_s = default_min_interval_s
        self.host_min_interval_s = host_min_interval_s or {}
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.check_robots = check_robots
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._memo: dict[str, Response] = {}  # same URL within one run -> one request

    # --- politeness -------------------------------------------------------
    def _wait_for_host(self, host: str) -> None:
        interval = self.host_min_interval_s.get(host, self.default_min_interval_s)
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.check_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout_s)
                if r.status_code >= 400:
                    rp = None  # no robots.txt -> allowed
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # --- fetching ---------------------------------------------------------
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        respect_robots: bool = True,
    ) -> Response:
        """GET with robots check, rate limit, retries, and ETag/Last-Modified revalidation.

        Raises RobotsDisallowed or requests.HTTPError (after retries) on failure.
        API endpoints with keys in params should pass respect_robots=False only if the
        API's own terms grant access (documented in SOURCES.md)."""
        full = url + ("?" + urlencode(params, doseq=True) if params else "")
        if full in self._memo:
            return self._memo[full]
        if respect_robots and not self.allowed(full):
            raise RobotsDisallowed(redact(full))

        hdrs = dict(headers or {})
        cached = None
        if use_cache and self.db is not None:
            cached = self.db.one("SELECT * FROM http_cache WHERE url = ?", (full,))
            if cached:
                if cached["etag"]:
                    hdrs["If-None-Match"] = cached["etag"]
                if cached["last_modified"]:
                    hdrs["If-Modified-Since"] = cached["last_modified"]

        host = urlsplit(full).netloc
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            self._wait_for_host(host)
            try:
                r = self.session.get(full, headers=hdrs, timeout=self.timeout_s)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                log.warning("http retry %s after %s", redact(full), e)
                time.sleep(delay)
                delay *= 2
                continue
            if r.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = float(r.headers.get("Retry-After", delay)) if r.headers.get("Retry-After", "").isdigit() else delay
                log.warning("http %s on %s; retrying in %.0fs", r.status_code, redact(full), wait)
                time.sleep(wait)
                delay *= 2
                continue
            break

        if r.status_code == 304 and cached:
            resp = Response(full, 200, cached["body"], from_cache=True)
        else:
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} error for {redact(full)}", response=r)
            resp = Response(full, r.status_code, r.text)
            if use_cache and self.db is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO http_cache (url, status, etag, last_modified, body, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (full, r.status_code, r.headers.get("ETag"), r.headers.get("Last-Modified"), r.text, utcnow()),
                )
        self._memo[full] = resp
        return resp
