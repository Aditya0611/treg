from app.normalize import (
    clean_company_name,
    clean_text,
    described_text,
    company_from_payload,
    email_status,
    employee_bounds,
    flatten_record,
    location_mismatch,
    normalize_domain,
    outside_employee_range,
    people_from_payload,
    pick_headcount,
    reported_headcount,
    size_evidence,
    naics_codes,
    naics_mismatch,
    software_mismatch,
    us_employee_count,
    split_location,
    tidy_company,
    tidy_street,
)


def test_hunter_rows_are_a_name_and_a_domain():
    from app.pipeline import companies_from_search

    rows = companies_from_search(
        {"data": [{"domain": "centerbase.com", "organization": "Centerbase"}], "meta": {"results": 1}},
        None,
    )
    assert rows[0]["domain"] == "centerbase.com"
    assert rows[0]["company_name"] == "Centerbase"


def test_nested_dropleads_fields_fill_the_standard_columns():
    company = {
        "domain": "4js.com",
        "name": "Genero software development platform low-code no-code",
        "website": "https://4js.com",
        "fields": {
            "data.companies.1.industry": "information technology & services",
            "data.companies.1.linkedinUrl": "http://www.linkedin.com/company/four-j's-development-tools",
            "data.companies.1.phone": "'+1 972-893-7300",
            "data.companies.1.state": "Texas",
            "data.companies.1.country": "United States",
            "data.companies.1.foundedYear": "1992",
            "data.companies.1.annualRevenue": "20806000",
            "data.companies.1.city": "Irving",
            "data.companies.1.employees": "110",
            "credits.remainingPayg": "227021",
            "data.companies.1.keywords": "low code development platform",
        },
    }
    tidy_company(company, headcount=110)
    assert company["industry"] == "information technology & services"
    assert company["state"] == "Texas"
    assert company["phone"] == "+1 972-893-7300"
    assert company["founded_year"] == "1992"
    assert company["revenue"] == "$20.8M"
    assert company["linkedin"].endswith("/four-j's-development-tools")
    assert company["name"] == "Four J's Development Tools"
    assert company["city"] == "Irving"


def test_naics_keeps_a_software_publisher_and_drops_consulting():
    rows = naics_codes({"codes": [{"code": "541512", "name": "Computer Systems Design Services", "confidence": "high"}]})
    assert naics_mismatch("SaaS", rows).endswith("is not a software publisher")
    assert naics_mismatch("SaaS", [("513210", "Software Publishers")]) == ""
    assert us_employee_count({"countries": [{"country": "United States", "employee_count": 86}, {"country": "India", "employee_count": 40}]}) == 86


def test_apollo_row_keeps_its_headcount_revenue_and_phone():
    payload = company_from_payload(
        {
            "name": "Acme",
            "primary_domain": "acme.com",
            "estimated_num_employees": 120,
            "organization_revenue": 0,
            "organization_revenue_printed": "$2.4M",
            "founded_year": 2014,
            "linkedin_url": "http://www.linkedin.com/company/acme",
            "primary_phone": {"number": "+1 512-555-0100", "sanitized_number": "+15125550100"},
            "state": "Texas",
            "country": "United States",
        },
        {},
    )
    assert payload["domain"] == "acme.com"
    assert payload["employees"] == "120"
    assert payload["revenue"] == "$2.4M"
    assert payload["phone"] == "+1 512-555-0100"
    assert payload["founded_year"] == "2014"
    assert payload["state"] == "Texas"


def test_apollo_primary_domain_is_the_company_domain():
    payload = company_from_payload({"name": "Acme", "primary_domain": "acme.com"}, {})
    assert payload["domain"] == "acme.com"


def test_consulting_firm_is_not_saas():
    reason = software_mismatch(
        "SaaS",
        "information technology & services",
        "Virtasant",
        "virtasant.com",
        "managed cloud services and it consulting",
    )
    assert "not a software" in reason


