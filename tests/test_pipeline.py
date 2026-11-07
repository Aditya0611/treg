import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from app.filters import Filters
from app.normalize import normalize_domain
from app.phones import foreign_phone_reason
from app.pipeline import Job, Options, _cache_decision, _headcount_band, _street_page_needs_fetch, run_job
from app.treg_client import CallResult, TregClient, TregError


class Fake:
    def __init__(self):
        self.emails = []
        self.people = []

    async def search_companies_sized(self, filters, page: int = 1, **_extra):
        return await self.search_companies(filters)

    async def fill_company(self, domain):
        enriched = await self.enrich(domain, "")
        return CallResult(
            endpoint="dropleads.companies.enrich",
            served_by="dropleads",
            cost_micro=1800,
            output=enriched.output if isinstance(enriched.output, dict) else {},
            raw=enriched.raw,
        )

    async def search_companies_by_sentence(self, filters):
        return await self.search_companies(filters)

    async def search_companies(self, filters):
        return CallResult(
            endpoint="treg.companies.search",
            served_by="hunter",
            cost_micro=0,
            output={"companies": [
                {"domain": "acme.com", "name": "Acme"},
                {"domain": "tiny.com", "name": "Tiny"},
                {"domain": "mystery.com", "name": "Mystery"},
            ]},
        )

    async def enrich(self, domain, name=""):
        payload = {
            "acme.com": {
                "name": "Acme", "domain": "acme.com", "employees": "80", "industry": "SaaS",
                "location": "Austin, Texas, United States",
                "description": "Acme sells billing software as a subscription for small teams.",
            },
            "tiny.com": {"name": "Tiny", "domain": "tiny.com", "employees": "8", "location": "Austin, Texas, United States"},
            "mystery.com": {"name": "Mystery", "domain": "mystery.com"},
        }
        return CallResult(endpoint="treg.companies.enrich", served_by="blitzapi", cost_micro=500, output=payload[domain])

    async def search_people(self, domain, limit=9, title=""):
        self.people.append(domain)
        people = []
        if domain == "acme.com":
            people = [
                {"full_name": "Jane Doe", "title": "VP Sales", "linkedin_url": "https://www.linkedin.com/in/janedoe"},
                {"full_name": "John Roe", "title": "CTO", "linkedin_url": "https://www.linkedin.com/in/johnroe"},
            ]
        return CallResult(endpoint="treg.people.search", served_by="quickenrich", cost_micro=100, output={"people": people})

    async def find_email(self, **kwargs):
        self.emails.append(kwargs["first"])
        email = "jane@acme.com" if kwargs["first"] == "Jane" else ""
        return CallResult(
            endpoint="treg.people.email.find",
            served_by="trykitt",
            cost_micro=5000,
            output={"email": email or None, "verified": False},
            treg={"verification": {"verdict": "valid"}} if email else {},
        )

    async def find_phone(self, **kwargs):
        raise AssertionError("phone lookup is off")

    async def judge_saas(self, company):
        return {"verdict": "SaaS", "reason": "sells subscription software"}


def test_enrich_asks_crawl4ai_by_domain():
    client = TregClient("token")

    async def fake_call(endpoint, body, extra_headers=None, method="POST"):
        assert endpoint == "crawl4ai.companies.enrich"
        assert method == "GET"
        assert body == {"domain": "acme.com"}
        return CallResult(endpoint=endpoint, output={"name": "Acme", "domain": "acme.com", "people_count": 80})

    client.call = fake_call
    result = asyncio.run(client.enrich("acme.com"))
    assert result.output["people_count"] == 80

    async def missing(endpoint, body, extra_headers=None, method="POST"):
        raise TregError(404, "Lead has no record of this domain")

    client.call = missing
    result = asyncio.run(client.enrich("gone.com"))
    assert result.output == {}


def test_sized_search_uses_hunter():
    client = TregClient("token")
    seen = {}

    async def fake_call(endpoint, body, extra_headers=None, method="POST", as_query=False):
        seen["endpoint"] = endpoint
        seen["body"] = body
        seen["as_query"] = as_query
        return CallResult(endpoint=endpoint, output={"organizations": []})

    client.call = fake_call
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=20)
    asyncio.run(client.search_companies_sized(filters, page=2))
    assert seen["endpoint"] == "hunter.x.discover-companies"
    assert seen["as_query"] is False
    assert seen["body"]["query"] == (
        "for-profit SaaS software companies headquartered in Texas with 51 to 200 employees"
    )
    assert seen["body"]["limit"] == 20
    assert seen["body"]["offset"] == 20


def test_dropleads_fill_keeps_a_different_company_visible():
    client = TregClient("token")

    async def fake_call(endpoint, body, extra_headers=None, method="POST"):
        assert endpoint == "dropleads.companies.enrich"
        assert body == {"domains": ["windows.com"]}
        return CallResult(
            endpoint=endpoint,
            output={"data": {"companies": [{"name": "Microsoft", "domain": "microsoft.com", "employees": 159595}]}},
        )

    client.call = fake_call
    result = asyncio.run(client.fill_company("windows.com"))
    assert result.output["domain"] == "microsoft.com"

    async def missing(endpoint, body, extra_headers=None, method="POST"):
        return CallResult(endpoint=endpoint, output={"data": {"companies": [], "notFoundDomains": ["windows.com"]}})

    client.call = missing
    result = asyncio.run(client.fill_company("windows.com"))
    assert result.output == {}


