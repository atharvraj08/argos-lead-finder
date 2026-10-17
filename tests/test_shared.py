"""Shared modules: config, http, llm, models."""
from __future__ import annotations

import pytest
import requests
import responses
import yaml

from argos_leads.config import CONFIG_DIR, ScoringConfig, load_config
from argos_leads.discovery.sources import REGISTRY
from argos_leads.http import HttpClient, RobotsDisallowed, redact
from argos_leads.llm import LLM
from argos_leads.models import COLUMNS, EventRecord, Lead, norm
from pydantic import BaseModel

# ------------------------------------------------------------------ config


def test_real_config_loads_and_segments_have_sources():
    cfg = load_config(CONFIG_DIR)
    active = cfg.targets.active()
    assert set(active) == {"kpop", "jpop"}
    for name, seg in active.items():
        assert seg.sources, name
        slugs = [s.slug for s in seg.sources]
        assert len(slugs) == len(set(slugs)), name


def test_every_source_adapter_is_registered():
    cfg = load_config(CONFIG_DIR)
    for name, seg in cfg.targets.segments.items():
        for src in seg.sources:
            assert src.adapter in REGISTRY, f"{name}/{src.slug}: unknown adapter {src.adapter}"


def test_every_scoring_weight_present_in_targets_yaml():
    raw = yaml.safe_load((CONFIG_DIR / "targets.yaml").read_text())
    assert set(ScoringConfig.model_fields) <= set(raw["scoring"])
    cfg = load_config(CONFIG_DIR).targets.scoring
    for f in ("scalping_evidence", "on_sale_within_30_days", "per_intro_role_known", "large_venue"):
        assert isinstance(getattr(cfg, f), int) and getattr(cfg, f) > 0
    assert cfg.large_venue_keywords


def test_email_config_and_exclusions():
    cfg = load_config(CONFIG_DIR)
    assert cfg.email.recipients and all("@" in r for r in cfg.email.recipients)
    assert "Ticketmaster" in cfg.targets.exclude_sellers
    assert cfg.targets.exclude_if_competitor in ("confirmed", "likely", "never")


def test_active_segment_must_be_defined():
    from argos_leads.config import TargetsConfig
    with pytest.raises(ValueError, match="not defined"):
        TargetsConfig(active_segments=["kpop", "ghost"], segments={"kpop": {}}).active()


# ------------------------------------------------------------------ http.redact


@pytest.mark.parametrize("url,secret", [
    ("https://api.seatgeek.com/2/events?q=kpop&client_id=ABC123&per_page=10", "ABC123"),
    ("https://api.example.com/x?apikey=SECRET&x=1", "SECRET"),
    ("https://api.example.com/x?client_secret=S3&api_key=K9", "S3"),
])
def test_redact_hides_credentials(url, secret):
    out = redact(url)
    assert secret not in out and "***" in out
    assert out.startswith(url.split("?")[0])


def test_redact_keeps_other_params():
    assert redact("https://x.com/a?client_id=Z&q=kpop") == "https://x.com/a?client_id=***&q=kpop"


# ------------------------------------------------------------------ LLM


class Out(BaseModel):
    value: str = ""


def test_llm_without_credentials_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    llm = LLM()
    assert llm.available is False
    assert llm.extract(Out, "extract", "page text", url="https://x.com") is None
    assert llm.search(Out, "question") is None
    assert llm._client is None  # never built a client


# ------------------------------------------------------------------ HttpClient


def client(**kw):
    return HttpClient(db=None, default_min_interval_s=0, **kw)


@responses.activate
def test_robots_disallow_respected():
    responses.add(responses.GET, "https://site.example.com/robots.txt",
                  body="User-agent: *\nDisallow: /private\n")
    responses.add(responses.GET, "https://site.example.com/public", body="ok")
    c = client()
    with pytest.raises(RobotsDisallowed):
        c.get("https://site.example.com/private/page")
    assert c.get("https://site.example.com/public").text == "ok"
    fetched = [call.request.url for call in responses.calls]
    assert "https://site.example.com/private/page" not in fetched
    assert fetched.count("https://site.example.com/robots.txt") == 1  # robots cached per host