def test_dropleads_linkedin_and_headcount():
    company = company_from_payload(
        {
            "name": "Centerbase",
            "domain": "centerbase.com",
            "employees": 140,
            "city": "Dallas",
            "state": "Texas",
            "linkedinUrl": "http://www.linkedin.com/company/centerbase",
        },
        None,
        domain="centerbase.com",
    )
    assert company["employees"] == "140"
    assert company["city"] == "Dallas"
    assert company["linkedin"] == "http://www.linkedin.com/company/centerbase"


def test_search_rows_use_website_url():
    from app.pipeline import companies_from_search

    rows = companies_from_search(
        {"companies": []},
        {"data": {"companies": [{"name": "Google", "website_url": "https://google.com", "company_size": "11-50", "industry": "Information Technology and Services"}]}},
    )
    assert rows[0]["domain"] == "google.com"
    assert rows[0]["preview"]["employees"] == "11-50"


def test_domain():
    assert normalize_domain("https://www.Stripe.com/about") == "stripe.com"
    assert normalize_domain("Ada@Notion.so") == "notion.so"
    assert normalize_domain("not a domain") is None


def test_people_count_is_headcount():
    company = company_from_payload(
        {"name": "RealPage", "domain": "realpage.com"},
        {"data": {"industry": "Software Development", "people_count": 4454, "linkedin": "realpage", "name": "RealPage"}},
    )
    assert company["employees"] == "4454"
    assert outside_employee_range(company["employees"], 11, 50) is True
    assert pick_headcount("11-50", 4454) == "4454"
    assert pick_headcount(0, "11-50") == "11-50"
    assert reported_headcount("11-50", {"data.people_count": 180, "data.size": 25}) == "180"
    assert reported_headcount("5000", {"data.people_count": 30}) == "30"
    assert outside_employee_range("180", 11, 50) is True
    assert outside_employee_range("30", 11, 50) is False
    assert outside_employee_range("4", 11, 50) is True
    assert size_evidence("11-50", {"data.people_count": 0}) == (None, "blank")
    assert size_evidence("11-50", {"data.people_count": 25}) == (25, "count")
    assert size_evidence("80", {}) == (80, "count")
    assert size_evidence("", {}) == (None, "unknown")
    assert outside_employee_range("49", 11, 50) is False


def test_employee_window():
    assert employee_bounds("51-200") == (51, 200)
    assert employee_bounds("1K-5K") == (1000, 5000)
    assert employee_bounds("251-1K") == (251, 1000)
    assert outside_employee_range("1K-5K", 11, 50) is True
    assert outside_employee_range("11-50", 11, 50) is False
    assert outside_employee_range("11-50", 50, 200) is False
    assert outside_employee_range("1-10", 50, 200) is True
    assert outside_employee_range("1000-5000", 50, 200) is True
    assert outside_employee_range("", 50, 200) is False
    assert outside_employee_range("120", 50, 200) is False


def test_flatten_keeps_deeper_treg_fields():
    raw = {"data": {"company": {"technologies": [{"name": "Postgres"}, {"name": "Redis"}]}}}
    flat = flatten_record(raw)
    assert flat["data.company.technologies.1.name"] == "Postgres"
    assert flat["data.company.technologies.2.name"] == "Redis"


def test_company_fields_from_output_and_raw():
    company = company_from_payload(
        {"name": "Acme", "domain": "acme.com", "industry": "SaaS", "employees": "80", "location": "Austin, Texas, United States", "linkedin_url": "https://www.linkedin.com/company/acme"},
        {"revenue": "$10M", "phone": "+1-512-555-0100", "founded_year": 2014},
    )
    assert company["name"] == "Acme"
    assert company["revenue"] == "$10M"
    assert company["phone"] == "+1-512-555-0100"
    assert company["founded_year"] == "2014"
    assert company["city"] == "Austin"
    assert company["state"] == "Texas"
    assert company["country"] == "United States"
    assert company["linkedin"].startswith("https://")