def test_street_is_looked_up_only_after_dropleads_agrees():
    filters = Filters(q="SaaS Texas", industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=20)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        def __init__(self):
            super().__init__()
            self.filled = []
            self.streets = []

        async def enrich(self, domain, name=""):
            self.streets.append(domain)
            pages = {
                "acme.com": {
                    "name": "Someone Else",
                    "domain": "acme.com",
                    "employees": "9",
                    "people_count": 9,
                    "description": "Acme sells billing software as a service",
                    "street_address": "100 Congress Ave",
                },
                "consult.com": {
                    "domain": "consult.com",
                    "description": "IT consulting services for hospitals",
                    "street_address": "1 Main St",
                },
                "swap.com": {"domain": "other.com", "street_address": "9 Other St", "employees": "80"},
                "edge.com": {
                    "domain": "edge.com",
                    "people_count": 180,
                    "description": "Edge sells subscription software",
                    "street_address": "200 Main St",
                },
                "loud.com": {
                    "domain": "loud.com",
                    "description": "Loud sells subscription software",
                    "street_address": "300 Main St",
                },
                "bay.com": {
                    "domain": "bay.com",
                    "description": "Bay sells subscription software",
                    "street_address": "1 Market St, San Francisco, CA",
                },
            }
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                served_by="crawl4ai",
                cost_micro=500,
                output=pages[domain],
            )

        async def fill_company(self, domain):
            self.filled.append(domain)
            rows = {
                "acme.com": {
                    "name": "Acme Holdings",
                    "domain": "acme.com",
                    "employees": 100,
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "phone": "+1 512-555-0100",
                    "annualRevenue": 20806000,
                    "totalFunding": 5000000,
                    "foundedYear": 2014,
                    "industry": "information technology & services",
                    "linkedinUrl": "https://www.linkedin.com/company/acme",
                    "credits": {"remainingPayg": 227021},
                },
                "tiny.com": {"domain": "tiny.com", "employees": 8, "city": "Austin", "state": "Texas"},
                "mystery.com": {"domain": "mystery.com"},
                "gone.com": {},
                "consult.com": {"domain": "consult.com", "employees": 80, "city": "Austin", "state": "Texas"},
                "surepoint.com": {"domain": "surepoint.com", "employees": 80, "city": "Cincinnati", "state": "Ohio"},
                "swap.com": {"domain": "swap.com", "employees": 90, "city": "Austin", "state": "Texas"},
                "uk.com": {
                    "domain": "uk.com", "employees": 80, "city": "Austin", "state": "Texas",
                    "phone": "+44 20 7946 0958",
                },
                "far.com": {"domain": "far.com", "employees": 800, "city": "Austin", "state": "Texas"},
                "edge.com": {"domain": "edge.com", "employees": 230, "city": "Austin", "state": "Texas"},
                "loud.com": {
                    "domain": "loud.com", "employees": 100, "city": "Austin", "state": "Texas",
                    "annualRevenue": 200000000,
                },
                "bay.com": {"domain": "bay.com", "employees": 100, "city": "Austin", "state": "Texas"},
            }
            return CallResult(
                endpoint="dropleads.companies.enrich",
                served_by="dropleads",
                cost_micro=1800,
                output=rows[domain],
            )

        async def judge_saas(self, company):
            if company.get("domain") == "consult.com":
                return {"verdict": "services", "reason": "consulting firm"}
            return {"verdict": "SaaS", "reason": "sells subscription software"}

        async def find_business(self, query):
            raise AssertionError("openmart is not part of this search")

        async def naics_for(self, domain):
            raise AssertionError("NAICS is not called")

        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", served_by="hunter", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                served_by="hunter",
                output={"companies": [
                    {"domain": "acme.com", "organization": "Acme"},
                    {"domain": "tiny.com", "organization": "Tiny"},
                    {"domain": "mystery.com", "organization": "Mystery"},
                    {"domain": "gone.com", "organization": "Gone"},
                    {"domain": "consult.com", "organization": "Consult"},
                    {"domain": "surepoint.com", "organization": "SurePoint"},
                    {"domain": "swap.com", "organization": "Swap"},
                    {"domain": "uk.com", "organization": "UK"},
                    {"domain": "far.com", "organization": "Far"},
                    {"domain": "edge.com", "organization": "Edge"},
                    {"domain": "loud.com", "organization": "Loud"},
                    {"domain": "bay.com", "organization": "Bay"},
                ]},
            )

    client = Client()
    asyncio.run(run_job(job, client, []))
    assert set(client.streets) == {"acme.com", "consult.com", "swap.com", "edge.com", "loud.com", "bay.com"}
    by_domain = {row["domain"]: row for row in job.companies}
    assert set(by_domain) == {"acme.com", "edge.com", "loud.com", "bay.com"}
    removed = {row["domain"]: row["reason"] for row in job.removed}
    assert "outside" in removed["tiny.com"]
    assert "outside" in removed["far.com"]
    assert removed["uk.com"] == "phone +44"
    assert "Texas" in removed["surepoint.com"]
    assert removed["consult.com"] == "Services: consulting firm"
    assert next(row["name"] for row in job.removed if row["domain"] == "consult.com") == "Consult"
    assert "other.com" in removed["swap.com"]
    acme = by_domain["acme.com"]
    assert acme["name"] == "Acme"
    assert acme["employees"] == "100"
    assert acme["industry"] == "SaaS"
    assert acme["phone"] == "+1 512-555-0100"
    assert acme["revenue"] == "$20.8M"
    assert acme["funding"] == "$5M"
    assert acme["city"] == "Austin"
    assert acme["state"] == "Texas"
    assert acme["founded_year"] == "2014"
    assert "100 Congress" in str(acme.get("fields"))
    assert "billing software" in acme["description"]
    assert acme["linkedin"].endswith("/acme")
    assert acme["status"] == "enriched"
    assert "ai.saas" in acme["sources"]
    assert "remainingPayg" not in str(acme.get("fields"))
    assert by_domain["edge.com"]["employees"] == "180"
    assert "per employee" in by_domain["loud.com"]["status"]
    assert "California" in by_domain["bay.com"]["status"]


