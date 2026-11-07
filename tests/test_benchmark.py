import asyncio
import json
from pathlib import Path

from openpyxl import load_workbook

from app.agents import AGENTS, AGENTS_BY_ID, extract, linkedin_slug
from app.benchmark_report import write_report
from app.benchmark_run import answers_from_cache, plan, run_agents, saved_domains
from app.benchmark_score import load_answers, load_truth, score_all, score_company
from app.treg_client import CallResult, TregError

ROOT = Path(__file__).resolve().parents[1]

HUNTER = {
    "data": {
        "name": "Stripe",
        "domain": "stripe.com",
        "category": {"industry": "Internet Software & Services"},
        "geo": {"city": "South San Francisco", "state": "California", "country": "United States"},
        "linkedin": {"handle": "company/stripe"},
        "metrics": {"employees": "5K-10K"},
    }
}
APOLLO = {
    "organization": {
        "name": "Microsoft",
        "primary_domain": "microsoft.com",
        "linkedin_url": "http://www.linkedin.com/company/microsoft",
        "estimated_num_employees": 221000,
        "city": "Redmond",
        "state": "Washington",
        "country": "United States",
        "industry": "computer software",
    }
}
CRUST = [
    {
        "matched_on": "stripe.com",
        "matches": [
            {
                "company_data": {
                    "basic_info": {
                        "name": "Stripe",
                        "primary_domain": "stripe.com",
                        "professional_network_url": "https://www.linkedin.com/company/stripe",
                        "employee_count_range": "5001-10000",
                        "industries": ["Technology, Information and Internet"],
                    }
                }
            }
        ],
    }
]
TRUTH_WINDOWS = {
    "domain": "windows.com",
    "canonical_domain": "microsoft.com",
    "linkedin": "https://www.linkedin.com/company/microsoft",
    "employees_min": "100000",
    "employees_max": "",
    "state": "Washington",
    "country": "United States",
    "industry": "software",
    "case": "generic_domain",
    "verified": "yes",
}


def test_extract_reads_nested_provider_shapes():
    hunter = extract("hunter.companies.enrich", {"raw": HUNTER, "output": {}})
    assert hunter["domain"] == "stripe.com"
    assert linkedin_slug(hunter["linkedin"]) == "stripe"
    assert (hunter["employees_low"], hunter["employees_high"]) == (5000, 10000)
    assert hunter["state"] == "California"

    crust = extract("crustdata.companies.identify", {"raw": CRUST, "output": {}})
    assert crust["domain"] == "stripe.com"
    assert crust["employees_low"] == 5001

    tomba = extract("tomba.companies.enrich", {"raw": {"data": {"domain": "acme.com", "linkedin": {"handle": "acme"}}}})
    assert tomba["linkedin"] == "https://www.linkedin.com/company/acme"

    empty = extract("apollo.companies.enrich", {})
    assert empty["answered"] is False


def test_identity_accepts_the_real_company_for_a_generic_domain():
    row = score_company("apollo.companies.enrich", TRUTH_WINDOWS, {"status": "ok", "raw": APOLLO, "output": {}})
    by_field = {item["field"]: item["status"] for item in row["results"]}
    assert by_field == {"identity": "pass", "linkedin": "pass", "employees": "pass", "location": "pass", "industry": "pass"}

    small = {"organization": {"primary_domain": "windows.com", "estimated_num_employees": 20, "state": "Texas", "country": "US"}}
    wrong = score_company("apollo.companies.enrich", TRUTH_WINDOWS, {"status": "ok", "raw": small, "output": {}})
    statuses = {item["field"]: item["status"] for item in wrong["results"]}
    assert statuses["identity"] == "pass"
    assert statuses["employees"] == "fail"
    assert statuses["location"] == "fail"
    assert statuses["linkedin"] == "missing"


def test_subsidiary_returning_the_parent_fails_identity():
    truth = {"domain": "slack.com", "canonical_domain": "", "case": "subsidiary"}
    row = score_company(
        "apollo.companies.enrich",
        truth,
        {"status": "ok", "raw": {"organization": {"primary_domain": "salesforce.com"}}, "output": {}},
    )
    assert row["results"][0]["status"] == "fail"


def test_unknown_truth_and_unoffered_fields_are_not_counted():
    truth = {"domain": "acme.com", "linkedin": "https://www.linkedin.com/company/acme", "case": "normal"}
    row = score_company(
        "blitzapi.companies.domain-to-linkedin",
        truth,
        {"status": "ok", "raw": {"company_linkedin_url": "https://www.linkedin.com/company/acme/"}, "output": {}},
    )
    statuses = {item["field"]: item["status"] for item in row["results"]}
    assert statuses == {"identity": "n/a", "linkedin": "pass", "employees": "n/a", "location": "n/a", "industry": "n/a"}

    miss = score_company("apollo.companies.enrich", truth, {"status": "miss"})
    assert {item["field"]: item["status"] for item in miss["results"]}["linkedin"] == "missing"
    assert miss["answered"] is False