def test_location_filter():
    texas = {"city": "Austin", "state": "Texas", "country": "United States", "country_code": "US", "location": "Austin, Texas, United States"}
    california = {"city": "San Francisco", "state": "California", "country": "United States", "country_code": "US", "location": ""}
    city_only = {"city": "Austin", "state": "", "country": "", "country_code": "", "location": "Austin"}
    assert location_mismatch(texas, state="Texas", country="US") == ""
    assert "California" not in location_mismatch(texas, state="Texas")
    assert location_mismatch(california, state="Texas") != ""
    assert location_mismatch(city_only, state="Texas") == ""
    assert location_mismatch({"city": "", "state": "", "country": "", "country_code": "", "location": ""}, state="Texas") == ""


def test_people_and_email_status():
    people = people_from_payload(
        {"people": [{"person": {"full_name": "Jane Doe", "title": "VP Sales", "linkedin_url": "linkedin.com/in/janedoe"}}]},
        None,
    )
    assert people[0]["first_name"] == "Jane"
    assert people[0]["last_name"] == "Doe"
    assert people[0]["linkedin"].startswith("https://")
    assert email_status({"email": "jane@acme.com", "verified": False}, {"verification": {"verdict": "valid"}}) == ("jane@acme.com", "verified")
    assert email_status({"email": "jane@acme.com"}, {}) == ("jane@acme.com", "found")
    assert email_status({}, {}) == ("", "not found")


def test_split_location():
    assert split_location("London, United Kingdom")[2] == "GB"


def test_company_name_drops_the_page_title():
    assert clean_company_name("Real Geeks - Real Estate CRM and Website solution.", "realgeeks.com") == "Real Geeks"
    assert clean_company_name("OptiSigns Digital Signage Software - Elevate Your Business", "optisigns.com") == "OptiSigns"
    assert clean_company_name("Centerbase - Centerbase", "centerbase.com") == "Centerbase"
    assert clean_company_name(
        "Workforce Intelligence",
        "sapienceanalytics.com",
        "https://www.linkedin.com/company/sapience-analytics",
    ) == "Sapience Analytics"


def test_blank_street_is_filled_from_the_matching_address():
    company = {
        "domain": "coder.com",
        "name": "Coder",
        "city": "Austin",
        "state": "Texas",
        "postal_code": "78748",
        "fields": {
            "data.address": "9901 Brodie Lane Suite 160 #1212, 78748-5892, Austin, TX",
            "data.city": "Austin",
        },
    }
    tidy_company(company, headcount=147)
    assert company["fields"]["street_address"] == "9901 Brodie Lane Suite 160 #1212"
    assert "data.address" not in company["fields"]