def test_large_people_count_is_not_enriched():
    filters = Filters(
        q="IT companies in USA",
        industry="information technology",
        country="US",
        employees_min=11,
        employees_max=50,
        limit=10,
    )
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(
                endpoint="tomba.companies.search.reveal",
                served_by="tomba.companies.search.reveal",
                cost_micro=200,
                output={"companies": [{"domain": "realpage.com", "name": "RealPage"}]},
            )

        async def search_companies_by_sentence(self, filters):
            return await self.search_companies_sized(filters)

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="treg.companies.enrich",
                served_by="companies.enrich",
                cost_micro=1000,
                output={"name": "RealPage", "domain": domain, "employees": "11-50"},
                raw={"data": {"people_count": 4454, "industry": "Software Development", "name": "RealPage", "size": 25}},
            )

        async def search_people(self, domain, limit=9, title=""):
            self.people.append(domain)
            return CallResult(
                endpoint="treg.people.search",
                served_by="quickenrich",
                cost_micro=100,
                output={"people": [
                    {"full_name": "Jane Doe", "title": "VP Sales", "linkedin_url": "https://www.linkedin.com/in/janedoe"},
                    {"full_name": "John Roe", "title": "CTO", "linkedin_url": "https://www.linkedin.com/in/johnroe"},
                ]},
            )

    client = Client()
    asyncio.run(run_job(job, client, []))
    assert job.companies == []
    assert client.people == []
    assert any("4454" in event for event in job.events)


def test_query_filters_before_contacts():
    filters = Filters(q="SaaS Texas", industry="SaaS", country="US", state="Texas", employees_min=50, employees_max=200, limit=10)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=9, verify_emails=True, lookup_phones=False, budget_usd=5))
    client = Fake()
    asyncio.run(run_job(job, client, []))
    by_domain = {row["domain"]: row for row in job.companies}
    assert by_domain["acme.com"]["status"] == "enriched"
    assert by_domain["acme.com"]["contacts_found"] == 2
    assert by_domain["acme.com"]["emails_found"] == 1
    assert "tiny.com" not in by_domain
    assert "mystery.com" not in by_domain
    assert set(client.people) == {"acme.com"}
    assert job.contacts[0]["email_status"] == "verified"
    assert job.contacts[1]["email_status"] == "not found"
    assert job.status == "done"


def test_empty_size_search_does_not_drop_the_place():
    filters = Filters(q="SaaS companies in Texas", industry="SaaS", country="US", state="Texas", employees_min=11, employees_max=50, limit=10)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(endpoint="tomba.companies.search.reveal", served_by="tomba", cost_micro=178, output={"companies": []})

        async def search_companies(self, filters, **kwargs):
            raise AssertionError("broad search must not replace an empty sized search")

    asyncio.run(run_job(job, Client(), []))
    assert job.companies == []
    assert "No companies matched" in job.error


def test_later_page_timeout_keeps_companies_already_found():
    filters = Filters(
        q="IT companies in USA",
        industry="information technology",
        country="US",
        employees_min=11,
        employees_max=20,
        limit=5,
    )
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, budget_usd=5, verify_emails=False))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page >= 3:
                raise httpx.ReadTimeout("")
            return CallResult(
                endpoint="tomba.companies.search.reveal",
                served_by="tomba",
                cost_micro=178,
                output={"companies": [{"domain": f"firm{page}.com", "name": f"Firm {page}", "company_size": "11-50"}]},
            )

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="treg.companies.enrich",
                served_by="companies.enrich",
                cost_micro=500,
                output={
                    "name": name,
                    "domain": domain,
                    "employees": "11-50",
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "description": f"{name} builds information technology products for US businesses.",
                },
                raw={"data": {"people_count": 18, "name": name}},
            )

        async def search_companies(self, filters, **kwargs):
            raise AssertionError("a timed-out later page must not start a broader search")

    asyncio.run(run_job(job, Client(), []))
    assert job.status == "done"
    assert job.error == ""
    assert {row["domain"] for row in job.companies} == {"firm1.com", "firm2.com"}
    assert "Treg took too long to answer" in "\n".join(job.events)
    assert all(row["status"] == "enriched" for row in job.companies)
    assert all(row["industry"] == "information technology" for row in job.companies)


