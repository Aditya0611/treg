"""Company search agents, each called on its own so their results can be compared.

Every agent gets the same SearchSpec. Each one turns it into its own request, and every
answer is read into the same hit shape: domain, name, LinkedIn, and what the agent reported.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from app.normalize import clean_linkedin, clean_text, normalize_domain
from app.places import EUROPE, countries_label, country_codes, country_label

DEFAULT_SEARCH_AGENTS = ("exa.companies.search", "tomba.companies.search.reveal", "apollo.companies.search")

TOMBA_SIZES = ((1, 10, "1-10"), (11, 50, "11-50"), (51, 250, "51-250"), (251, 1000, "251-1K"),
               (1001, 5000, "1K-5K"), (5001, 10000, "5K-10K"), (10001, 50000, "10K-50K"))


@dataclass
class SearchSpec:
    label: str
    industry: str
    keywords: list[str]
    countries: list[str]
    employees_min: int
    employees_max: int
    place_text: str
    sentence: str = ""
    locations: list[str] = field(default_factory=list)

    def text(self) -> str:
        if self.sentence:
            return self.sentence
        kinds = ", ".join(self.keywords[:8]) if self.keywords else self.industry
        text = f"For-profit companies whose business is {kinds}"
        if self.place_text:
            text += f", headquartered in {self.place_text}"
        return f"{text}, with {self.employees_min} to {self.employees_max} employees"


def spec_from_filters(filters) -> SearchSpec:
    """The parsed query as one spec every search agent can read."""
    industry = " ".join((filters.industry or filters.q or "companies").split())
    keywords = [part.strip() for part in re.split(r",|;|\bor\b", industry) if part.strip()]
    codes = country_codes(filters.country or "")
    if filters.state:
        place = ", ".join(part for part in (filters.city, filters.state) if part)
        locations = [f"{place}, US"]
    elif filters.city:
        place = filters.city
        locations = [f"{filters.city}, {country_label(codes[0])}" if codes else filters.city]
    else:
        place = countries_label(filters.country or "")
        locations = []
    return SearchSpec(
        label="query",
        industry=industry,
        keywords=keywords,
        countries=codes,
        employees_min=filters.employees_min or 1,
        employees_max=filters.employees_max or 1_000_000,
        place_text=place,
        locations=locations,
    )


PRESETS = {
    "metal": SearchSpec(
        label="metal",
        industry="metal fabrication, machining, or contract metal manufacturing",
        keywords=[
            "metal fabrication", "custom metal fabrication", "contract manufacturing", "precision machining",
            "CNC machining", "job shop", "machine shop", "sheet metal fabrication", "plate fabrication",
            "laser cutting", "plasma cutting", "structural steel fabrication", "welding services", "custom steel",
            "machined components", "fabricated metal products", "metalworking",
        ],
        countries=["US", "CA", *EUROPE],
        employees_min=50,
        employees_max=750,
        place_text="the United States, Canada, or Europe",
        sentence=(
            "Metal fabrication, CNC machining, sheet metal, welding and contract metal manufacturing companies "
            "headquartered in the United States, Canada or Europe with 50 to 750 employees"
        ),
    ),
    "it-us": SearchSpec(
        label="it-us",
        industry="information technology",
        keywords=["IT services", "managed services", "software"],
        countries=["US"],
        employees_min=11,
        employees_max=50,
        place_text="the United States",
        sentence="For-profit information technology companies headquartered in the United States with 11-50 employees",
    ),
}


@dataclass(frozen=True)
class SearchAgent:
    id: str
    note: str
    method: str = "POST"
    as_query: bool = False
    hunter_style: bool = field(default=False)
    pages: bool = False

    def price(self, n: int) -> float:
        if self.id == "tomba.companies.search.reveal":
            return 0.000178 * math.ceil(n / 50)
        if self.id == "apollo.companies.search":
            return 0.026 * math.ceil(n / 100)
        if self.id == "exa.companies.search":
            return 0.007 + 0.001 * max(0, n - 10)
        if self.id == "parallel.companies.search":
            return 0.005
        return 0.0

    def request(self, spec: SearchSpec, n: int, page: int = 1) -> dict:
        if self.id == "hunter.x.discover-companies":
            return {"query": spec.text(), "limit": n, "offset": (page - 1) * n}
        if self.id == "tomba.companies.search.reveal":
            filters = {"keywords": {"include": spec.keywords}}
            if spec.countries:
                filters["location_country"] = {"include": spec.countries}
            sizes = tomba_sizes(spec.employees_min, spec.employees_max)
            if sizes:
                filters["size"] = {"include": sizes}
            return {"filters": filters, "page": page}
        if self.id == "apollo.companies.search":
            return {
                "q_organization_keyword_tags[]": spec.keywords,
                "organization_locations[]": spec.locations or [country_label(code) or code for code in spec.countries],
                "organization_num_employees_ranges[]": [f"{spec.employees_min},{spec.employees_max}"],
                "per_page": min(100, n),
                "page": page,
            }
        if self.id == "exa.companies.search":
            return {"query": spec.text(), "category": "company", "numResults": n}
        if self.id == "parallel.companies.search":
            return {"entity_type": "companies", "objective": spec.text(), "match_limit": max(5, min(100, n))}
        raise KeyError(self.id)


SEARCH_AGENTS = {
    agent.id: agent
    for agent in (
        SearchAgent("hunter.x.discover-companies", "sentence search, free (what the pipeline used before)", pages=True),
        SearchAgent("tomba.companies.search.reveal", "keywords matched on the description, country list, size bands", pages=True),
        SearchAgent("apollo.companies.search", "keyword tags, exact employee range, country list", as_query=True, pages=True),
        SearchAgent("exa.companies.search", "semantic search from a sentence"),
        SearchAgent("parallel.companies.search", "entity search from a sentence"),
    )
}


def tomba_sizes(low: int, high: int) -> list[str]:
    """Tomba's size labels that mostly sit inside the asked range (51-250 and 251-1K for 50-750)."""
    picked = []
    for band_low, band_high, label in TOMBA_SIZES:
        overlap = min(band_high, high) - max(band_low, low)
        if overlap > 0 and overlap >= 0.2 * (band_high - band_low):
            picked.append(label)
    return picked


