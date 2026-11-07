"""Treg agents that look a company up by domain, and how to read each one's answer.

Each agent is called directly by its catalog id, so Treg does not route to another provider.
Prices are the catalog's per-call (or per-success) prices, used only to budget a run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.normalize import clean_linkedin, clean_text, company_from_payload, employee_bounds, normalize_domain


@dataclass(frozen=True)
class Agent:
    id: str
    price_usd: float
    method: str
    body: Callable[[str], dict]
    note: str = ""


def _by_domain(domain: str) -> dict:
    return {"domain": domain}


def _by_domain_list(domain: str) -> dict:
    return {"domains": [domain]}


AGENTS: tuple[Agent, ...] = (
    Agent("crawl4ai.companies.enrich", 0.0005, "GET", _by_domain),
    Agent("dropleads.companies.enrich", 0.0018, "POST", _by_domain_list),
    Agent("thecompaniesapi.companies.enrich", 0.0019, "GET", _by_domain),
    Agent("hunter.companies.enrich", 0.0049, "GET", _by_domain, "billed only when the record is complete"),
    Agent("leadocean.companies.enrich", 0.005, "POST", _by_domain),
    Agent("tomba.companies.enrich", 0.0089, "GET", _by_domain),
    Agent("companyenrich.companies.enrich", 0.0098, "GET", _by_domain),
    Agent("prospeo.companies.enrich", 0.0245, "POST", lambda domain: {"data": {"company_website": domain}}),
    Agent("apollo.companies.enrich", 0.026, "GET", _by_domain),
    Agent("crustdata.companies.identify", 0.0, "POST", _by_domain_list, "free; identity and LinkedIn only"),
    Agent("blitzapi.companies.domain-to-linkedin", 0.000495, "POST", _by_domain, "LinkedIn only"),
)

AGENTS_BY_ID = {agent.id: agent for agent in AGENTS}

FIELDS = ("domain", "name", "linkedin", "employees", "city", "state", "country", "industry")

_HQ = "locations.headquarters"
_LEADOCEAN = "data.company_data"
_CRUST = "0.matches.0.company_data"

# Candidate paths per field, tried in order against the saved answer.
PATHS: dict[str, dict[str, tuple[str, ...]]] = {
    "crawl4ai.companies.enrich": {
        "domain": ("domain",),
        "name": ("name",),
        "linkedin": ("linkedin_url", "linkedin"),
        "employees": ("people_count", "employees", "employee_count"),
        "city": ("city", "location.city"),
        "state": ("state", "location.state"),
        "country": ("country", "location.country"),
        "industry": ("industry",),
    },
    "dropleads.companies.enrich": {
        "domain": ("companies.0.domain", "domain", "companies.0.websiteUrl", "websiteUrl"),
        "name": ("companies.0.name", "name"),
        "linkedin": ("companies.0.linkedinUrl", "linkedinUrl"),
        "employees": ("companies.0.employees", "employees"),
        "city": ("companies.0.city", "city"),
        "state": ("companies.0.state", "state"),
        "country": ("companies.0.country", "country"),
        "industry": ("companies.0.industry", "industry"),
    },
    "thecompaniesapi.companies.enrich": {
        "domain": ("domain.domain",),
        "name": ("about.name",),
        "linkedin": ("socials.linkedin.url", "socials.linkedin"),
        "employees": ("about.totalEmployeesExact", "about.totalEmployees"),
        "city": (f"{_HQ}.city.name",),
        "state": (f"{_HQ}.state.name",),
        "country": (f"{_HQ}.country.name", f"{_HQ}.country.code"),
        "industry": ("about.industry", "about.industries.0"),
    },
    "hunter.companies.enrich": {
        "domain": ("data.domain",),
        "name": ("data.name",),
        "linkedin": ("data.linkedin.handle",),
        "employees": ("data.metrics.employees", "data.metrics.employeesRange"),
        "city": ("data.geo.city",),
        "state": ("data.geo.state",),
        "country": ("data.geo.country", "data.geo.countryCode"),
        "industry": ("data.category.industry", "data.category.sector"),
    },
    "tomba.companies.enrich": {
        "domain": ("data.domain", "meta.domain"),
        "name": ("data.name",),
        "linkedin": ("data.linkedin.handle",),
        "employees": ("data.metrics.employees",),
        "city": ("data.geo.city",),
        "state": ("data.geo.state",),
        "country": ("data.geo.country", "data.geo.countryCode"),
        "industry": ("data.category.industry", "data.tags.0"),
    },
    "leadocean.companies.enrich": {
        "domain": (f"{_LEADOCEAN}.company_domain",),
        "name": (f"{_LEADOCEAN}.company_name",),
        "linkedin": (f"{_LEADOCEAN}.company_linkedin_url", f"{_LEADOCEAN}.linkedin_url"),
        "employees": (f"{_LEADOCEAN}.company_employees",),
        "city": (f"{_LEADOCEAN}.company_primary_address.city",),
        "state": (f"{_LEADOCEAN}.company_primary_address.state", f"{_LEADOCEAN}.company_primary_address.region"),
        "country": (f"{_LEADOCEAN}.company_primary_address.country",),
        "industry": (f"{_LEADOCEAN}.company_industry",),
    },
    "companyenrich.companies.enrich": {
        "domain": ("domain",),
        "name": ("name",),
        "linkedin": ("socials.linkedin_url", "socials.linkedin"),
        "employees": ("employees", "reported_employees"),
        "city": ("location.city.name", "location.city"),
        "state": ("location.state.name", "location.state"),
        "country": ("location.country.name", "location.country.code", "location.country"),
        "industry": ("industry", "industries.0"),
    },
    "prospeo.companies.enrich": {
        "domain": ("company.domain",),
        "name": ("company.name",),
        "linkedin": ("company.linkedin_url",),
        "employees": ("company.employee_count", "company.employee_range"),
        "city": ("company.location.city",),
        "state": ("company.location.state",),
        "country": ("company.location.country", "company.location.country_code"),
        "industry": ("company.industry",),
    },
    "apollo.companies.enrich": {
        "domain": ("organization.primary_domain", "organization.website_url"),
        "name": ("organization.name",),
        "linkedin": ("organization.linkedin_url",),
        "employees": ("organization.estimated_num_employees",),
        "city": ("organization.city",),
        "state": ("organization.state",),
        "country": ("organization.country",),
        "industry": ("organization.industry",),
    },
    "crustdata.companies.identify": {
        "domain": (f"{_CRUST}.basic_info.primary_domain",),
        "name": (f"{_CRUST}.basic_info.name",),
        "linkedin": (f"{_CRUST}.basic_info.professional_network_url",),
        "employees": (f"{_CRUST}.basic_info.employee_count_range",),
        "industry": (f"{_CRUST}.basic_info.industries.0",),
    },
    "blitzapi.companies.domain-to-linkedin": {
        "name": ("company_name",),
        "linkedin": ("company_linkedin_url",),
    },
}

# These agents never report the field, so a blank is "not offered", not "missing".
NOT_OFFERED: dict[str, frozenset[str]] = {
    "crustdata.companies.identify": frozenset({"city", "state", "country"}),
    "blitzapi.companies.domain-to-linkedin": frozenset({"domain", "employees", "city", "state", "country", "industry"}),
}


def _get(node, path: str):
    for part in path.split("."):
        if isinstance(node, list):
            if not part.isdigit():
                return None
            index = int(part)
            node = node[index] if index < len(node) else None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def _scalar(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return clean_text(value)
    return ""


def _roots(answer: dict) -> list:
    roots = []
    for key in ("raw", "output"):
        value = answer.get(key)
        if isinstance(value, (dict, list)) and value:
            roots.append(value)
    return roots


def _linkedin(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if "linkedin.com" in text.lower():
        return clean_linkedin(text)
    handle = text.strip("/")
    if handle.lower().startswith(("company/", "school/", "showcase/")):
        return f"https://www.linkedin.com/{handle}"
    return f"https://www.linkedin.com/company/{handle}"


def linkedin_slug(value: str) -> str:
    """company/acme-inc from any LinkedIn company URL or handle."""
    text = _linkedin(value).lower().split("?", 1)[0].rstrip("/")
    marker = "linkedin.com/"
    if marker not in text:
        return ""
    path = text.split(marker, 1)[1]
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"company", "school", "showcase"}:
        return parts[1]
    return parts[-1] if parts else ""


def extract(agent_id: str, answer: dict) -> dict:
    """The fields one agent returned, in a common shape. Blank means the agent did not say."""
    record = {field: "" for field in FIELDS}
    roots = _roots(answer or {})
    for field, paths in PATHS.get(agent_id, {}).items():
        for root in roots:
            for path in paths:
                text = _scalar(_get(root, path))
                if text:
                    record[field] = text
                    break
            if record[field]:
                break
    offered = set(FIELDS) - set(NOT_OFFERED.get(agent_id, ()))
    if roots and any(not record[field] for field in offered):
        output = roots[-1] if isinstance(roots[-1], dict) else {}
        raw = roots[0]
        fallback = company_from_payload(output, raw, domain="", fallback_name="")
        for field in offered:
            if not record[field] and fallback.get(field):
                record[field] = _scalar(fallback.get(field))
    record["domain"] = normalize_domain(record["domain"]) or ""
    record["linkedin"] = _linkedin(record["linkedin"])
    low, high = employee_bounds(record["employees"]) if record["employees"] else (None, None)
    record["employees_low"] = low
    record["employees_high"] = high
    record["answered"] = any(record[field] for field in FIELDS)
    return record
