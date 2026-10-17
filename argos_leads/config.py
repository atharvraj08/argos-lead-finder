"""Load config/targets.yaml and config/email.yaml into typed objects."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
DEFAULT_DB_PATH = DATA_DIR / "argos.db"


class SourceConfig(BaseModel):
    """One entry under segments.<name>.sources. Adapter-specific keys land in `extra`."""

    model_config = ConfigDict(extra="allow")
    slug: str
    adapter: str
    daily: bool = False  # shallow feed: collect every day so the weekly run misses nothing

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class SegmentConfig(BaseModel):
    keywords: list[str] = []
    seed_agencies: list[str] = []
    artist_watchlist: list[str] = []
    sources: list[SourceConfig] = []


class ScoringConfig(BaseModel):
    scalping_evidence: int = 3
    on_sale_within_30_days: int = 2
    per_intro_role_known: int = 1
    large_venue: int = 1
    large_venue_keywords: list[str] = ["Arena", "Stadium"]


class LLMConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: str = "low"


class HttpConfig(BaseModel):
    contact_email: str = ""
    default_min_interval_s: float = 1.0
    host_min_interval_s: dict[str, float] = {}


class CachingConfig(BaseModel):
    competitor_recheck_days: int = 30
    scalping_recheck_days: int = 90
    lookup_stale_days: int = 180


class SheetConfig(BaseModel):
    spreadsheet_id_env: str = "GOOGLE_SHEET_ID"
    leads_tab: str = "Leads"
    outreach_tab: str = "Outreach"


class TargetsConfig(BaseModel):
    active_segments: list[str]
    region: str = "US"
    segments: dict[str, SegmentConfig]
    competitor_vendors: list[str] = []
    exclude_if_competitor: Literal["confirmed", "likely", "never"] = "confirmed"
    exclude_sellers: list[str] = []
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    caching: CachingConfig = Field(default_factory=CachingConfig)
    sheet: SheetConfig = Field(default_factory=SheetConfig)

    def active(self) -> dict[str, SegmentConfig]:
        missing = [s for s in self.active_segments if s not in self.segments]
        if missing:
            raise ValueError(f"active_segments not defined under segments: {missing}")
        return {name: self.segments[name] for name in self.active_segments}


class EmailConfig(BaseModel):
    recipients: list[str]
    method: Literal["gmail_smtp", "resend"] = "gmail_smtp"
    subject_prefix: str = "[Argos Lead Finder]"


class Config(BaseModel):
    targets: TargetsConfig
    email: EmailConfig


def load_config(config_dir: Path | str = CONFIG_DIR) -> Config:
    config_dir = Path(config_dir)
    targets = yaml.safe_load((config_dir / "targets.yaml").read_text())
    email = yaml.safe_load((config_dir / "email.yaml").read_text())
    return Config(targets=TargetsConfig(**targets), email=EmailConfig(**email))
