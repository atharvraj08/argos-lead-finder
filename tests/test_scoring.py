from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from argos_leads import scoring
from argos_leads.config import CONFIG_DIR, ScoringConfig, TargetsConfig, load_config
from argos_leads.models import Lead
from argos_leads.scoring import excluded_reason, score_lead

TODAY = date(2026, 10, 17)
CFG = ScoringConfig(scalping_evidence=3, on_sale_within_30_days=2, per_intro_role_known=1, large_venue=1,
                    large_venue_keywords=["Arena", "Stadium", "Garden"])
CUSTOM = ScoringConfig(scalping_evidence=7, on_sale_within_30_days=5, per_intro_role_known=4, large_venue=9,
                       large_venue_keywords=["Hall"])


def mk(**kw) -> Lead:
    base = dict(event_key="k", week_found="2026-W42", segment="kpop", event="Tour", artist_or_headliner="X")
    base.update(kw)
    return Lead(**base)


def iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def targets(**kw) -> TargetsConfig:
    return TargetsConfig(active_segments=[], segments={}, **kw)


# --- weights -------------------------------------------------------------

def test_blank_lead_scores_zero():
    assert score_lead(mk(), CFG, TODAY) == (0, "no signals = 0")


def test_scalping_url():
    assert score_lead(mk(scalping_evidence_url="http://x"), CFG, TODAY) == (3, "scalping +3 = 3")


def test_scalping_summary_only():
    assert score_lead(mk(scalping_evidence_summary="resale at 5x"), CFG, TODAY)[0] == 3


def test_on_sale_soon():
    assert score_lead(mk(on_sale_date=iso(10)), CFG, TODAY) == (2, "on-sale soon +2 = 2")


@pytest.mark.parametrize("role", ["agency", "promoter", "organizer"])
def test_each_role(role):
    assert score_lead(mk(**{role: "Someone"}), CFG, TODAY) == (1, f"{role} +1 = 1")


def test_large_venue():
    assert score_lead(mk(venue="Crypto.com Arena"), CFG, TODAY) == (1, "large venue +1 = 1")


def test_custom_weights_config_driven():
    lead = mk(scalping_evidence_url="u", on_sale_date=iso(1), agency="A", promoter="P", organizer="O",
              venue="Carnegie Hall")
    total, bd = score_lead(lead, CUSTOM, TODAY)
    assert total == 7 + 5 + 4 * 3 + 9
    assert bd == "scalping +7, on-sale soon +5, agency +4, promoter +4, organizer +4, large venue +9 = 33"
    # keywords also config-driven: Arena is not large under CUSTOM
    assert score_lead(mk(venue="Big Arena"), CUSTOM, TODAY)[0] == 0


def test_zero_weight_disables_signal():
    cfg = CFG.model_copy(update={"large_venue": 0})
    assert score_lead(mk(venue="Big Arena"), cfg, TODAY) == (0, "no signals = 0")


def test_blank_roles_not_penalized():
    assert score_lead(mk(agency="  ", promoter="", organizer="", venue=""), CFG, TODAY)[0] == 0


def test_breakdown_format_full():
    lead = mk(scalping_evidence_url="u", on_sale_date=iso(5), agency="HYBE", venue="MetLife Stadium")
    assert score_lead(lead, CFG, TODAY) == (7, "scalping +3, on-sale soon +2, agency +1, large venue +1 = 7")


def test_real_config_weights_used():
    cfg = load_config(CONFIG_DIR).targets.scoring
    total, _ = score_lead(mk(scalping_evidence_url="u"), cfg, TODAY)
    assert total == cfg.scalping_evidence


# --- on-sale window --------------------------------------------------------

@pytest.mark.parametrize("days,expected", [(0, 2), (30, 2), (31, 0), (-1, 0), (-100, 0)])
def test_on_sale_boundaries(days, expected):
    assert score_lead(mk(on_sale_date=iso(days)), CFG, TODAY)[0] == expected


@pytest.mark.parametrize("value", ["", "   ", "TBA", "2026-13-45", None])
def test_on_sale_blank_or_unparseable(value):
    assert score_lead(mk(on_sale_date=value), CFG, TODAY)[0] == 0


def test_on_sale_iso_datetime_accepted():
    assert score_lead(mk(on_sale_date=iso(3) + "T10:00:00-04:00"), CFG, TODAY)[0] == 2


def test_uses_on_sale_not_event_date():
    assert score_lead(mk(event_date=iso(5), on_sale_date=""), CFG, TODAY)[0] == 0
    assert score_lead(mk(event_date=iso(200), on_sale_date=iso(5)), CFG, TODAY)[0] == 2


# --- venue whole-word -------------------------------------------------------

@pytest.mark.parametrize("venue,expected", [
    ("Crypto.com Arena", 1), ("crypto.com arena", 1), ("Arena", 1), ("Madison Square Garden", 1),
    ("SoFi Stadium (Inglewood)", 1), ("Arenas Bar", 0), ("Gardenia Lounge", 0), ("The Stadiumist", 0),
    ("Prudential Center", 0), ("", 0),
])
def test_large_venue_whole_word(venue, expected):
    assert score_lead(mk(venue=venue), CFG, TODAY)[0] == expected


# --- exclusion ----------------------------------------------------------------

def test_excluded_seller_norm_match():
    t = targets(exclude_sellers=["Ticketmaster"])
    assert excluded_reason(mk(ticket_seller="TICKETMASTER "), t) == "excluded seller"
    assert excluded_reason(mk(ticket_seller="AXS"), t) == ""
    assert excluded_reason(mk(ticket_seller=""), t) == ""


def test_seller_exclusion_precedes_competitor():
    t = targets(exclude_sellers=["Ticketmaster"], exclude_if_competitor="confirmed")
    lead = mk(ticket_seller="Ticketmaster", competitor_status="confirmed", competitor_vendor="Persona")
    assert excluded_reason(lead, t) == "excluded seller"


@pytest.mark.parametrize("mode,status,expected", [
    ("confirmed", "confirmed", "competitor confirmed: Persona"),
    ("confirmed", "likely", ""),
    ("confirmed", "unknown", ""),
    ("likely", "confirmed", "competitor confirmed: Persona"),
    ("likely", "likely", "competitor likely: Persona"),
    ("likely", "unknown", ""),
    ("likely", "", ""),
    ("never", "confirmed", ""),
    ("never", "likely", ""),
])
def test_competitor_modes(mode, status, expected):
    t = targets(exclude_if_competitor=mode)
    lead = mk(ticket_seller="AXS", competitor_status=status, competitor_vendor="Persona")
    assert excluded_reason(lead, t) == expected


def test_competitor_without_vendor_name():
    t = targets(exclude_if_competitor="confirmed")
    assert excluded_reason(mk(competitor_status="confirmed"), t) == "competitor confirmed"


# --- apply --------------------------------------------------------------------

def test_apply_sets_fields_and_keeps_excluded():
    t = targets(exclude_sellers=["Ticketmaster"], scoring=CFG)
    ctx = SimpleNamespace(config=SimpleNamespace(targets=t), today=TODAY)
    leads = [mk(ticket_seller="Ticketmaster", venue="X Arena"), mk(ticket_seller="AXS", agency="HYBE")]
    scoring.apply(ctx, leads)
    assert len(leads) == 2
    assert (leads[0].score, leads[0].excluded_reason) == (1, "excluded seller")
    assert leads[1].score == 1 and leads[1].score_breakdown == "agency +1 = 1" and leads[1].excluded_reason == ""