def test_tidy_company_repairs_place_size_and_revenue():
    company = {
        "domain": "fieldpulse.com",
        "name": "FieldPulse",
        "industry": "Software Development",
        "employees": "182",
        "revenue": "$0-$1M",
        "description": "FieldPulse",
        "city": "texas",
        "state": "Texas",
        "country": "United States",
        "linkedin": "https://www.linkedin.com/company/fieldpulse",
        "fields": {
            "company_size": "11-50",
            "data.city": "Dallas",
            "postal_code": "suite 200",
            "street_address": "-96.80",
            "data.address": "8144 Walnut Hill Ln, 75231, Dallas, TX",
            "founded": 2015.0,
            "founded_year": "2015",
        },
    }
    tidy_company(company, headcount=182)
    assert company["city"] == "Dallas"
    assert company["description"] == ""
    assert company["revenue"] == "$0-$1M"
    assert company["postal_code"] == "75231"
    assert company["fields"]["street_address"] == "8144 Walnut Hill Ln"
    assert "data.address" not in company["fields"]
    assert company["fields"]["company_size"] == "51-250"
    assert clean_text(2015.0) == "2015"
    assert clean_text("2004.0") == "2004"
    assert tidy_street("3100 richmond") == "3100 Richmond"
    assert tidy_street("3800 n lamar blvd") == "3800 N Lamar Blvd"
    assert tidy_street("3801 s capital of texas hwy") == "3801 S Capital of Texas Hwy"
    kept = {
        "name": "CareerPlug",
        "city": "Austin",
        "state": "Texas",
        "postal_code": "78746",
        "fields": {"street_address": "3801 S Capital of Texas Hwy"},
    }
    tidy_company(kept, headcount=125)
    assert kept["fields"]["street_address"] == "3801 S Capital of Texas Hwy"
    numbered = {
        "name": "FileCloud",
        "city": "Austin",
        "state": "Texas",
        "postal_code": "78750",
        "fields": {"street_address": "13785 research blvd"},
    }
    tidy_company(numbered, headcount=96)
    assert numbered["fields"]["street_address"] == "13785 Research Blvd"
    moved = {
        "name": "Topaz Labs",
        "city": "Addison",
        "state": "Texas",
        "postal_code": "75001",
        "fields": {
            "street_address": "14555 dallas pkwy",
            "data.address": "5100 Belt Line Rd Ste 930, 75254, Addison, TX",
        },
    }
    tidy_company(moved, headcount=69)
    assert moved["fields"]["street_address"] == "5100 Belt Line Rd Ste 930"
    assert moved["postal_code"] == "75254"
    houston = {
        "name": "Graylog",
        "city": "Houston",
        "state": "Texas",
        "postal_code": "77002",
        "fields": {
            "street_address": "4845 Pearl East Circle",
            "data.address": "1301 Fannin St, Ste. 2000",
        },
    }
    tidy_company(houston, headcount=107)
    assert houston["fields"]["street_address"] == "1301 Fannin St, Ste. 2000"
    assert houston.get("postal_code", "") == ""
    moved_zip = {
        "name": "ActiveProspect",
        "city": "Austin",
        "state": "Texas",
        "postal_code": "78751",
        "fields": {
            "street_address": "4015 Guadalupe St",
            "data.address": "5900 Balcones Drive, Suite 100",
        },
    }
    tidy_company(moved_zip, headcount=178)
    assert moved_zip["fields"]["street_address"] == "5900 Balcones Drive, Suite 100"
    assert moved_zip.get("postal_code", "") == ""
    place_only = {
        "name": "Wander",
        "city": "Austin",
        "state": "Texas",
        "fields": {"street_address": "Austin, Texas, United States", "description": "Never book a bad vacation home again."},
    }
    tidy_company(place_only, headcount=131)
    assert "street_address" not in place_only["fields"]
    assert software_mismatch(
        "SaaS",
        "Technology, Information and Internet",
        "Wander",
        "wander.com",
        described_text(place_only),
    )
    austin = {"name": "Liquibase", "state": "Texas", "postal_code": "78765", "city": "", "fields": {}}
    tidy_company(austin, headcount=70)
    assert austin["city"] == "Austin"
    assert location_mismatch(
        {"city": "Paris", "state": "Texas", "country": "United States", "location": "Paris France", "fields": {}},
        state="Texas",
        country="US",
    )
    assert software_mismatch("SaaS", "Staffing and Recruiting", "Talroo", "talroo.com")
    assert software_mismatch("SaaS", "Public Safety", "Asset Panda", "assetpanda.com")
    assert software_mismatch(
        "SaaS",
        "Technology, Information and Internet",
        "Wander",
        "wander.com",
        "Never book a bad vacation home again.",
    )
    assert tidy_street("collin mckinney pkwy") == "Collin McKinney Pkwy"
    assert software_mismatch("SaaS", "Landscaping Services", "LawnStarter", "lawnstarter.com")
    assert software_mismatch("SaaS", "Technology, Information and Internet", "Shoebacca", "shoebacca.com")
    assert software_mismatch("SaaS", "Software Development", "FieldPulse", "fieldpulse.com") == ""
