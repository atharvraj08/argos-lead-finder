"""Competitor detection: does a ticket seller already use a competing KYC vendor?

Evidence sources, strongest first:
  1. Vendor SDK/script/iframe loaded on the seller's homepage or linked login/sign-up/verify/
     help pages -> confirmed.
  2. Vendor named in the seller's privacy policy / subprocessor / third-party list with
     identity-verification or processor context -> confirmed; bare mention -> likely.
  3. One web search (ctx.llm.search) for a public statement (press release, vendor case
     study): explicit -> confirmed; indirect -> likely.

Never assumes a vendor: every confirmed/likely result carries an evidence URL. Results are
cached per seller in `competitor_cache` for `caching.competitor_recheck_days`.
Exclusion is NOT decided here (scoring applies `exclude_if_competitor`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from ..db import utcnow
from ..models import norm
from .vendors import Vendor, host_matches, vendors_for

log = logging.getLogger(__name__)

Status = Literal["confirmed", "likely", "unknown"]

MAX_ACCOUNT_PAGES = 4   # login / sign-up / verify / help pages linked from the homepage
MAX_POLICY_PAGES = 3    # privacy / subprocessor / third-party pages
CONTEXT_CHARS = 200     # text window around a vendor-name match

ACCOUNT_LINK = re.compile(
    r"log\s*-?\s*in|sign\s*-?\s*in|sign\s*-?\s*up|register|create.?account|my.?account|"
    r"verif|identity|help|support|faq", re.IGNORECASE)
POLICY_LINK = re.compile(
    r"privacy|sub-?processor|third.?part|service.?provider|vendors?\b|partners?\b", re.IGNORECASE)
SUBPROCESSOR_PAGE = re.compile(r"sub-?processor|third.?part(?:y|ies)\s+(?:list|providers?|vendors?)|service.?providers?",
                               re.IGNORECASE)
# Context that makes a vendor-name mention an actual statement of use.
STRONG_CONTEXT = re.compile(
    r"identity\s+verif|verif(?:y|ies|ying)\s+(?:your|the|user|customer)?\s*identit|id\s+verif|"
    r"verify\s+your\s+id|kyc|know\s+your\s+customer|age\s+verif|document\s+verif|selfie|biometric|"
    r"sub-?processor|service\s+provider|data\s+processor|processor|identity\s+provider|"
    r"fraud\s+prevention|we\s+use|we\s+partner|powered\s+by|provided\s+by",
    re.IGNORECASE)

_RANK = {"confirmed": 2, "likely": 1, "unknown": 0}
_KIND_RANK = {"script": 3, "policy": 2, "page_text": 2, "search": 1}


@dataclass
class CompetitorResult:
    vendor: str
    status: Status
    evidence_url: str


UNKNOWN = CompetitorResult("", "unknown", "")


@dataclass
class _Finding:
    vendor: str
    status: Status
    url: str
    kind: str          # script | policy | page_text | search
    detail: str = ""
    published: str = ""  # search findings only, YYYY-MM-DD when known

    def key(self) -> tuple:
        return (_RANK[self.status], _KIND_RANK.get(self.kind, 0), self.published or "")


# --- web search schema -------------------------------------------------------

class SearchFinding(BaseModel):
    vendor: str = Field("", description="Exactly one name from the allowed vendor list, or empty")
    evidence_url: str = Field("", description="URL of the page that states it")
    explicit: bool = Field(False, description="True only if the page explicitly says this seller uses this vendor "
                                              "for identity verification (e.g. press release, vendor case study)")
    published: str = Field("", description="Page date YYYY-MM-DD if shown")
    summary: str = Field("", description="One short paraphrased line")


class SearchAnswer(BaseModel):
    findings: list[SearchFinding] = []


# --- HTML analysis (pure functions, unit-testable) ---------------------------

def _resource_urls(soup: BeautifulSoup) -> list[str]:
    urls = []
    for tag, attr in (("script", "src"), ("iframe", "src"), ("link", "href"), ("embed", "src"),
                      ("object", "data"), ("frame", "src")):
        for el in soup.find_all(tag):
            v = el.get(attr)
            if v:
                urls.append(v)
    return urls


def analyze_scripts(html: str, page_url: str, vendors: list[Vendor]) -> list[_Finding]:
    """Vendor SDK loaded by the page (script/iframe/link src, or SDK identifiers in inline JS)."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, _Finding] = {}
    for raw in _resource_urls(soup):
        host = urlsplit(urljoin(page_url, raw)).netloc
        for v in vendors:
            if v.name not in found and any(host_matches(host, d) for d in v.script_domains):
                found[v.name] = _Finding(v.name, "confirmed", page_url, "script", f"loads {raw}")
    inline = "\n".join(s.get_text() for s in soup.find_all("script") if not s.get("src")).lower()
    if inline:
        hosts = {h.lower() for h in re.findall(r"https?://([a-z0-9.-]+)", inline)}
        for v in vendors:
            if v.name in found:
                continue
            hit = next((s for s in v.sdk_strings if s in inline), None)
            if not hit:
                hit = next((h for h in hosts for d in v.script_domains if host_matches(h, d)), None)
            if hit:
                found[v.name] = _Finding(v.name, "confirmed", page_url, "script", f"inline script: {hit}")
    return list(found.values())


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def analyze_text(text: str, page_url: str, vendors: list[Vendor], kind: str = "policy",
                 is_subprocessor_list: bool = False) -> list[_Finding]:
    """Vendor named in page text. Named with identity/processor context (or anywhere on a
    subprocessor list) -> confirmed; bare mention -> likely."""
    out = []
    for v in vendors:
        best: _Finding | None = None
        for pat in v.name_patterns:
            for m in pat.finditer(text):
                window = text[max(0, m.start() - CONTEXT_CHARS): m.end() + CONTEXT_CHARS]
                strong = is_subprocessor_list or bool(STRONG_CONTEXT.search(window))
                if not strong and kind != "policy":
                    continue  # bare mentions on non-policy pages (event listings, news) are noise
                status: Status = "confirmed" if strong else "likely"
                snippet = text[max(0, m.start() - 80): m.end() + 80]
                if best is None or _RANK[status] > _RANK[best.status]:
                    best = _Finding(v.name, status, page_url, kind, snippet)
                if strong:
                    break
            if best and best.status == "confirmed":
                break
        if best:
            out.append(best)
    return out