def test_parent_company_lookup_is_left_out():
    filters = Filters(
        q="IT companies in USA",
        industry="information technology",
        country="US",
        employees_min=11,
        employees_max=50,
        limit=10,
    )
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(
                    endpoint="tomba.companies.search.reveal",
                    served_by="tomba",
                    cost_micro=178,
                    output={"companies": []},
                )
            return CallResult(
                endpoint="tomba.companies.search.reveal",
                served_by="tomba",
                cost_micro=178,
                output={"companies": [
                    {"domain": "windows.com", "name": "Windows", "company_size": "11-50"},
                    {"domain": "statuspage.io", "name": "Statuspage", "company_size": "11-50"},
                    {"domain": "cognitoforms.com", "name": "Cognito Forms", "company_size": "11-50"},
                ]},
            )

        async def enrich(self, domain, name=""):
            if domain == "windows.com":
                output = {"name": "Microsoft", "domain": "microsoft.com", "employees": "159595"}
            elif domain == "statuspage.io":
                output = {"name": "Atlassian", "domain": "atlassian.com", "employees": "13519"}
            else:
                output = {
                    "name": name,
                    "domain": domain,
                    "employees": "46",
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "description": "Cognito Forms sells online form software for businesses.",
                }
            return CallResult(
                endpoint="treg.companies.enrich",
                served_by="companies.enrich",
                cost_micro=500,
                output=output,
            )

    asyncio.run(run_job(job, Client(), []))
    assert [row["domain"] for row in job.companies] == ["cognitoforms.com"]
    assert job.companies[0]["name"] == "Cognito Forms"
    assert job.companies[0]["employees"] == "46"
    assert job.companies[0]["status"] == "enriched"
    assert job.companies[0]["industry"] == "information technology"
    assert "ai.industry" in job.companies[0]["sources"]
    assert job.skipped == 2
    removed = {row["domain"]: row for row in job.removed}
    assert "microsoft.com" in removed["windows.com"]["reason"]
    # Parent facts must not be copied onto the seed before the drop.
    assert not (removed["windows.com"].get("employees") or "").strip()


def test_zero_contacts_skips_people_and_email():
    job = Job(
        mode="file",
        filters=Filters(),
        options=Options(industry_check=True, contacts_per_company=0, verify_emails=False, budget_usd=5),
    )
    client = Fake()
    asyncio.run(run_job(job, client, [{"domain": "acme.com", "company_name": "Acme"}]))
    assert Options.from_dict({"contacts_per_company": 0}).contacts_per_company == 0
    assert client.people == []
    assert client.emails == []
    assert job.companies[0]["contacts_found"] == 0
    assert job.companies[0]["emails_found"] == 0
    assert job.status == "done"


def test_dropped_connection_keeps_the_other_companies():
    job = Job(mode="file", filters=Filters(), options=Options(industry_check=True, verify_emails=False, budget_usd=5))

    class Client(Fake):
        async def enrich(self, domain, name=""):
            if domain == "drop.com":
                raise httpx.ReadError("")
            return await super().enrich(domain, name)

    asyncio.run(run_job(job, Client(), [
        {"domain": "drop.com", "company_name": "Drop"},
        {"domain": "acme.com", "company_name": "Acme"},
    ]))
    by_domain = {row["domain"]: row for row in job.companies}
    assert job.status == "done"
    assert job.error == ""
    assert "connection to Treg dropped" in by_domain["drop.com"]["status"]
    assert by_domain["acme.com"]["status"] == "enriched"


def test_call_retries_a_dropped_connection():
    class Transport:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ReadError("")
            return httpx.Response(200, json={"output": {"name": "Acme"}})

    async def run():
        client = TregClient("token")
        client._client = Transport()
        with patch("app.treg_client.asyncio.sleep", new=AsyncMock()):
            result = await client.call("treg.companies.enrich", {"domain": "acme.com"})
        assert result.output["name"] == "Acme"
        assert client._client.calls == 3

    asyncio.run(run())


def test_edge_band_and_foreign_phone():
    assert _headcount_band(40, 51, 200) == "edge"
    assert _headcount_band(50, 51, 200) == "edge"
    assert _headcount_band(51, 51, 200) == "edge"
    assert _headcount_band(57, 51, 200) == "edge"
    assert _headcount_band(60, 51, 200) == "edge"
    assert _headcount_band(61, 51, 200) == "inside"
    assert _headcount_band(100, 51, 200) == "inside"
    assert _headcount_band(179, 51, 200) == "inside"
    assert _headcount_band(180, 51, 200) == "edge"
    assert _headcount_band(200, 51, 200) == "edge"
    assert _headcount_band(201, 51, 200) == "edge"
    assert _headcount_band(250, 51, 200) == "edge"
    assert _headcount_band(39, 51, 200) == "outside"
    assert _headcount_band(251, 51, 200) == "outside"
    # Small bands do not use crawl4ai edge recovery.
    assert _headcount_band(16, 11, 50) == "inside"
    assert _headcount_band(43, 11, 50) == "inside"
    assert _headcount_band(8, 11, 50) == "outside"
    assert _headcount_band(60, 11, 50) == "outside"
    assert foreign_phone_reason("+44 20 7946 0958", state="Texas", country="US") == "phone +44"
    assert foreign_phone_reason("+55 11 99999 0000", state="Texas", country="US") == "phone +55"
    assert foreign_phone_reason("+91 91687 22372", state="Texas", country="US") == "phone +91"
    assert foreign_phone_reason("+1 512-555-0100", state="Texas", country="US") == ""
    assert foreign_phone_reason("+1 415-555-0100", state="Texas", country="US") == ""


