"""Detection signatures for competing KYC / ID-verification vendors.

Three kinds of signal per vendor:
  - script_domains: hosts that serve the vendor's SDK/widget. A <script>, <iframe>, <link>,
    <embed>/<object> or inline-JS URL on one of these hosts means the page loads the vendor.
  - sdk_strings: identifiers that only appear in code that uses the vendor's SDK (matched in
    inline <script> bodies, case-insensitive).
  - name_patterns: regexes for the vendor's name in visible/policy text. A match is evidence
    only together with surrounding context (see competitors.analyze_text).

Vendors listed in config `competitor_vendors` without a built-in entry get a name-only
signature (case-insensitive whole-word match), so they are still detectable in policy text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import norm


@dataclass(frozen=True)
class Vendor:
    name: str
    script_domains: tuple[str, ...] = ()
    sdk_strings: tuple[str, ...] = ()
    name_patterns: tuple[re.Pattern, ...] = field(default=())


def _ci(*pats: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in pats)


def _cs(*pats: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in pats)


# Keyed by norm(name). Domains are matched as host == d or host endswith "." + d.
BUILTIN: dict[str, Vendor] = {norm(v.name): v for v in [
    Vendor(
        "Persona",
        script_domains=("withpersona.com",),
        sdk_strings=("persona.client", "persona-client", "@withpersona", "withpersona.com"),
        # "persona" is a common word: require capitalized brand or the company name.
        name_patterns=_cs(r"\bPersona Identities\b", r"\bwithpersona\.com\b", r"\bPersona\b(?!\s*(?:\d|Live|Super|Concert|Q\b|Dancing|Royal|Golden|Stage|Orchestra))")
        + _ci(r"\bwithpersona\b"),
    ),
    Vendor(
        "Onfido",
        script_domains=("onfido.com",),
        sdk_strings=("onfido.init", "onfido-sdk", "onfido_sdk", "onfidosdk"),
        name_patterns=_ci(r"\bonfido\b"),
    ),
    Vendor(
        "Jumio",
        script_domains=("jumio.com", "netverify.com", "jumio.ai"),
        sdk_strings=("netverify", "jumio-sdk", "jumiosdk"),
        name_patterns=_ci(r"\bjumio\b", r"\bnetverify\b"),
    ),
    Vendor(
        "Veriff",
        script_domains=("veriff.com", "veriff.me"),
        sdk_strings=("@veriff/", "veriff.mount", "createveriffframe", "veriffsdk", "veriff-sdk"),
        name_patterns=_ci(r"\bveriff\b"),  # \b keeps "verified" out
    ),
    Vendor(
        "Sumsub",
        script_domains=("sumsub.com", "sumsub.io"),
        sdk_strings=("snswebsdk", "@sumsub/", "sumsub-websdk"),
        name_patterns=_ci(r"\bsum\s*sub\b", r"\bsum\s*(?:&|and)\s*substance\b"),
    ),
    Vendor(
        "Socure",
        script_domains=("socure.com", "socure.us"),
        sdk_strings=("socureinitializer", "devicer.io", "socure-sdk", "socuredocv"),
        name_patterns=_ci(r"\bsocure\b"),
    ),
    Vendor(
        "ID.me",
        script_domains=("id.me",),
        sdk_strings=("api.id.me", "idme-button", "idme_button"),
        name_patterns=_ci(r"\bid\.me\b"),
    ),
    Vendor(
        "Incode",
        script_domains=("incode.com", "incodesmile.com", "incodesmile.ai"),
        sdk_strings=("incodesmile", "@incodetech/", "incode-welcome", "onboardingsdk"),
        name_patterns=_ci(r"\bincode\s+technologies\b", r"\bincodesmile\b") + _cs(r"\bIncode\b"),
    ),
    Vendor(
        "AU10TIX",
        script_domains=("au10tix.com",),
        sdk_strings=("au10tix",),
        name_patterns=_ci(r"\bau10tix\b"),
    ),
    Vendor(
        "Stripe Identity",
        # NOT js.stripe.com / checkout.stripe.com: those are payments, not identity.
        script_domains=("verify.stripe.com", "identity.stripe.com"),
        sdk_strings=("verifyidentity(", "stripe.verifyidentity", "identity/verification_sessions",
                     "identity.verificationsessions"),
        name_patterns=_ci(r"\bstripe\s+identity\b"),
    ),
    Vendor(
        "Clear",
        script_domains=("clearme.com",),
        sdk_strings=("clearme.com",),
        # Bare "clear"/"CLEAR" is far too ambiguous (all-caps legal text); require brand forms.
        name_patterns=_ci(r"\bclear\s+secure\b", r"\balclear\b", r"\bclearme\b", r"\bclear\s+verified\b")
        + _cs(r"\bCLEAR\s+(?:ID|identity|app|Verified|verification|Powered)\b"),
    ),
]}


def vendors_for(names: list[str]) -> list[Vendor]:
    """Signatures for the configured competitor_vendors (in config order)."""
    out = []
    for name in names:
        v = BUILTIN.get(norm(name))
        if v is None:
            v = Vendor(name, name_patterns=(re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE),))
        elif v.name != name:
            v = Vendor(name, v.script_domains, v.sdk_strings, v.name_patterns)
        out.append(v)
    return out


def host_matches(host: str, domain: str) -> bool:
    host = host.lower().split(":")[0]
    return host == domain or host.endswith("." + domain)
