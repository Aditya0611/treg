import asyncio

from app.identity import brand, crustdata_identity, identity_mismatch, locked_domain, redirect_identity
from app.treg_client import CallResult, TregClient


def test_locked_domain_strips_www():
    assert locked_domain("https://www.Rebilly.com/path") == "rebilly.com"


def test_identity_mismatch_catches_parent_brand():
    assert identity_mismatch("windows.com", "microsoft.com")
    assert identity_mismatch("windows.com", "windows.com") == ""
    assert identity_mismatch("windows.com", "") == ""


def test_brand_ignores_country_and_subdomain():
    assert brand("acme.com") == "acme"
    assert brand("shop.acme.co.uk") == "acme"
    assert brand("acme.de") == "acme"


def test_redirect_to_another_brand_moves_the_row():
    assert redirect_identity("windows.com", "microsoft.com") == ("microsoft.com", "")


def test_redirect_within_the_same_company_keeps_the_searched_domain():
    assert redirect_identity("acme.com", "acme.de") == ("acme.com", "")
    assert redirect_identity("acme.com", "www.acme.com") == ("acme.com", "")
    assert redirect_identity("acme.com", "eu.acme.com") == ("acme.com", "")
    assert redirect_identity("acme.com", "acme.wixsite.com") == ("acme.com", "")
    assert redirect_identity("acme.com", "linkedin.com") == ("acme.com", "")


def test_redirect_to_a_domain_marketplace_is_parked():
    owner, parked = redirect_identity("apex.com", "www.hugedomains.com")
    assert owner == "apex.com"
    assert "parked" in parked


def _crust(name, primary, domains, linkedin, confidence=1.0):
    return [{
        "matched_on": primary,
        "matches": [{
            "confidence_score": confidence,
            "company_data": {"basic_info": {
                "name": name,
                "primary_domain": primary,
                "all_domains": domains,
                "professional_network_url": linkedin,
                "employee_count_range": "11-50",
            }},
        }],
    }]


def test_crustdata_identity_reads_the_top_match():
    found = crustdata_identity(
        _crust("CisCom Solutions", "ciscom.com", ["ciscom.com"], "https://www.linkedin.com/company/ciscom-solutions-llc"),
        "ciscom.com",
    )
    assert found["name"] == "CisCom Solutions"
    assert found["linkedin"].endswith("ciscom-solutions-llc")
    assert found["employees"] == "11-50"


def test_crustdata_identity_ignores_weak_or_off_domain_matches():
    weak = _crust("Someone", "ciscom.com", ["ciscom.com"], "https://www.linkedin.com/company/x", confidence=0.4)
    assert crustdata_identity(weak, "ciscom.com") == {}
    other = _crust("Other", "other.com", ["other.com"], "https://www.linkedin.com/company/x")
    assert crustdata_identity(other, "ciscom.com") == {}
    assert crustdata_identity({}, "ciscom.com") == {}


def test_identify_company_calls_crustdata(monkeypatch):
    client = TregClient(token="t")
    client.identify_interval_s = 0
    seen = []

    async def call(endpoint, body, extra_headers=None, **_kw):
        seen.append((endpoint, body))
        return CallResult(endpoint=endpoint, output={})

    monkeypatch.setattr(client, "call", call)
    asyncio.run(client.identify_company("ciscom.com"))
    assert seen == [("crustdata.companies.identify", {"domains": ["ciscom.com"]})]
