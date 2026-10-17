"""Event discovery: collect raw items from v1 sources, turn them into new U.S. EventRecords.

Public API:
    collect(ctx, segments=None, daily_only=False) -> dict[slug, int]
    discover(ctx, segment) -> list[EventRecord]
"""
from .core import build_sources, collect, discover, merge_records

__all__ = ["collect", "discover", "build_sources", "merge_records"]
