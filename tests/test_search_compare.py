import asyncio

from app.search_agents import PRESETS, SEARCH_AGENTS, read_hits, tomba_sizes
from app.search_judge import judge, outcome, score, size_verdict
from app.treg_client import CallResult


def test_tomba_sizes_cover_the_asked_range():
    assert tomba_sizes(50, 750) == ["51-250", "251-1K"]
    assert tomba_sizes(11, 50) == ["11-50"]


def test_size_verdict_from_linkedin_ranges():
    assert size_verdict("51-200", 50, 750) == "pass"
    assert size_verdict("201-500", 50, 750) == "pass"
    assert size_verdict("501-1000", 50, 750) == "edge"
    assert size_verdict("11-50", 50, 750) == "fail"
    assert size_verdict("10001+", 50, 750) == "fail"
    assert size_verdict("myself only", 11, 50) == "fail"
    assert size_verdict("", 11, 50) == "unknown"


def test_requests_carry_the_spec():
    spec = PRESETS["metal"]
    apollo = SEARCH_AGENTS["apollo.companies.search"].request(spec, 25)
    assert apollo["organization_num_employees_ranges[]"] == ["50,750"]
    assert "Canada" in apollo["organization_locations[]"]
    assert "CNC machining" in apollo["q_organization_keyword_tags[]"]
    tomba = SEARCH_AGENTS["tomba.companies.search.reveal"].request(spec, 25)
    assert tomba["filters"]["size"]["include"] == ["51-250", "251-1K"]
    assert "DE" in tomba["filters"]["location_country"]["include"]
    assert SEARCH_AGENTS["exa.companies.search"].price(25) == 0.007 + 0.015


def test_read_hits_for_each_answer_shape():
    apollo = {"organizations": [{"name": "Acme Fab", "primary_domain": "acmefab.com", "linkedin_url": "http://www.linkedin.com/company/acme-fab"}]}
    assert read_hits("apollo.companies.search", apollo, apollo)[0]["domain"] == "acmefab.com"
    exa = {"results": [{"url": "https://www.steelco.de/", "title": "SteelCo", "entities": [{"properties": {"name": "SteelCo GmbH"}}]}]}
    assert read_hits("exa.companies.search", exa, exa)[0] == {"domain": "steelco.de", "name": "SteelCo GmbH", "linkedin": "", "reported": {}}
    parallel = {"entities": [{"name": "Weld Inc", "url": "https://www.linkedin.com/company/weld-inc", "description": "Welding"}]}
    hit = read_hits("parallel.companies.search", parallel, parallel)[0]
    assert hit["domain"] == "" and "weld-inc" in hit["linkedin"]


def test_judge_and_score(monkeypatch):
    async def industry(company, asked, *, saas):
        return ("match", "CNC job shop", "") if "machin" in company["description"] else ("no", "software", "")

    monkeypatch.setattr("app.pipeline._llm_industry", industry)

    class Client:
        async def identify_companies(self, domains):
            data = {
                "shop.com": ("Shop Machining", "51-200", "Precision CNC machining job shop"),
                "soft.com": ("Soft", "51-200", "Accounting software"),
                "huge.com": ("Huge", "10001+", "Machining conglomerate"),
            }
            rows = [
                {"matched_on": d, "matches": [{"confidence_score": 1.0, "company_data": {"basic_info": {
                    "name": data[d][0], "primary_domain": d, "all_domains": [d], "employee_count_range": data[d][1],
                    "description": data[d][2], "professional_network_url": f"https://www.linkedin.com/company/{d[:-4]}",
                }}}]}
                for d in domains if d in data
            ]
            return CallResult(endpoint="crustdata.companies.identify", output={}, raw=rows)

        async def identify_linkedin(self, url):
            return CallResult(endpoint="crustdata.companies.identify", output={}, raw=[])

    hits = {d: {"domain": d, "name": "", "linkedin": ""} for d in ("shop.com", "soft.com", "huge.com", "nobody.com")}
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as folder:
        verdicts = asyncio.run(judge(Client(), hits, PRESETS["metal"], Path(folder) / "judged.json", log=lambda _m: None))
    assert outcome(verdicts["shop.com"]) == "fits"
    assert outcome(verdicts["soft.com"]) == "wrong"
    assert outcome(verdicts["huge.com"]) == "wrong"
    assert outcome(verdicts["nobody.com"]) == "unclear"
    rows = score(
        {"a": [hits["shop.com"], hits["soft.com"]], "b": [hits["huge.com"], hits["nobody.com"]]},
        verdicts,
        {"a": 0.01, "b": 0.0},
    )
    assert rows[0]["agent"] == "a"
    assert rows[0]["precision"] == 0.5
    assert rows[0]["only_here"] == 1
    assert rows[1]["fits"] == 0