def _base_domain(host: str) -> str:
    host = host.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def find_links(html: str, page_url: str, pattern: re.Pattern, limit: int) -> list[str]:
    """Same-site links whose href or anchor text matches `pattern`."""
    soup = BeautifulSoup(html, "html.parser")
    base = _base_domain(urlsplit(page_url).netloc)
    seen, out = {page_url.rstrip("/")}, []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(page_url, href).split("#")[0]
        parts = urlsplit(full)
        if parts.scheme not in ("http", "https") or not host_matches(parts.netloc, base):
            continue
        if not (pattern.search(href) or pattern.search(a.get_text(" ") or "")):
            continue
        if full.rstrip("/") in seen:
            continue
        seen.add(full.rstrip("/"))
        out.append(full)
        if len(out) >= limit:
            break
    return out


# --- evidence gathering -------------------------------------------------------

def _fetch(ctx, url: str) -> str | None:
    try:
        r = ctx.http.get(url)
    except Exception as e:  # RobotsDisallowed, HTTP errors, network errors
        log.info("competitors: skip %s (%s)", url, e, extra={"url": url})
        return None
    return r.text if r.ok else None


def _homepage(seller: str) -> str | None:
    try:
        from ..enrich.sellers import seller_homepage  # Agent B; lazy so a missing module is non-fatal
    except ImportError:
        log.info("competitors: enrich.sellers unavailable; no homepage for %s", seller)
        return None
    try:
        url = seller_homepage(seller)
    except Exception as e:
        log.warning("competitors: seller_homepage(%r) failed: %s", seller, e)
        return None
    return url or None


def _page_evidence(ctx, homepage: str, vendors: list[Vendor]) -> list[_Finding]:
    findings: list[_Finding] = []
    html = _fetch(ctx, homepage)
    if html is None:
        return findings
    findings += analyze_scripts(html, homepage, vendors)
    findings += analyze_text(page_text(html), homepage, vendors, kind="page_text")

    for url in find_links(html, homepage, ACCOUNT_LINK, MAX_ACCOUNT_PAGES):
        sub = _fetch(ctx, url)
        if sub is None:
            continue
        findings += analyze_scripts(sub, url, vendors)
        findings += analyze_text(page_text(sub), url, vendors, kind="page_text")

    policy_urls = find_links(html, homepage, POLICY_LINK, MAX_POLICY_PAGES)
    fetched: set[str] = set()
    queue = list(policy_urls)
    while queue and len(fetched) < MAX_POLICY_PAGES:
        url = queue.pop(0)
        if url in fetched:
            continue
        fetched.add(url)
        sub = _fetch(ctx, url)
        if sub is None:
            continue
        text = page_text(sub)
        title = (BeautifulSoup(sub, "html.parser").title or None)
        is_list = bool(SUBPROCESSOR_PAGE.search(url) or (title and SUBPROCESSOR_PAGE.search(title.get_text())))
        findings += analyze_text(text, url, vendors, kind="policy", is_subprocessor_list=is_list)
        # one hop: privacy policy -> its subprocessor list
        for nxt in find_links(sub, url, SUBPROCESSOR_PAGE, 2):
            if nxt not in fetched and nxt not in queue:
                queue.insert(0, nxt)
    return findings