def test_a_redirect_is_the_same_company(monkeypatch):
    async def final_host(domain: str) -> str:
        host = normalize_domain(domain) or ""
        if host == "dealerbuilt.com":
            return "oplogic.com"
        return host

    monkeypatch.setattr("app.pipeline._final_host", final_host)
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                served_by="hunter",
                output={"companies": [{"domain": "dealerbuilt.com", "organization": "DealerBuilt"}]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                served_by="dropleads",
                output={
                    "domain": "oplogic.com",
                    "employees": 80,
                    "city": "Houston",
                    "state": "Texas",
                    "foundedYear": 2001,
                },
            )

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                served_by="crawl4ai",
                output={
                    "domain": "oplogic.com",
                    "description": "Dealer software sold as a subscription",
                    "street_address": "100 Main St",
                },
            )

        async def judge_saas(self, company):
            return {"verdict": "SaaS", "reason": "subscription software"}

    asyncio.run(run_job(job, Client(), []))
    assert [row["domain"] for row in job.companies] == ["oplogic.com"]
    assert job.companies[0]["fields"]["searched_domain"] == "dealerbuilt.com"
    assert job.companies[0]["employees"] == "80"
    assert job.companies[0]["founded_year"] == "2001"
    assert job.companies[0]["industry"] == "SaaS"
    assert job.removed == []


def test_an_inside_edge_uses_crawl4ai_headcount():
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        def __init__(self):
            super().__init__()
            self.judged = []

        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [
                    {"domain": "skimmer.com", "organization": "Skimmer"},
                    {"domain": "tinyedge.com", "organization": "TinyEdge"},
                    {"domain": "straviso.com", "organization": "StraViso"},
                    {"domain": "brief.com", "organization": "Brief"},
                ]},
            )

        async def fill_company(self, domain):
            row = {
                "domain": domain,
                "employees": 57,
                "city": "Austin",
                "state": "Texas",
                "foundedYear": 2014.0,
                "industry": "software",
            }
            if domain == "brief.com":
                row.pop("industry")
            return CallResult(endpoint="dropleads.companies.enrich", served_by="dropleads", output=row)

        async def enrich(self, domain, name=""):
            pages = {
                "skimmer.com": {
                    "domain": domain,
                    "people_count": 80,
                    "description": "Skimmer sells pool-service software as a subscription to small businesses.",
                    "street_address": "10 Main St, Austin, TX",
                },
                "tinyedge.com": {
                    "domain": domain,
                    "people_count": 9,
                    "description": "TinyEdge sells subscription software for small teams.",
                    "street_address": "11 Main St, Austin, TX",
                },
                "straviso.com": {
                    "domain": domain,
                    "people_count": 80,
                    "description": "StraViso sells subscription software for operations teams.",
                    "street_address": "12 Main St, Austin, TX",
                },
                "brief.com": {"domain": domain, "people_count": 80, "description": "Hi", "street_address": "13 Main St"},
            }
            return CallResult(endpoint="crawl4ai.companies.enrich", output=pages[domain])

        async def judge_saas(self, company):
            self.judged.append(company.get("domain"))
            if company.get("domain") == "straviso.com":
                raise AssertionError("StraViso is on the manual review list")
            return {"verdict": "SaaS", "reason": "subscription software"}

        async def naics_for(self, domain):
            raise AssertionError("NAICS is only for unsure rows")

    client = Client()
    asyncio.run(run_job(job, client, []))
    by_domain = {row["domain"]: row for row in job.companies}
    assert set(by_domain) == {"skimmer.com", "tinyedge.com", "straviso.com", "brief.com"}
    assert by_domain["skimmer.com"]["employees"] == "80"
    assert by_domain["skimmer.com"]["founded_year"] == "2014"
    assert by_domain["skimmer.com"]["industry"] == "SaaS"
    assert "ai.saas" in by_domain["skimmer.com"]["sources"]
    assert by_domain["tinyedge.com"]["employees"] == "57"
    assert by_domain["tinyedge.com"]["industry"] == "SaaS"
    assert by_domain["tinyedge.com"]["status"].startswith("needs review")
    assert "disagree" in by_domain["tinyedge.com"]["status"]
    assert by_domain["straviso.com"]["industry"] == "needs review"
    assert by_domain["straviso.com"]["status"] == "needs review: on the manual review list"
    assert by_domain["brief.com"]["industry"] == "needs review"
    assert "the description was too short to judge" in by_domain["brief.com"]["status"]
    assert "straviso.com" not in client.judged
    assert "brief.com" not in client.judged
    assert "tinyedge.com" in client.judged
    assert job.removed == []


