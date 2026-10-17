"""Ticketing domain -> canonical ticket seller name.

`seller_from_url(url)`:
  * known primary-seller domain -> canonical name (e.g. "AXS")
  * resale/aggregator domain (StubHub, Vivid Seats, SeatGeek, ...) -> "" (never the primary
    seller from a URL alone; only a page that states it is the official seller can make it one)
  * social / link-shortener / news / search domains -> "" (not a seller)
  * any other domain -> its registrable domain (e.g. "thewarfieldtheatre.com"), usually a venue
    box office; callers must mark that fill low confidence (see `is_known_seller_url`).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..models import norm

# (domain suffix, canonical seller name). Longest suffix wins, so subdomains can override.
_DOMAINS: list[tuple[str, str]] = [
    # Ticketmaster family. TM itself and Live Nation pages (which sell via TM) -> "Ticketmaster";
    # Universe, TicketWeb and Front Gate keep their own names (see SELLER_PARENT).
    ("ticketmaster.com", "Ticketmaster"), ("ticketmaster.ca", "Ticketmaster"),
    ("ticketmaster.com.mx", "Ticketmaster"), ("ticketmaster.co.uk", "Ticketmaster"),
    ("ticketmaster.ie", "Ticketmaster"), ("ticketmaster.com.au", "Ticketmaster"),
    ("ticketmaster.co.nz", "Ticketmaster"), ("ticketmaster.de", "Ticketmaster"),
    ("ticketmaster.fr", "Ticketmaster"), ("ticketmaster.nl", "Ticketmaster"),
    ("ticketmaster.es", "Ticketmaster"), ("ticketmaster.co.jp", "Ticketmaster"),
    ("ticketmaster.sg", "Ticketmaster"), ("ticketmaster.com.br", "Ticketmaster"),
    ("livenation.com", "Ticketmaster"), ("livenation.ca", "Ticketmaster"),
    ("livenation.co.uk", "Ticketmaster"), ("livenationentertainment.com", "Ticketmaster"),
    ("tm.com", "Ticketmaster"), ("ticketm.net", "Ticketmaster"), ("ticketmaster.evyy.net", "Ticketmaster"),
    ("universe.com", "Universe"),
    ("ticketweb.com", "TicketWeb"), ("ticketweb.ca", "TicketWeb"), ("ticketweb.co.uk", "TicketWeb"),
    ("frontgatetickets.com", "Front Gate Tickets"),
    # AEG
    ("axs.com", "AXS"), ("axs.co.uk", "AXS"), ("aegpresents.com", "AXS"), ("aegworldwide.com", "AXS"),
    # Other U.S. primaries
    ("eventbrite.com", "Eventbrite"), ("eventbrite.ca", "Eventbrite"), ("eventbrite.co.uk", "Eventbrite"),
    ("eventbrite.com.au", "Eventbrite"), ("evbuc.com", "Eventbrite"), ("eventbrite.es", "Eventbrite"),
    ("ticketfly.com", "Ticketfly"),
    ("dice.fm", "DICE"), ("link.dice.fm", "DICE"),
    ("etix.com", "Etix"),
    ("tixr.com", "Tixr"),
    ("seetickets.us", "See Tickets"), ("seetickets.com", "See Tickets"), ("wl.seetickets.us", "See Tickets"),
    ("showclix.com", "ShowClix"),
    ("feverup.com", "Fever"), ("fever.com", "Fever"),
    ("prekindle.com", "Prekindle"),
    ("ticketspice.com", "TicketSpice"), ("webconnex.com", "TicketSpice"),
    ("freshtix.com", "Freshtix"),
    ("evenue.net", "Paciolan"), ("paciolan.com", "Paciolan"),
    ("ticketon.com", "Ticketon"),
    ("ticketleap.com", "TicketLeap"), ("ticketleap.events", "TicketLeap"),
    ("ticketsauce.com", "Ticketsauce"),
    ("holdmyticket.com", "HoldMyTicket"),
    ("ticketbud.com", "Ticketbud"),
    ("brownpapertickets.com", "Brown Paper Tickets"),
    ("stubwire.com", "Stubwire"),
    ("ticketstoday.com", "TicketsToday"),
    ("tix.com", "Tix.com"),
    ("eventim.us", "CTS Eventim"), ("eventim.com", "CTS Eventim"), ("eventim.de", "CTS Eventim"),
    ("ticketsource.us", "TicketSource"), ("ticketsource.co.uk", "TicketSource"),
    ("simpletix.com", "SimpleTix"),
    ("soundrink.com", "Soundrink"), ("fakestarusa.com", "Fakestar USA"), ("atgtickets.com", "ATG Tickets"),
    ("ticketor.com", "Ticketor"),
    ("vbotickets.com", "VBO Tickets"),
    ("bandsintown.com", ""),  # aggregator of links; not a seller
    ("songkick.com", ""),
    ("ticketek.com", "Ticketek"), ("ticketek.com.au", "Ticketek"), ("premier.ticketek.com.au", "Ticketek"),
    ("ticketek.co.nz", "Ticketek"),
    # K-pop / J-pop specialty and Asian sellers
    ("interpark.com", "Interpark (NOL)"), ("globalinterpark.com", "Interpark (NOL)"),
    ("nol.com", "Interpark (NOL)"), ("nolticket.com", "Interpark (NOL)"),
    ("yes24.com", "Yes24"),
    ("melon.com", "Melon Ticket"),
    ("weverse.io", "Weverse"),
    ("mymusictaste.com", "MyMusicTaste"), ("mmt.live", "MyMusicTaste"),
    ("kpopmasterz.com", "Kpop Masterz"), ("danalent.com", "Kpop Masterz"),
    ("kohai.com", "Kohai Tickets"), ("tickets.kohai.com", "Kohai Tickets"), ("kohaitickets.com", "Kohai Tickets"),
    ("kktix.com", "KKTIX"), ("kkday.com", "KKday"),
    ("eplus.jp", "e+"), ("pia.jp", "Ticket Pia"), ("l-tike.com", "Lawson Ticket"),
    ("zaiko.io", "ZAIKO"),
    ("ticketmelon.com", "Ticketmelon"),
    ("trip.com", ""),
]

# Resale marketplaces / aggregators: never the primary seller from a URL alone.
AGGREGATOR_DOMAINS: dict[str, str] = {
    "seatgeek.com": "SeatGeek", "seatgeek.ca": "SeatGeek", "sgcdn.com": "SeatGeek",
    "stubhub.com": "StubHub", "stubhub.ca": "StubHub", "stubhub.co.uk": "StubHub",
    "vividseats.com": "Vivid Seats", "tickpick.com": "TickPick", "gametime.co": "Gametime",
    "viagogo.com": "viagogo", "ticketnetwork.com": "TicketNetwork", "ticketsmarter.com": "TicketSmarter",
    "ticketcity.com": "TicketCity", "razorgator.com": "RazorGator", "ticketliquidator.com": "Ticket Liquidator",
    "ticketiq.com": "TicketIQ", "megaseats.com": "MegaSeats", "rukkus.com": "Rukkus", "scorebig.com": "ScoreBig",
    "ticketsnow.com": "TicketsNow", "seatsforeveryone.com": "Seats For Everyone",
    "concerts50.com": "Concerts50", "ticketsales.com": "TicketSales", "boxofficeticketsales.com": "Box Office Ticket Sales",
    "cheaptickets.com": "CheapTickets", "eventticketscenter.com": "Event Tickets Center",
    "ticketclub.com": "Ticket Club", "goldstar.com": "Goldstar",
}

# Domains that are never a ticket seller (social, shorteners, news, search, fan sites).
NON_SELLER_DOMAINS: set[str] = {
    "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.me", "youtube.com", "youtu.be",
    "tiktok.com", "threads.net", "linktr.ee", "linkin.bio", "bit.ly", "t.co", "tinyurl.com", "ow.ly",
    "buff.ly", "lnk.to", "found.ee", "smarturl.it", "google.com", "goo.gl", "apple.com", "spotify.com",
    "wikipedia.org", "reddit.com", "soompi.com", "allkpop.com", "billboard.com", "pollstar.com",
    "kpopofficial.com", "kpopconcerts.com", "mamasgeeky.com", "tokyonoizu.com", "jrocknews.com",
    "animenewsnetwork.com", "koreaboo.com", "ticketnews.com", "gdeltproject.org", "bandsintown.com",
    "songkick.com", "naver.com", "daum.net", "x.co", "trip.com",
}

# Ticketing sites whose terms bar scraping or that block non-browser clients: never fetched.
NO_FETCH_DOMAINS: set[str] = {
    "ticketmaster.com", "livenation.com", "axs.com", "eventbrite.com", "seatgeek.com", "tixr.com",
    "seetickets.us", "stubhub.com", "vividseats.com",
}

SELLER_HOMEPAGES: dict[str, str] = {
    "Ticketmaster": "https://www.ticketmaster.com", "Universe": "https://www.universe.com",
    "TicketWeb": "https://www.ticketweb.com", "Front Gate Tickets": "https://www.frontgatetickets.com",
    "AXS": "https://www.axs.com", "Eventbrite": "https://www.eventbrite.com", "Ticketfly": "https://www.eventbrite.com",
    "DICE": "https://dice.fm", "Etix": "https://www.etix.com", "Tixr": "https://www.tixr.com",
    "See Tickets": "https://www.seetickets.us", "ShowClix": "https://www.showclix.com",
    "Fever": "https://feverup.com", "Prekindle": "https://www.prekindle.com",
    "TicketSpice": "https://www.ticketspice.com", "Freshtix": "https://www.freshtix.com",
    "Paciolan": "https://www.paciolan.com", "Ticketon": "https://www.ticketon.com",
    "TicketLeap": "https://www.ticketleap.com", "Ticketsauce": "https://www.ticketsauce.com",
    "HoldMyTicket": "https://www.holdmyticket.com", "Ticketbud": "https://www.ticketbud.com",
    "Brown Paper Tickets": "https://www.brownpapertickets.com", "Stubwire": "https://www.stubwire.com",
    "TicketsToday": "https://www.ticketstoday.com", "Tix.com": "https://www.tix.com",
    "CTS Eventim": "https://www.eventim.us", "TicketSource": "https://www.ticketsource.us",
    "SimpleTix": "https://www.simpletix.com", "Ticketor": "https://www.ticketor.com",
    "VBO Tickets": "https://www.vbotickets.com", "Ticketek": "https://premier.ticketek.com.au",
    "Interpark (NOL)": "https://world.nol.com", "Yes24": "https://ticket.yes24.com",
    "Melon Ticket": "https://ticket.melon.com", "Weverse": "https://weverse.io",
    "MyMusicTaste": "https://www.mymusictaste.com", "Kpop Masterz": "https://kpopmasterz.com",
    "Kohai Tickets": "https://tickets.kohai.com", "KKTIX": "https://kktix.com", "KKday": "https://www.kkday.com",
    "e+": "https://eplus.jp", "Ticket Pia": "https://t.pia.jp", "Lawson Ticket": "https://l-tike.com",
    "ZAIKO": "https://zaiko.io", "Ticketmelon": "https://www.ticketmelon.com",
    "SeatGeek": "https://seatgeek.com", "StubHub": "https://www.stubhub.com", "Vivid Seats": "https://www.vividseats.com",
    "TickPick": "https://www.tickpick.com", "Gametime": "https://gametime.co",
    "Soundrink": "https://tix.soundrink.com", "Fakestar USA": "https://fakestarusa.com",
    "ATG Tickets": "https://www.atgtickets.com",
}

# Ownership: lets competitor/exclusion logic treat TM subsidiaries as the TM family if wanted.
SELLER_PARENT: dict[str, str] = {
    "Universe": "Ticketmaster", "TicketWeb": "Ticketmaster", "Front Gate Tickets": "Ticketmaster",
    "Ticketfly": "Eventbrite", "Kpop Masterz": "Danal Entertainment",
}

# Free-text seller names (as pages write them) -> canonical name.
_NAME_ALIASES: dict[str, str] = {
    "ticketmaster": "Ticketmaster", "ticket master": "Ticketmaster", "live nation": "Ticketmaster",
    "livenation": "Ticketmaster", "ticketmaster com": "Ticketmaster",
    "universe": "Universe", "ticketweb": "TicketWeb", "front gate tickets": "Front Gate Tickets",
    "frontgate tickets": "Front Gate Tickets", "front gate": "Front Gate Tickets",
    "axs": "AXS", "axs com": "AXS", "aeg presents": "AXS",
    "eventbrite": "Eventbrite", "ticketfly": "Ticketfly", "dice": "DICE", "dice fm": "DICE",
    "etix": "Etix", "tixr": "Tixr", "see tickets": "See Tickets", "seetickets": "See Tickets",
    "showclix": "ShowClix", "fever": "Fever", "prekindle": "Prekindle", "ticketspice": "TicketSpice",
    "freshtix": "Freshtix", "evenue": "Paciolan", "paciolan": "Paciolan", "ticketon": "Ticketon",
    "ticketleap": "TicketLeap", "ticketek": "Ticketek", "interpark": "Interpark (NOL)",
    "interpark global": "Interpark (NOL)", "nol": "Interpark (NOL)", "nol world": "Interpark (NOL)",
    "nol interpark": "Interpark (NOL)", "interpark nol": "Interpark (NOL)", "yes24": "Yes24",
    "yes24 global": "Yes24", "melon ticket": "Melon Ticket", "weverse": "Weverse", "weverse shop": "Weverse",
    "mymusictaste": "MyMusicTaste", "my music taste": "MyMusicTaste", "kpop masterz": "Kpop Masterz",
    "kpopmasterz": "Kpop Masterz", "kohai tickets": "Kohai Tickets", "kohai": "Kohai Tickets",
    "seatgeek": "SeatGeek", "stubhub": "StubHub", "vivid seats": "Vivid Seats", "vividseats": "Vivid Seats",
    "tickpick": "TickPick", "gametime": "Gametime", "zaiko": "ZAIKO", "kktix": "KKTIX",
}
AGGREGATOR_NAMES: set[str] = set(AGGREGATOR_DOMAINS.values())

_MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "ne.jp", "or.jp",
    "co.kr", "or.kr", "com.mx", "com.br", "com.sg", "com.hk", "com.tw", "co.in", "com.cn",
}


def _host(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return ""
    return host[4:] if host.startswith("www.") else host


def registrable_domain(url: str) -> str:
    """'https://tickets.foo.co.uk/x' -> 'foo.co.uk'. Good enough without a PSL dependency."""
    host = _host(url)
    if not host or host.replace(".", "").isdigit():
        return ""
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _match(host: str, domains) -> str | None:
    """Return the longest domain in `domains` that `host` equals or is a subdomain of."""
    best = None
    for d in domains:
        if host == d or host.endswith("." + d):
            if best is None or len(d) > len(best):
                best = d
    return best


_DOMAIN_MAP = dict(_DOMAINS)


def is_aggregator_url(url: str) -> bool:
    return _match(_host(url), AGGREGATOR_DOMAINS) is not None


def is_non_seller_url(url: str) -> bool:
    host = _host(url)
    if _match(host, NON_SELLER_DOMAINS):
        return True
    d = _match(host, _DOMAIN_MAP)
    return d is not None and _DOMAIN_MAP[d] == ""


def is_known_seller_url(url: str) -> bool:
    """True when the URL is on a known primary-seller domain (not a guess from the domain)."""
    d = _match(_host(url), _DOMAIN_MAP)
    return d is not None and _DOMAIN_MAP[d] != ""


def may_fetch(url: str) -> bool:
    """False for ticketing sites whose terms bar scraping or that block bots."""
    return _match(_host(url), NO_FETCH_DOMAINS) is None and not is_known_seller_url(url) \
        and not is_aggregator_url(url)


def aggregator_name(url: str) -> str:
    d = _match(_host(url), AGGREGATOR_DOMAINS)
    return AGGREGATOR_DOMAINS[d] if d else ""


def seller_from_url(url: str) -> str:
    """Canonical seller for a ticket URL; '' if unknown, aggregator/resale, or not a seller.
    Unknown non-aggregator domains return their registrable domain (callers mark low confidence)."""
    host = _host(url)
    if not host:
        return ""
    d = _match(host, _DOMAIN_MAP)
    if d is not None:
        return _DOMAIN_MAP[d]
    if _match(host, AGGREGATOR_DOMAINS) or _match(host, NON_SELLER_DOMAINS):
        return ""
    return registrable_domain(url)


def canonical_seller_name(name: str) -> str:
    """Normalize a free-text seller name ('Ticketmaster.com', 'AXS') to the canonical name.
    Unknown names are returned trimmed, unchanged."""
    name = (name or "").strip()
    if not name:
        return ""
    n = norm(name)
    if n in _NAME_ALIASES:
        return _NAME_ALIASES[n]
    if "." in name and " " not in name:  # looks like a domain
        mapped = seller_from_url(name)
        if mapped:
            return mapped
    for canon in set(_DOMAIN_MAP.values()) | AGGREGATOR_NAMES:
        if canon and norm(canon) == n:
            return canon
    return name


def is_aggregator_name(name: str) -> bool:
    return canonical_seller_name(name) in AGGREGATOR_NAMES


def seller_homepage(name: str) -> str | None:
    """Homepage for a canonical seller name (or a domain-named box office); None if unknown."""
    canon = canonical_seller_name(name)
    if canon in SELLER_HOMEPAGES:
        return SELLER_HOMEPAGES[canon]
    if canon and "." in canon and " " not in canon:
        dom = registrable_domain(canon)
        if dom:
            return f"https://{dom}"
    return None


def seller_parent(name: str) -> str:
    canon = canonical_seller_name(name)
    return SELLER_PARENT.get(canon, canon)