def _exa_hits(payload) -> list[dict]:
    rows = payload.get("results") if isinstance(payload, dict) else None
    hits = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        entity = next((item for item in row.get("entities") or [] if isinstance(item, dict)), {})
        props = entity.get("properties") if isinstance(entity.get("properties"), dict) else entity
        url = str(row.get("url") or "")
        linkedin = clean_linkedin(url) if "linkedin.com" in url else ""
        hits.append({
            "domain": "" if linkedin else normalize_domain(url) or "",
            "name": clean_text(props.get("name") or row.get("title") or ""),
            "linkedin": linkedin,
            "reported": {key: props.get(key) for key in ("headcount", "location", "industry") if props.get(key)},
        })
    return hits


def _parallel_hits(payload) -> list[dict]:
    rows = payload.get("entities") if isinstance(payload, dict) else None
    hits = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        linkedin = clean_linkedin(url) if "linkedin.com" in url else ""
        hits.append({
            "domain": "" if linkedin else normalize_domain(url) or "",
            "name": clean_text(row.get("name") or ""),
            "linkedin": linkedin,
            "reported": {"description": clean_text(row.get("description") or "")[:300]},
        })
    return hits


def read_hits(agent_id: str, output, raw) -> list[dict]:
    """The companies an agent returned, deduplicated by domain or LinkedIn page, in its order."""
    from app.pipeline import companies_from_search

    if agent_id == "exa.companies.search":
        hits = _exa_hits(output) or _exa_hits(raw)
    elif agent_id == "parallel.companies.search":
        hits = _parallel_hits(output) or _parallel_hits(raw)
    else:
        hits = []
        for row in companies_from_search(output if isinstance(output, dict) else {}, raw):
            preview = row.get("preview") or {}
            hits.append({
                "domain": row["domain"],
                "name": row.get("company_name") or "",
                "linkedin": clean_linkedin(preview.get("linkedin") or ""),
                "reported": {key: preview.get(key) for key in ("employees", "city", "state", "country") if preview.get(key)},
            })
    unique, seen = [], set()
    for hit in hits:
        key = hit["domain"] or hit["linkedin"]
        if key and key not in seen:
            seen.add(key)
            unique.append(hit)
    return unique
