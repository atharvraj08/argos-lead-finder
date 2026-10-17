"""Lead scoring and exclusion rules. All weights come from config (targets.yaml `scoring`)."""
from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from ..config import ScoringConfig, TargetsConfig
from ..models import Lead, norm

if TYPE_CHECKING:
    from ..context import RunContext

ON_SALE_WINDOW_DAYS = 30  # window length is part of the rule name (on_sale_within_30_days), not a weight

_ROLES = ("agency", "promoter", "organizer")
_EXCLUDED_STATUSES = {
    "confirmed": ("confirmed",),
    "likely": ("confirmed", "likely"),
    "never": (),
}


def _parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD (or an ISO datetime starting with it). Anything else -> None."""
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _is_large_venue(venue: str, keywords: list[str]) -> bool:
    """Whole-word, case-insensitive match: 'Crypto.com Arena' yes, 'Arenas Bar' no."""
    if not venue:
        return False
    for kw in keywords:
        kw = kw.strip()
        if kw and re.search(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", venue, re.IGNORECASE):
            return True
    return False


def score_lead(lead: Lead, scoring_cfg: ScoringConfig, today: date) -> tuple[int, str]:
    """Additive score and a human-readable breakdown. Never negative; blank fields add nothing."""
    parts: list[tuple[str, int]] = []

    if (lead.scalping_evidence_url or "").strip() or (lead.scalping_evidence_summary or "").strip():
        parts.append(("scalping", scoring_cfg.scalping_evidence))

    on_sale = _parse_date(lead.on_sale_date)  # on-sale date only, never event_date
    if on_sale is not None and 0 <= (on_sale - today).days <= ON_SALE_WINDOW_DAYS:
        parts.append(("on-sale soon", scoring_cfg.on_sale_within_30_days))

    for role in _ROLES:
        if (getattr(lead, role) or "").strip():
            parts.append((role, scoring_cfg.per_intro_role_known))

    if _is_large_venue(lead.venue, scoring_cfg.large_venue_keywords):
        parts.append(("large venue", scoring_cfg.large_venue))

    parts = [(label, pts) for label, pts in parts if pts > 0]  # a 0 weight disables a signal
    total = sum(pts for _, pts in parts)
    if not parts:
        return 0, "no signals = 0"
    return total, ", ".join(f"{label} +{pts}" for label, pts in parts) + f" = {total}"


def excluded_reason(lead: Lead, targets_cfg: TargetsConfig) -> str:
    """Reason this lead is excluded from output, or '' if it is kept."""
    seller = norm(lead.ticket_seller)
    if seller and seller in {norm(s) for s in targets_cfg.exclude_sellers}:
        return "excluded seller"
    status = (lead.competitor_status or "").strip().lower()
    if status and status in _EXCLUDED_STATUSES.get(targets_cfg.exclude_if_competitor, ()):
        vendor = (lead.competitor_vendor or "").strip()
        return f"competitor {status}: {vendor}" if vendor else f"competitor {status}"
    return ""


def apply(ctx: "RunContext", leads: list[Lead]) -> None:
    """Set score, score_breakdown and excluded_reason on every lead (excluded leads stay in the list)."""
    targets = ctx.config.targets
    for lead in leads:
        lead.score, lead.score_breakdown = score_lead(lead, targets.scoring, ctx.today)
        lead.excluded_reason = excluded_reason(lead, targets)