def test_an_incomplete_cache_is_fetched_again_once():
    facts, refetch = _cache_decision({"dropleads_full": True, "dropleads": {"employees": "57"}})
    assert facts is None and refetch is True
    facts, refetch = _cache_decision(
        {
            "dropleads_response": {"domain": "skimmer.com", "employees": 57, "foundedYear": 2014, "industry": "software"},
            "dropleads_source": "dropleads.companies.enrich",
        }
    )
    assert refetch is False
    assert facts["founded_year"] == "2014"
    assert facts["industry"] == "software"
    facts, refetch = _cache_decision(
        {
            "dropleads_response": {"domain": "skimmer.com", "employees": 57},
            "dropleads_source": "dropleads.companies.enrich",
        }
    )
    assert facts is None and refetch is True
    facts, refetch = _cache_decision(
        {
            "dropleads_response": {"domain": "skimmer.com", "employees": 57},
            "dropleads_source": "dropleads.companies.enrich",
            "dropleads_refetched": True,
        }
    )
    assert refetch is False
    assert facts["employees"] == "57"


def test_a_blank_street_page_is_fetched_again_once():
    assert _street_page_needs_fetch({}, True) is True
    assert _street_page_needs_fetch({"street_page": {"street": "", "description": ""}}, True) is True
    assert _street_page_needs_fetch(
        {"street_page": {"street": "", "description": ""}, "street_refetched": True},
        True,
    ) is False
    assert _street_page_needs_fetch(
        {"street_page": {"street": "100 Congress Ave", "description": "Hi"}},
        True,
    ) is True
    assert _street_page_needs_fetch(
        {"street_page": {"street": "100 Congress Ave", "description": "Acme sells billing software as a subscription."}},
        True,
    ) is False


def test_a_saved_industry_answer_is_reused_on_failure():
    from app.pipeline import _cached_industry_verdict, _JUDGMENT_VERSION

    current = {
        "industry_judgment": {
            "verdict": "SaaS",
            "reason": "subscription software",
            "industry": "SaaS",
            "version": _JUDGMENT_VERSION,
        }
    }
    assert _cached_industry_verdict(current, "SaaS", saas=True, current_version_only=True) == (
        "SaaS",
        "subscription software",
    )
    old = {
        "industry_judgment": {
            "verdict": "SaaS",
            "reason": "subscription software",
            "industry": "SaaS",
            "version": 1,
        }
    }
    assert _cached_industry_verdict(old, "SaaS", saas=True, current_version_only=True) == ("", "")
    assert _cached_industry_verdict(old, "SaaS", saas=True, current_version_only=False) == (
        "SaaS",
        "subscription software",
    )


def test_a_missing_saas_answer_stays_visible():
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [{"domain": "acme.com", "organization": "Acme"}]},
            )

        async def judge_saas(self, company):
            return {}

    asyncio.run(run_job(job, Client(), []))
    assert job.companies[0]["industry"] == "needs review"
    assert job.companies[0]["status"] == "needs review: the industry check did not answer"
    assert "ai.saas" not in job.companies[0]["sources"]


def test_unsure_saas_stays_for_hand_review_without_naics():
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        def __init__(self):
            super().__init__()
            self.naics = []

        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [{"domain": "maybe.com", "organization": "Maybe"}]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                output={
                    "domain": domain,
                    "employees": 100,
                    "city": "Austin",
                    "state": "Texas",
                    "industry": "information technology & services",
                    "keywords": "software, cloud",
                },
            )

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                output={
                    "domain": domain,
                    "description": "Maybe helps teams run their business online with tools and services.",
                    "street_address": "100 Congress Ave, Austin, TX",
                },
            )

        async def judge_saas(self, company):
            return {"verdict": "unsure", "reason": "no clear indication of a subscription product"}

        async def naics_for(self, domain):
            self.naics.append(domain)
            raise AssertionError("unsure rows must not spend NAICS")

    client = Client()
    asyncio.run(run_job(job, client, []))
    assert client.naics == []
    assert job.companies[0]["industry"] == "needs review"
    assert "no clear indication" in job.companies[0]["status"]
    assert job.companies[0]["fields"]["industry_reason"].startswith("no clear indication")


def test_a_hesitant_other_verdict_becomes_needs_review():
    from app.pipeline import _soften_guess

    assert _soften_guess("other", "likely a licensed product") == (
        "unsure",
        "likely a licensed product",
    )
    assert _soften_guess("services", "no clear indication of SaaS")[0] == "unsure"
    assert _soften_guess("other", "sells perpetual desktop licenses") == (
        "other",
        "sells perpetual desktop licenses",
    )


def test_out_of_state_phone_keeps_texas_street_and_reviews_missing_street():
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        def __init__(self):
            super().__init__()
            self.streets = []

        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [
                    {"domain": "kept.com", "organization": "Kept"},
                    {"domain": "review.com", "organization": "Review"},
                    {"domain": "later.com", "organization": "Later"},
                ]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                output={
                    "domain": domain,
                    "employees": 100,
                    "city": "Austin",
                    "state": "Texas",
                    "phone": "+1 206-555-0100",
                    "industry": "software",
                },
            )

        async def enrich(self, domain, name=""):
            self.streets.append(domain)
            if domain == "kept.com":
                street = "100 Congress Ave, Austin, TX 78701"
            elif domain == "later.com" and self.streets.count("later.com") >= 2:
                street = "200 Congress Ave, Austin, TX 78701"
            else:
                street = ""
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                output={
                    "domain": domain,
                    "description": f"{name} sells subscription software for sales teams across the United States.",
                    "street_address": street,
                },
            )

        async def judge_saas(self, company):
            return {"verdict": "SaaS", "reason": "subscription software"}

    client = Client()
    asyncio.run(run_job(job, client, []))
    by_domain = {row["domain"]: row for row in job.companies}
    assert by_domain["kept.com"]["status"].startswith("enriched; flagged:")
    assert "phone area code" in by_domain["kept.com"]["status"]
    assert "street is in Texas" in by_domain["kept.com"]["status"]
    assert by_domain["review.com"]["status"] == "needs review: phone is out of state and street is missing"
    assert client.streets.count("later.com") == 2
    assert by_domain["later.com"]["status"].startswith("enriched; flagged:")
    assert "street is in Texas" in by_domain["later.com"]["status"]
    assert "200 Congress" in str(by_domain["later.com"].get("fields"))


