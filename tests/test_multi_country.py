import asyncio

from app.filters import Filters, parse_query_rules
from app.normalize import location_mismatch
from app.pipeline import Job, Options, run_job
from app.places import countries_label, countries_value, country_codes
from app.search_agents import SEARCH_AGENTS, spec_from_filters
from app.treg_client import CallResult


def test_country_list_expands_europe():
    codes = country_codes("US,CA,EU")
    assert codes[:2] == ["US", "CA"]
    assert "DE" in codes and "GB" in codes
    assert countries_value("US, Canada and Europe") == "US,CA,EU"
    assert countries_label("US,CA,EU") == "United States, Canada or Europe"
    assert country_codes("Texas") == []


def test_location_accepts_any_listed_country():
    assert location_mismatch({"country": "Germany"}, country="US,CA,EU") == ""
    assert location_mismatch({"state": "Ohio"}, country="US,CA,EU") == ""
    assert location_mismatch({}, country="US,CA,EU") == ""
    assert "outside United States, Canada or Europe" in location_mismatch({"country": "India"}, country="US,CA,EU")


def test_parser_reads_several_countries_and_keywords():
    filters = parse_query_rules(
        "metal fabrication, CNC machining, sheet metal fabrication companies in US, Canada and Europe with 50-750 employees"
    )
    assert filters.country == "US,CA,EU"
    assert filters.industry == "metal fabrication, CNC machining, sheet metal fabrication"
    assert filters.employees_max == 750
    austin = parse_query_rules("SaaS companies in Austin, Texas with 11-50 employees")
    assert (austin.city, austin.state, austin.country) == ("Austin", "Texas", "US")


def test_spec_from_filters_feeds_every_agent():
    filters = Filters(industry="metal fabrication, CNC machining", country="US,CA,EU", employees_min=51, employees_max=750)
    spec = spec_from_filters(filters)
    assert spec.keywords == ["metal fabrication", "CNC machining"]
    assert "DE" in spec.countries
    apollo = SEARCH_AGENTS["apollo.companies.search"].request(spec, 100, page=2)
    assert apollo["page"] == 2
    assert apollo["organization_num_employees_ranges[]"] == ["51,750"]
    assert "Germany" in apollo["organization_locations[]"]
    tomba = SEARCH_AGENTS["tomba.companies.search.reveal"].request(spec, 50)
    assert tomba["filters"]["size"]["include"] == ["51-250", "251-1K"]
    assert "Europe" in spec.text()


def test_pipeline_searches_with_the_benchmark_agents(monkeypatch):
    monkeypatch.setenv("TREG_SEARCH_AGENTS", "tomba.companies.search.reveal,apollo.companies.search")
    filters = Filters(industry="metal fabrication", country="US,CA,EU", employees_min=51, employees_max=750, limit=4)
    job = Job(mode="query", filters=filters, options=Options(contacts_per_company=0, budget_usd=5))
    answers = {
        "tomba.companies.search.reveal": [["steelco.com", "Steelco"], ["dupe.com", "Dupe"]],
        "apollo.companies.search": [["dupe.com", "Dupe"], ["fabwerk.de", "Fabwerk"], ["indiafab.in", "IndiaFab"]],
    }
    countries = {"fabwerk.de": "Germany", "indiafab.in": "India"}

    class Client:
        def __init__(self):
            self.asked = []

        async def search_with_agent(self, agent, spec, n, page=1):
            self.asked.append((agent.id, page))
            rows = answers[agent.id] if page == 1 else []
            return CallResult(
                endpoint=agent.id,
                cost_micro=100,
                output={"companies": [{"domain": d, "name": name} for d, name in rows]},
            )

        async def search_companies_sized(self, *args, **kwargs):
            raise AssertionError("Hunter must not be used when search agents are set")

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                cost_micro=1800,
                output={"domain": domain, "employees": 120, "country": countries.get(domain, "United States")},
            )

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                cost_micro=500,
                output={"domain": domain, "description": "Custom sheet metal fabrication and CNC machining shop.", "street_address": "1 Main St"},
            )

        async def judge_industry(self, company, industry=""):
            return {"verdict": "match", "reason": "metal fabrication shop"}

    client = Client()
    asyncio.run(run_job(job, client, []))
    kept = [row["domain"] for row in job.companies]
    assert kept == ["steelco.com", "fabwerk.de", "dupe.com"]
    assert ("tomba.companies.search.reveal", 1) in client.asked
    removed = {row["domain"]: row["reason"] for row in job.removed}
    assert removed == {"indiafab.in": "location India is outside United States, Canada or Europe"}