def _search_evidence(ctx, seller: str, vendors: list[Vendor]) -> list[_Finding]:
    allowed = {norm(v.name): v.name for v in vendors}
    question = (
        f"Which identity-verification (ID verification / KYC) vendor does the ticket seller "
        f"\"{seller}\" use? Only consider these vendors: {', '.join(v.name for v in vendors)}. "
        f"Look for press releases, vendor case studies, or the seller's own help/privacy pages "
        f"(e.g. search \"{seller} identity verification\"). Report a finding only if a page you "
        f"found names both {seller} and the vendor; set explicit=true only if it plainly states "
        f"{seller} uses that vendor. If nothing is found, return no findings."
    )
    try:
        ans = ctx.llm.search(SearchAnswer, question, max_searches=2)
    except Exception as e:
        log.warning("competitors: search failed for %s: %s", seller, e)
        return []
    out = []
    for f in (ans.findings if ans else []):
        name = allowed.get(norm(f.vendor))
        url = (f.evidence_url or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            continue
        out.append(_Finding(name, "confirmed" if f.explicit else "likely", url, "search",
                            f.summary, f.published or ""))
    return out


def _pick(seller: str, findings: list[_Finding]) -> CompetitorResult:
    if not findings:
        return UNKNOWN
    best = max(findings, key=_Finding.key)
    others = sorted({f"{f.vendor}:{f.status}" for f in findings if f.vendor != best.vendor})
    if others:
        log.info("competitors: %s -> %s (%s); other vendors seen: %s",
                 seller, best.vendor, best.status, ", ".join(others))
    log.info("competitors: %s -> %s %s via %s (%s) %s", seller, best.vendor, best.status, best.kind,
             best.detail[:160], best.url, extra={"url": best.url})
    return CompetitorResult(best.vendor, best.status, best.url)


# --- cache -------------------------------------------------------------------

def _cached(ctx, key: str) -> CompetitorResult | None:
    row = ctx.db.one("SELECT * FROM competitor_cache WHERE seller_norm = ?", (key,))
    if not row or not row["checked_at"]:
        return None
    try:
        checked = datetime.fromisoformat(row["checked_at"]).date()
    except ValueError:
        return None
    if (ctx.today - checked).days >= ctx.config.targets.caching.competitor_recheck_days:
        return None
    status = row["status"] if row["status"] in _RANK else "unknown"
    if status == "unknown":
        return UNKNOWN
    return CompetitorResult(row["vendor"] or "", status, row["evidence_url"] or "")


def _store(ctx, seller: str, key: str, res: CompetitorResult) -> None:
    ctx.db.execute(
        "INSERT OR REPLACE INTO competitor_cache (seller_norm, seller, vendor, status, evidence_url, checked_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (key, seller, res.vendor, res.status, res.evidence_url, utcnow()),
    )


# --- public API ----------------------------------------------------------------

def check_seller(ctx, seller: str) -> CompetitorResult:
    key = norm(seller)
    if not key:
        return UNKNOWN
    hit = _cached(ctx, key)
    if hit is not None:
        return hit

    vendors = vendors_for(ctx.config.targets.competitor_vendors)
    if not vendors:
        return UNKNOWN
    findings: list[_Finding] = []
    homepage = _homepage(seller)
    if homepage:
        findings += _page_evidence(ctx, homepage, vendors)
    else:
        log.info("competitors: no known homepage for %s; search evidence only", seller)
    # Search only if the seller's own pages did not already confirm a vendor (saves a search).
    if not any(f.status == "confirmed" for f in findings):
        findings += _search_evidence(ctx, seller, vendors)

    res = _pick(seller, findings)
    _store(ctx, seller, key, res)
    return res


def apply(ctx, leads) -> None:
    """Fill competitor_vendor/status/evidence_url on each lead (each distinct seller checked once)."""
    results: dict[str, CompetitorResult] = {}
    for lead in leads:
        key = norm(lead.ticket_seller)
        if not key:
            res = UNKNOWN
        else:
            if key not in results:
                try:
                    results[key] = check_seller(ctx, lead.ticket_seller)
                except Exception as e:  # never let one seller break the run
                    log.exception("competitors: check failed for %s: %s", lead.ticket_seller, e)
                    results[key] = UNKNOWN
            res = results[key]
        lead.competitor_vendor = res.vendor
        lead.competitor_status = res.status
        lead.competitor_evidence_url = res.evidence_url
