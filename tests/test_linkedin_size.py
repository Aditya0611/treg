import asyncio

from app.filters import Filters
from app.pipeline import Job, Options, run_job
from app.treg_client import CallResult


def _run(linkedin_range: str, dropleads_count, leadocean=None, linkedin_page=None):
    filters = Filters(industry="metal fabrication", country="US", employees_min=51, employees_max=750, limit=1)
    job = Job(mode="query", filters=filters, options=Options(budget_usd=5))

    class Client:
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            rows = [{"domain": "steelco.com", "organization": "Steelco"}] if page == 1 else []
            return CallResult(endpoint="hunter.x.discover-companies", output={"companies": rows})

        async def identify_company(self, domain):
            raw = [{"matches": [{"confidence_score": 1.0, "company_data": {"basic_info": {
                "name": "Steelco",
                "primary_domain": domain,
                "all_domains": [domain],
                "professional_network_url": "https://www.linkedin.com/company/steelco",
                "employee_count_range": linkedin_range,
            }}}]}]
            return CallResult(endpoint="crustdata.companies.identify", output={}, raw=raw)

        async def linkedin_page(self, company):
            rows = [linkedin_page] if linkedin_page else []
            return CallResult(endpoint="crawl4ai.linkedin.company.profile", cost_micro=900, output={}, raw={"rows": rows})

        async def locate_company(self, domain):
            data = {"company_primary_address": leadocean} if leadocean else {}
            return CallResult(endpoint="leadocean.companies.enrich", cost_micro=5000, output={}, raw={"data": {"company_data": data}})

        async def fill_company(self, domain):
            facts = {"domain": domain, "city": "Cleveland", "state": "Ohio", "country": "United States"}
            if dropleads_count is not None:
                facts["employees"] = dropleads_count
            return CallResult(endpoint="dropleads.companies.enrich", cost_micro=1800, output=facts)

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                cost_micro=500,
                output={"domain": domain, "description": "Custom steel fabrication and welding shop.", "street_address": "1 Main St"},
            )

        async def judge_industry(self, company, industry=""):
            job.log("industry AI was called")
            return {"verdict": "match", "reason": "steel fabrication"}

    asyncio.run(run_job(job, Client(), []))
    return job


def test_industry_ai_is_off_by_default():
    job = _run("51-200", 130)
    row = job.companies[0]
    assert not any("industry AI was called" in line for line in job.events)
    assert "ai.industry" not in row["sources"]
    assert "industry_reason" not in row["fields"]
    assert row["status"] == "enriched"


def test_linkedin_range_inside_beats_a_wrong_dropleads_count():
    job = _run("201-500", 40)
    row = job.companies[0]
    assert row["employees"] == "201-500"
    assert row["fields"]["company_size"] == "201-500"
    assert row["fields"]["dropleads_employees"] == "40"
    assert row["status"] == "enriched"


def test_linkedin_range_inside_keeps_a_matching_exact_count():
    row = _run("51-200", 130).companies[0]
    assert row["employees"] == "130"
    assert row["fields"]["company_size"] == "51-200"


def test_linkedin_range_outside_removes_the_company():
    job = _run("1001-5000", 300)
    assert job.companies == []
    assert job.removed[0]["reason"] == "LinkedIn size 1001-5000 is outside 51–750"


def test_edge_range_uses_the_exact_count():
    kept = _run("501-1000", 620).companies[0]
    assert kept["employees"] == "620"
    assert kept["status"] == "enriched"
    dropped = _run("501-1000", 900)
    assert dropped.companies == []


def test_leadocean_supplies_the_location():
    row = _run("51-200", 130, {"city": "Akron", "state": "Ohio", "country": "US"}).companies[0]
    assert (row["city"], row["state"]) == ("Akron", "Ohio")
    assert row["fields"]["dropleads_location"] == "Cleveland, Ohio, United States"
    assert "leadocean.companies.enrich" in row["sources"]


def test_leadocean_country_is_shown_as_a_name():
    row = _run("51-200", 130, {"city": "Akron", "state": "Ohio", "country": "US"}).companies[0]
    assert row["country"] == "United States"
    assert row["location"] == "Akron, Ohio, United States"


def test_leadocean_with_another_country_keeps_dropleads():
    job = _run("51-200", 130, {"city": "Hasselager", "state": "", "country": "PL"})
    row = job.companies[0]
    assert (row["city"], row["country"]) == ("Cleveland", "United States")
    assert row["fields"]["leadocean_location"] == "Hasselager, PL"
    assert "leadocean.companies.enrich" not in row["sources"]
    assert job.removed == []


def test_linkedin_hq_matches_without_accents_or_order():
    page = {"size": "51-200 employees", "headquarters": "OHIO, Cléveland", "website": "steelco.com"}
    row = _run("51-200", 130, linkedin_page=page).companies[0]
    assert row["linkedin_check"] == "confirmed: website, size, HQ"


def test_linkedin_short_link_is_not_a_website_mismatch():
    page = {"size": "51-200 employees", "headquarters": "Cleveland, Ohio", "website": "https://bit.ly/abc"}
    row = _run("51-200", 130, linkedin_page=page).companies[0]
    assert row["linkedin_check"] == "confirmed: size, HQ"
    assert row["status"] == "enriched"


def test_leadocean_miss_keeps_dropleads_location():
    row = _run("51-200", 130, None).companies[0]
    assert row["city"] == "Cleveland"
    assert "leadocean.companies.enrich" not in row["sources"]


def test_leadocean_is_not_bought_for_a_size_drop():
    job = _run("1001-5000", 300, {"city": "Akron", "state": "Ohio", "country": "US"})
    assert job.removed and job.removed[0]["cost_micro"] == 1800


def test_linkedin_page_confirms_the_row():
    page = {"size": "51-200 employees", "headquarters": "Cleveland, Ohio", "website": "https://www.steelco.com/"}
    row = _run("51-200", 130, linkedin_page=page).companies[0]
    assert row["linkedin_check"] == "confirmed: website, size, HQ"
    assert row["status"] == "enriched"
    assert row["fields"]["linkedin_page_size"] == "51-200 employees"


def test_linkedin_page_mismatch_flags_without_removing():
    page = {"size": "1,001-5,000 employees", "headquarters": "Toledo, Ohio", "website": "steelco.com"}
    job = _run("51-200", 130, linkedin_page=page)
    row = job.companies[0]
    assert row["linkedin_check"].startswith("mismatch:")
    assert "LinkedIn page says 1,001-5,000 employees" in row["status"]
    assert "LinkedIn page HQ is Toledo, Ohio" in row["status"]
    assert row["status"].startswith("enriched; flagged:")
    assert job.removed == []


def test_linkedin_page_with_another_website_needs_review():
    page = {"size": "51-200 employees", "headquarters": "Cleveland, Ohio", "website": "othersteel.com"}
    row = _run("51-200", 130, linkedin_page=page).companies[0]
    assert row["status"].startswith("needs review: LinkedIn page lists the website othersteel.com")


def test_unreadable_linkedin_page_is_not_checked():
    row = _run("51-200", 130, linkedin_page=None).companies[0]
    assert row["linkedin_check"] == "not checked: LinkedIn page could not be read"
    assert row["status"] == "enriched"


def test_edge_range_without_a_count_needs_review():
    row = _run("501-1000", None).companies[0]
    assert row["employees"] == "501-1000"
    assert row["status"].startswith("needs review")
    assert "crosses the edge" in row["status"]
