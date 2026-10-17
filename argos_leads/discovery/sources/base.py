"""Source adapter base class and registry (keyed by `adapter` in config/targets.yaml)."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...config import SegmentConfig, SourceConfig
from ...models import EventRecord, RawItem

if TYPE_CHECKING:
    from ...context import RunContext

log = logging.getLogger("argos_leads.discovery")

REGISTRY: dict[str, type["Source"]] = {}


class SourceSkipped(Exception):
    """Raised by collect() when a source can't run (no API key, robots, etc.).
    Logged as status 'skipped' rather than 'error'."""


class ItemFailed(Exception):
    """Raised by parse_item() when an item could not be processed this run (e.g. LLM
    unavailable). The raw item stays unprocessed and is retried next run."""


def register(name: str):
    def deco(cls: type["Source"]) -> type["Source"]:
        cls.adapter_name = name
        REGISTRY[name] = cls
        return cls

    return deco


def get_adapter(name: str) -> type["Source"]:
    if name not in REGISTRY:
        raise KeyError(f"unknown discovery adapter {name!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[name]


class Source(ABC):
    adapter_name: str = ""
    # Structured sources (APIs, trackers with fixed fields) win field conflicts on merge.
    structured: bool = False

    def __init__(self, cfg: SourceConfig, segment: str, segment_cfg: SegmentConfig):
        self.cfg = cfg
        self.segment = segment
        self.segment_cfg = segment_cfg
        self.params = cfg.params

    @property
    def slug(self) -> str:
        return self.cfg.slug

    def region(self, ctx: "RunContext") -> str:
        return ctx.config.targets.region or "US"

    @abstractmethod
    def collect(self, ctx: "RunContext") -> list[RawItem]:
        """Fetch new items from the source (cheap; no LLM)."""

    def after_collect(self, ctx: "RunContext") -> None:
        """Called once collected items are stored (e.g. to save a 'last seen' marker)."""

    @abstractmethod
    def parse_item(self, ctx: "RunContext", item: RawItem) -> list[EventRecord]:
        """Turn one raw item into zero or more EventRecords (U.S. only)."""

    def parse(self, ctx: "RunContext", items: list[RawItem]) -> list[EventRecord]:
        out: list[EventRecord] = []
        for item in items:
            try:
                out.extend(self.parse_item(ctx, item))
            except ItemFailed as e:
                log.warning("item not parsed: %s", e, extra={"source": self.slug, "url": item.url})
        return out
