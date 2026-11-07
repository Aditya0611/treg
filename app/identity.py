"""Lock company identity before any enrich facts are trusted.

The website itself is the first witness: a domain that redirects to another company's site
(windows.com → microsoft.com) belongs to that company. A company database is the second
witness, and LinkedIn is compared across sources before the row is called confirmed.
"""

from __future__ import annotations

from app.normalize import normalize_domain

# A redirect here means the domain is parked or for sale, not a company site.
PARKED_HOSTS = (
    "hugedomains.com", "sedo.com", "dan.com", "afternic.com", "godaddy.com", "namecheap.com",
    "parkingcrew.net", "bodis.com", "above.com", "undeveloped.com", "squadhelp.com", "atom.com",
    "brandbucket.com", "domainmarket.com", "buydomains.com", "uniregistry.com", "sav.com", "epik.com",
)

# A redirect here is a hosted page or profile; the searched domain stays the company's identity.
PASS_THROUGH_HOSTS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "wixsite.com", "squarespace.com", "godaddysites.com", "business.site", "google.com",
    "weebly.com", "wordpress.com", "myshopify.com", "shopify.com", "webflow.io", "carrd.co",
    "hubspotpagebuilder.com", "site123.me", "jimdosite.com", "yolasite.com",
)

_SECOND_LEVEL = ("co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.za", "com.br", "com.mx", "co.jp", "co.in", "com.tr", "com.cn")


def locked_domain(seed_domain: str) -> str:
    """The domain that owns this row. Enrichment may not replace it."""
    return normalize_domain(seed_domain) or ""


def identity_mismatch(seed_domain: str, returned_domain: str) -> str:
    """Empty when the provider stayed on the same company; otherwise a drop reason."""
    left = locked_domain(seed_domain)
    right = normalize_domain(returned_domain) or ""
    if not left or not right or left == right:
        return ""
    return f"the lookup returned {right}, a different company from {left}"


def _on(host: str, bases: tuple[str, ...]) -> bool:
    return any(host == base or host.endswith("." + base) for base in bases)


def brand(host: str) -> str:
    """acme from acme.com, acme.de, shop.acme.co.uk."""
    text = normalize_domain(host) or ""
    for suffix in _SECOND_LEVEL:
        if text.endswith("." + suffix):
            text = text[: -(len(suffix) + 1)]
            return text.rsplit(".", 1)[-1]
    parts = text.split(".")
    return parts[-2] if len(parts) >= 2 else text


def redirect_identity(seed: str, final: str) -> tuple[str, str]:
    """(the domain that owns this row, a parked reason or "").

    Same brand on another country domain (acme.com → acme.de) keeps the searched domain.
    A different brand (windows.com → microsoft.com) means the row is that company.
    """
    left = normalize_domain(seed) or ""
    right = normalize_domain(final) or ""
    if not left or not right or left == right:
        return left, ""
    if _on(right, PARKED_HOSTS):
        return left, f"{left} is a parked or for-sale domain (it redirects to {right})"
    if _on(right, PASS_THROUGH_HOSTS):
        return left, ""
    if right.endswith("." + left) or left.endswith("." + right) or brand(left) == brand(right):
        return left, ""
    return right, ""


def crustdata_identity(payload, domain: str, *, min_confidence: float = 0.9) -> dict:
    """The company Crustdata matched to this domain, or {} when the match is weak or for another domain."""
    wanted = normalize_domain(domain) or ""
    rows = payload if isinstance(payload, list) else (payload.get("rows") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for match in row.get("matches") or []:
            if not isinstance(match, dict):
                continue
            try:
                confidence = float(match.get("confidence_score") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            info = (match.get("company_data") or {}).get("basic_info") or {}
            primary = normalize_domain(info.get("primary_domain") or "") or ""
            others = {normalize_domain(item) or "" for item in info.get("all_domains") or [] if isinstance(item, str)}
            if confidence < min_confidence or (wanted and wanted != primary and wanted not in others):
                continue
            return {
                "name": str(info.get("name") or "").strip(),
                "primary_domain": primary,
                "linkedin": str(info.get("professional_network_url") or "").strip(),
                "employees": str(info.get("employee_count_range") or "").strip(),
                "description": str(info.get("description") or "").strip(),
                "industries": [str(item) for item in info.get("industries") or [] if item],
                "company_type": str(info.get("company_type") or "").strip(),
                "confidence": confidence,
            }
    return {}
