"""Discovery source adapters. Importing this package registers every adapter."""
from .base import REGISTRY, ItemFailed, Source, SourceSkipped, get_adapter, register
from . import gdelt, rss, seatgeek, tokyonoizu, wp_events  # noqa: F401  (registration side effects)

__all__ = ["REGISTRY", "ItemFailed", "Source", "SourceSkipped", "get_adapter", "register"]