def test_missing_location_is_needs_review_for_a_place_search():
    filters = Filters(industry="SaaS", country="US", state="Texas", employees_min=51, employees_max=200, limit=5)
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [{"domain": "zeal.biz", "organization": "Zeal"}]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                output={"domain": domain, "employees": 100, "industry": "software"},
            )

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                output={
                    "domain": domain,
                    "description": "Zeal sells subscription software for operations teams across the United States.",
                    "street_address": "",
                },
            )

        async def judge_saas(self, company):
            return {"verdict": "SaaS", "reason": "subscription software"}

    asyncio.run(run_job(job, Client(), []))
    assert job.companies[0]["industry"] == "SaaS"
    assert "location was not reported" in job.companies[0]["status"]


def test_headcount_disagree_still_removes_a_clear_industry_mismatch():
    filters = Filters(
        q="IT companies in USA",
        industry="information technology",
        country="US",
        employees_min=11,
        employees_max=50,
        limit=10,
    )
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [
                    {"domain": "school.k12.va.us", "organization": "County Schools"},
                    {"domain": "soft.com", "organization": "Soft"},
                ]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                cost_micro=1800,
                output={
                    "domain": domain,
                    "employees": 16,
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "industry": "education",
                    "name": "County Schools" if "school" in domain else "Soft",
                },
            )

        async def enrich(self, domain, name=""):
            if "school" in domain:
                description = "Botetourt County Public Schools serves students across the county."
                people = 5
            else:
                description = "Soft sells managed IT and cloud software for US businesses."
                people = 28
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                cost_micro=500,
                output={"domain": domain, "people_count": people, "description": description, "street_address": "10 Main St"},
            )

        async def judge_industry(self, company, industry=""):
            if "school" in str(company.get("domain") or ""):
                return {"verdict": "no", "reason": "public school district"}
            return {"verdict": "match", "reason": "IT products"}

    asyncio.run(run_job(job, Client(), []))
    assert [row["domain"] for row in job.companies] == ["soft.com"]
    assert job.companies[0]["industry"] == "information technology"
    assert "disagree" not in job.companies[0]["status"]
    assert {row["domain"]: row["reason"] for row in job.removed}["school.k12.va.us"] == "not a for-profit company domain"


def test_an_it_search_keeps_matches_and_removes_mismatches():
    filters = Filters(
        q="IT companies in USA",
        industry="information technology",
        country="US",
        employees_min=11,
        employees_max=50,
        limit=10,
    )
    job = Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))

    class Client(Fake):
        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [
                    {"domain": "soft.com", "organization": "Soft"},
                    {"domain": "bakery.com", "organization": "Bakery"},
                ]},
            )

        async def fill_company(self, domain):
            return CallResult(
                endpoint="dropleads.companies.enrich",
                served_by="dropleads",
                cost_micro=1800,
                output={
                    "domain": domain,
                    "employees": 30,
                    "city": "Austin",
                    "state": "Texas",
                    "country": "United States",
                    "industry": "information technology & services",
                },
            )

        async def enrich(self, domain, name=""):
            pages = {
                "soft.com": {
                    "domain": "soft.com",
                    "people_count": 28,
                    "description": "Soft sells managed IT and cloud software for US businesses.",
                    "street_address": "10 Main St",
                },
                "bakery.com": {
                    "domain": "bakery.com",
                    "people_count": 28,
                    "description": "Bakery sells bread and cakes from a retail storefront.",
                    "street_address": "20 Main St",
                },
            }
            return CallResult(endpoint="crawl4ai.companies.enrich", cost_micro=500, output=pages[domain])

        async def judge_industry(self, company, industry=""):
            if company.get("domain") == "bakery.com":
                return {"verdict": "no", "reason": "retail bakery"}
            return {"verdict": "match", "reason": "IT products"}

    asyncio.run(run_job(job, Client(), []))
    assert [row["domain"] for row in job.companies] == ["soft.com"]
    assert job.companies[0]["industry"] == "information technology"
    assert "ai.industry" in job.companies[0]["sources"]
    assert {row["domain"]: row["reason"] for row in job.removed}["bakery.com"] == "Not a match: retail bakery"


def _identity_job():
    filters = Filters(industry="information technology", country="US", employees_min=11, employees_max=50, limit=10)
    return Job(mode="query", filters=filters, options=Options(industry_check=True, contacts_per_company=0, budget_usd=5))