@responses.activate
def test_missing_robots_allows():
    responses.add(responses.GET, "https://nr.example.com/robots.txt", status=404)
    responses.add(responses.GET, "https://nr.example.com/a", body="A")
    assert client().get("https://nr.example.com/a").text == "A"


@responses.activate
def test_retries_503_then_succeeds():
    responses.add(responses.GET, "https://api.example.com/robots.txt", status=404)
    responses.add(responses.GET, "https://api.example.com/data", status=503, headers={"Retry-After": "0"})
    responses.add(responses.GET, "https://api.example.com/data", status=503, headers={"Retry-After": "0"})
    responses.add(responses.GET, "https://api.example.com/data", json={"ok": True})
    r = client().get("https://api.example.com/data")
    assert r.ok and r.json() == {"ok": True}
    assert sum(1 for c in responses.calls if c.request.url.endswith("/data")) == 3


@responses.activate
def test_gives_up_after_max_retries():
    responses.add(responses.GET, "https://api.example.com/robots.txt", status=404)
    responses.add(responses.GET, "https://api.example.com/down", status=503, headers={"Retry-After": "0"})
    with pytest.raises(requests.HTTPError):
        client(max_retries=1).get("https://api.example.com/down")


@responses.activate
def test_user_agent_has_contact_and_params_encoded():
    responses.add(responses.GET, "https://api.example.com/robots.txt", status=404)
    responses.add(responses.GET, "https://api.example.com/q", body="x")
    HttpClient(db=None, contact_email="qa@example.com", default_min_interval_s=0).get(
        "https://api.example.com/q", params={"q": "k pop", "client_id": "C"})
    req = responses.calls[-1].request
    assert "mailto:qa@example.com" in req.headers["User-Agent"]
    assert "q=k+pop" in req.url and "client_id=C" in req.url


@responses.activate
def test_conditional_get_uses_cache(tmp_path):
    from argos_leads.db import Database
    db = Database(tmp_path / "c.db")
    responses.add(responses.GET, "https://c.example.com/robots.txt", status=404)
    responses.add(responses.GET, "https://c.example.com/feed", body="v1", headers={"ETag": '"e1"'})
    responses.add(responses.GET, "https://c.example.com/feed", status=304)
    assert HttpClient(db=db, default_min_interval_s=0).get("https://c.example.com/feed").text == "v1"
    r = HttpClient(db=db, default_min_interval_s=0).get("https://c.example.com/feed")
    assert r.text == "v1" and r.from_cache
    assert responses.calls[-1].request.headers["If-None-Match"] == '"e1"'


# ------------------------------------------------------------------ models


def rec(artist, city, d):
    return EventRecord(source="s", segment="kpop", event="e", artist=artist, city=city, event_date=d)


def test_event_key_normalization():
    assert rec("Stray Kids", "Los Angeles", "2026-12-05").key() == "stray kids|los angeles|2026-12-05"
    assert rec("  STRAY-KIDS! ", "LOS  ANGELES", "2026-12-05").key() == rec("Stray Kids", "Los Angeles",
                                                                             "2026-12-05").key()
    assert rec("Beyoncé", "São Paulo", None).key() == "beyonce|sao paulo|tbd"
    assert rec("&TEAM", "New York", "2026-11-01").key() == "team|new york|2026-11-01"


def test_norm_edge_cases():
    assert norm(None) == "" and norm("") == ""
    assert norm("BE:FIRST") == "be first"


def test_lead_row_matches_columns():
    lead = Lead(event_key="k", week_found="2026-W43", segment="kpop", event="E", artist_or_headliner="A",
                score=5, sources="https://x")
    row = lead.row()
    assert len(row) == len(COLUMNS)
    assert dict(zip(COLUMNS, row))["score"] == 5
    assert "excluded_reason" not in COLUMNS and "field_sources" not in COLUMNS
