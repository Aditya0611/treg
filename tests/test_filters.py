from app.filters import Filters, filters_from_dict, finalize_filters, hunter_sentence, llm_target, parse_query_rules, search_industries


def test_groq_key_uses_groq_endpoint():
    url, model = llm_target("gsk_example")
    assert url == "https://api.groq.com/openai/v1"
    assert model == "openai/gpt-oss-120b"
    assert llm_target("sk-example")[0] == "https://api.openai.com/v1"


def test_saas_uses_tomba_industries():
    assert "computer software" in search_industries("SaaS")
    assert "SaaS" not in search_industries("SaaS")


def test_texas_saas_query():
    filters = parse_query_rules("SaaS companies in Texas, 50-200 employees")
    assert filters.industry == "SaaS"
    assert filters.state == "Texas"
    assert filters.country == "US"
    assert filters.city == ""
    assert filters.employees_min == 51
    assert filters.employees_max == 200
    assert "11-50" in filters.note
    assert "SaaS" in filters.q
    assert filters.limit == 100


def test_city_and_word_range():
    filters = parse_query_rules("B2B SaaS startups based in Austin, Texas, 100 to 500 employees")
    assert filters.city == "Austin"
    assert filters.state == "Texas"
    assert filters.country == "US"
    assert filters.industry == "B2B SaaS"
    assert filters.employees_min == 100
    assert filters.employees_max == 500


def test_it_companies_in_usa_with_a_headcount():
    filters = parse_query_rules("IT companies in USA having 20 employees")
    assert filters.industry == "information technology"
    assert "IT companies in USA" in filters.q
    assert filters.country == "US"
    assert filters.state == ""
    assert filters.employees_min == 11
    assert filters.employees_max == 50
    assert "11-50" in filters.note


def test_llm_draft_uses_the_size_band_and_state_name():
    rules = parse_query_rules("IT companies of the USA having 20 employees")
    draft = Filters(industry="IT", country="US", state="TX", employees_min=20, employees_max=20, q="IT companies USA")
    filters = finalize_filters(draft, rules)
    assert filters.parser == "llm"
    assert filters.industry == "information technology"
    assert filters.state == "Texas"
    assert filters.country == "US"
    assert filters.employees_min == 11
    assert filters.employees_max == 50
    assert "11-50" in filters.note


def test_saved_form_does_not_keep_the_band_edge():
    filters = filters_from_dict({"industry": "SaaS", "country": "US", "state": "Texas", "employees_min": 50, "employees_max": 200})
    assert filters.employees_min == 51
    assert filters.employees_max == 200


def test_explicit_range_is_not_rounded_down():
    rules = parse_query_rules("SaaS companies in Texas, 51-200 employees")
    draft = Filters(industry="SaaS", country="US", state="Texas", employees_min=50, employees_max=200, q="SaaS")
    filters = finalize_filters(draft, rules)
    assert filters.employees_min == 51
    assert filters.employees_max == 200


def test_llm_draft_keeps_a_range():
    rules = parse_query_rules("SaaS companies in Texas, 50-200 employees")
    draft = Filters(industry="SaaS", country="US", state="Texas", employees_min=50, employees_max=200, q="SaaS companies in Texas")
    filters = finalize_filters(draft, rules)
    assert filters.industry == "SaaS"
    assert filters.employees_min == 51
    assert filters.employees_max == 200


def test_it_companies_of_usa():
    filters = parse_query_rules("it companies of usa having 20 employees")
    assert filters.industry == "information technology"
    assert filters.country == "US"
    assert filters.state == ""
    assert filters.employees_min == 11
    assert filters.employees_max == 50


def test_it_companies_of_the_usa():
    filters = parse_query_rules("IT companies of the USA having 20 employees")
    assert filters.industry == "information technology"
    assert filters.country == "US"


def test_hunter_sentence_names_the_place_and_headcount():
    filters = Filters(industry="SaaS", state="Texas", country="US", employees_min=51, employees_max=200)
    assert hunter_sentence(filters) == (
        "for-profit SaaS software companies headquartered in Texas with 51 to 200 employees"
    )


def test_hunter_sentence_uses_size_band_for_exact_bands():
    filters = Filters(industry="information technology", country="US", employees_min=11, employees_max=50)
    assert hunter_sentence(filters) == (
        "for-profit information technology companies headquartered in the United States with 11-50 employees"
    )


def test_country_and_limit():
    filters = parse_query_rules("top 40 fintech companies in Germany under 50 employees")
    assert filters.country == "DE"
    assert filters.industry == "fintech"
    assert filters.employees_max == 50
    assert filters.employees_min is None
    assert filters.limit == 40