def _identity_client(seeds, identities, dropleads):
    """seeds: Hunter domains. identities: domain -> (name, linkedin). dropleads: domain -> facts."""

    class Client(Fake):
        def __init__(self):
            super().__init__()
            self.identified = []
            self.filled = []

        async def search_companies_sized(self, filters, page: int = 1, **_extra):
            if page > 1:
                return CallResult(endpoint="hunter.x.discover-companies", output={"companies": []})
            return CallResult(
                endpoint="hunter.x.discover-companies",
                output={"companies": [{"domain": d, "organization": d.split(".")[0].title()} for d in seeds]},
            )

        async def identify_company(self, domain):
            self.identified.append(domain)
            if domain not in identities:
                return CallResult(endpoint="crustdata.companies.identify", output={}, raw=[{"matches": []}])
            name, linkedin = identities[domain]
            raw = [{"matches": [{"confidence_score": 1.0, "company_data": {"basic_info": {
                "name": name,
                "primary_domain": domain,
                "all_domains": [domain],
                "professional_network_url": linkedin,
            }}}]}]
            return CallResult(endpoint="crustdata.companies.identify", output={}, raw=raw)

        async def fill_company(self, domain):
            self.filled.append(domain)
            facts = {
                "domain": domain,
                "employees": 30,
                "city": "Austin",
                "state": "Texas",
                "country": "United States",
                **dropleads.get(domain, {}),
            }
            return CallResult(endpoint="dropleads.companies.enrich", cost_micro=1800, output=facts)

        async def enrich(self, domain, name=""):
            return CallResult(
                endpoint="crawl4ai.companies.enrich",
                cost_micro=500,
                output={"domain": domain, "description": "Managed IT services for businesses.", "street_address": "1 Main St"},
            )

        async def judge_industry(self, company, industry=""):
            return {"verdict": "match", "reason": "IT services"}

    return Client()


def test_identity_follows_the_redirect_before_buying_facts(monkeypatch):
    async def final_host(domain: str) -> str:
        host = normalize_domain(domain) or ""
        return "microsoft.com" if host == "windows.com" else host

    monkeypatch.setattr("app.pipeline._final_host", final_host)
    job = _identity_job()
    client = _identity_client(
        ["windows.com"],
        {"microsoft.com": ("Microsoft", "https://www.linkedin.com/company/microsoft")},
        {"microsoft.com": {"employees": 220000, "city": "Redmond", "state": "Washington"}},
    )
    asyncio.run(run_job(job, client, []))
    assert client.identified == ["microsoft.com"]
    assert client.filled == ["microsoft.com"]
    assert job.companies == []
    assert job.removed[0]["domain"] == "microsoft.com"
    assert job.removed[0]["name"] == "Microsoft"
    assert any("redirects to microsoft.com" in line for line in job.events)


def test_identity_linkedin_disagreement_needs_review():
    job = _identity_job()
    client = _identity_client(
        ["ciscom.com"],
        {"ciscom.com": ("CisCom Solutions", "https://www.linkedin.com/company/ciscom-solutions-llc")},
        {"ciscom.com": {"linkedin": "https://www.linkedin.com/company/some-other-ciscom"}},
    )
    asyncio.run(run_job(job, client, []))
    row = job.companies[0]
    assert row["status"].startswith("needs review: LinkedIn differs between sources")
    assert row["fields"]["identity_linkedin"].endswith("ciscom-solutions-llc")
    assert "crustdata.companies.identify" in row["sources"]


def test_identity_linkedin_fills_and_confirms():
    job = _identity_job()
    client = _identity_client(
        ["ciscom.com", "dataedo.com"],
        {
            "ciscom.com": ("CisCom Solutions", "https://www.linkedin.com/company/ciscom-solutions-llc"),
            "dataedo.com": ("Dataedo", "https://www.linkedin.com/company/dataedo"),
        },
        {"dataedo.com": {"linkedin": "https://linkedin.com/company/dataedo/"}},
    )
    asyncio.run(run_job(job, client, []))
    rows = {row["domain"]: row for row in job.companies}
    assert rows["ciscom.com"]["linkedin"].endswith("ciscom-solutions-llc")
    assert rows["ciscom.com"]["status"] == "enriched"
    assert rows["dataedo.com"]["fields"]["identity_check"] == "Crustdata and Dropleads agree on LinkedIn"
    assert rows["dataedo.com"]["status"] == "enriched"


def test_parked_domain_is_left_out_without_buying_facts(monkeypatch):
    async def final_host(domain: str) -> str:
        host = normalize_domain(domain) or ""
        return "www.hugedomains.com" if host == "apex.com" else host

    monkeypatch.setattr("app.pipeline._final_host", final_host)
    job = _identity_job()
    client = _identity_client(["apex.com"], {}, {})
    asyncio.run(run_job(job, client, []))
    assert client.filled == []
    assert client.identified == []
    assert "parked" in job.removed[0]["reason"]


def test_two_domains_for_one_company_are_kept_once(monkeypatch):
    async def final_host(domain: str) -> str:
        host = normalize_domain(domain) or ""
        return "atlassian.com" if host in {"statuspage.io", "trello.com"} else host

    monkeypatch.setattr("app.pipeline._final_host", final_host)
    job = _identity_job()
    job.options.concurrency = 1
    client = _identity_client(["statuspage.io", "trello.com"], {}, {"atlassian.com": {"employees": 40}})
    asyncio.run(run_job(job, client, []))
    assert [row["domain"] for row in job.companies] == ["atlassian.com"]
    assert job.removed[0]["reason"] == "same company as an earlier row (atlassian.com)"
    assert client.filled == ["atlassian.com"]