class FakeClient:
    def __init__(self):
        self.calls = []

    async def call(self, endpoint, body, extra_headers=None, *, method="POST", as_query=False):
        self.calls.append((endpoint, body, method))
        domain = body.get("domain") or (body.get("domains") or [""])[0] or body.get("data", {}).get("company_website", "")
        if domain == "missing.com":
            raise TregError(404, "not found")
        return CallResult(endpoint=endpoint, output={}, raw={"data": {"domain": domain}}, cost_micro=5000)


def test_runner_saves_raw_answers_resumes_and_respects_the_cap(tmp_path):
    hunter = AGENTS_BY_ID["hunter.companies.enrich"]
    client = FakeClient()
    domains = ["acme.com", "missing.com", "beta.com"]

    outcome = asyncio.run(run_agents(client, [hunter], domains, tmp_path, max_usd=1.0, pause_s=0, log=lambda _m: None))
    assert outcome["calls"] == 3
    assert client.calls[0] == ("hunter.companies.enrich", {"domain": "acme.com"}, "GET")
    lines = [json.loads(line) for line in (tmp_path / "hunter.companies.enrich.jsonl").read_text().splitlines()]
    assert [line["status"] for line in lines] == ["ok", "miss", "ok"]
    assert saved_domains(tmp_path, hunter.id) == set(domains)

    again = asyncio.run(run_agents(client, [hunter], domains, tmp_path, max_usd=1.0, pause_s=0, log=lambda _m: None))
    assert again["calls"] == 0
    assert plan([hunter], domains, tmp_path)[0]["todo"] == 0

    apollo = AGENTS_BY_ID["apollo.companies.enrich"]
    capped = asyncio.run(run_agents(client, [apollo], domains, tmp_path, max_usd=0.03, pause_s=0, log=lambda _m: None))
    assert capped["calls"] == 1
    assert "cap" in capped["stopped"]


def test_cached_dropleads_answers_score_for_free(tmp_path):
    cache = {
        "acme.com": {
            "dropleads_response": {
                "domain": "acme.com",
                "linkedinUrl": "http://www.linkedin.com/company/acme",
                "employees": 30,
                "state": "Texas",
                "country": "United States",
                "industry": "information technology & services",
            },
            "street_page": {"domain": "acme.com", "employees": "9", "state": ""},
        }
    }
    written = answers_from_cache(cache, ["acme.com", "other.com"], tmp_path)
    assert written == {"dropleads.companies.enrich": 1, "crawl4ai.companies.enrich": 1}
    truth = [{
        "domain": "acme.com",
        "linkedin": "https://www.linkedin.com/company/acme",
        "employees_min": "11",
        "employees_max": "50",
        "state": "Texas",
        "country": "United States",
        "industry": "information technology",
        "case": "normal",
        "verified": "yes",
    }]
    scored = score_all(truth, load_answers(tmp_path))
    dropleads = next(item for item in scored["summaries"] if item["agent"] == "dropleads.companies.enrich")
    assert dropleads["overall"] == 1.0
    assert dropleads["spent_usd"] == 0
    crawl = next(item for item in scored["summaries"] if item["agent"] == "crawl4ai.companies.enrich")
    assert crawl["fields"]["employees"]["fail"] == 1
    # The cache kept only part of the crawl4ai page, so unsaved fields are not held against it.
    assert crawl["fields"]["location"]["missing"] == 0
    assert crawl["fields"]["industry"]["missing"] == 0

    out = write_report(scored, truth, tmp_path / "scores.xlsx", verified_only=True)
    book = load_workbook(out)
    assert book.sheetnames == ["Read me", "Agents", "Best per field", "Field detail", "Every check", "Ground truth"]
    agents = [row[0].value for row in book["Agents"].iter_rows(min_row=2)]
    assert set(agents) == {"dropleads.companies.enrich", "crawl4ai.companies.enrich"}


def test_ground_truth_template_loads_and_every_agent_has_paths():
    rows = load_truth(ROOT / "benchmark" / "ground_truth.csv")
    assert len(rows) == 30
    assert len({row["domain"] for row in rows}) == 30
    assert {"generic_domain", "rebrand", "subsidiary", "foreign_with_us_office"} <= {row["case"] for row in rows}
    for agent in AGENTS:
        assert extract(agent.id, {})["answered"] is False
