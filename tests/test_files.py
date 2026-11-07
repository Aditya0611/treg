import io

from openpyxl import load_workbook

from app.export import workbook_bytes
from app.files import parse_upload


def test_xlsx_upload():
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["company_domain", "company_name"])
    sheet.append(["https://www.linear.app/home", "Linear"])
    buffer = io.BytesIO()
    book.save(buffer)
    rows, column, skipped = parse_upload("list.xlsx", buffer.getvalue())
    assert column == "company_domain"
    assert rows == [{"domain": "linear.app", "company_name": "Linear"}]
    assert skipped == 0


def test_csv_upload():
    raw = "company_name,website\nStripe,https://www.stripe.com\nStripe,stripe.com\nNope,not a domain\n".encode()
    rows, column, skipped = parse_upload("list.csv", raw)
    assert column == "website"
    assert rows == [{"domain": "stripe.com", "company_name": "Stripe"}]
    assert skipped == 2


def test_workbook_keeps_every_returned_field():
    payload = workbook_bytes(
        [{
            "domain": "acme.com",
            "name": "Acme",
            "status": "enriched",
            "sources": ["companies.enrich:blitzapi"],
            "cost_micro": 500,
            "contacts_found": 1,
            "emails_found": 1,
            "fields": {"revenue_range": "$10M", "tech_stack.1": "postgres"},
        }],
        [{
            "company": "Acme",
            "domain": "acme.com",
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@acme.com",
            "email_status": "verified",
            "sources": ["email.find:trykitt"],
            "cost_micro": 5000,
            "fields": {"seniority": "vp"},
        }],
    )
    book = load_workbook(io.BytesIO(payload))
    headers = [cell.value for cell in book["Companies"][1]]
    assert book.sheetnames == ["Companies", "Contacts", "Removed", "Accuracy", "Summary"]
    assert [cell.value for cell in book["Accuracy"][1]][0] == "Domain"
    accuracy_headers = [cell.value for cell in book["Accuracy"][1]]
    assert "Correct" in accuracy_headers
    assert book["Accuracy"].cell(2, accuracy_headers.index("Domain") + 1).value == "acme.com"
    assert book["Accuracy"].cell(2, accuracy_headers.index("Correct") + 1).value in (None, "")
    assert book["Removed"].cell(1, 3).value == "Reason"
    assert "Cost" in [cell.value for cell in book["Removed"][1]]
    assert book["Companies"].cell(4, 1).value is None
    assert book["Removed"].cell(3, 1).value is None
    assert book["Summary"]["A1"].value == "Kept"
    assert book["Summary"]["B1"].value == "$0.0005"
    assert book["Summary"]["A2"].value == "Removed"
    assert book["Summary"]["B2"].value == "$0.0000"
    assert book["Summary"]["A3"].value == "Total"
    assert book["Summary"]["B3"].value == "$0.0005"
    assert book["Companies"].cell(2, headers.index("Contacts Found") + 1).value == 1
    assert "Revenue Range" in headers
    assert "Tech Stack / 1" in headers
    assert book["Companies"].cell(2, headers.index("Domain") + 1).value == "acme.com"
    assert book["Companies"].cell(2, headers.index("Cost") + 1).value == 0.0005
    contact_headers = [cell.value for cell in book["Contacts"][1]]
    assert "Seniority" in contact_headers
    assert book["Contacts"].cell(2, contact_headers.index("Email Status") + 1).value == "verified"


def test_workbook_writes_founded_year_without_decimals():
    payload = workbook_bytes(
        [{
            "domain": "stata.com",
            "name": "Stata",
            "founded_year": "1992.0",
            "status": "enriched",
            "sources": [],
            "cost_micro": 0,
            "contacts_found": 0,
            "emails_found": 0,
        }],
        [],
    )
    book = load_workbook(io.BytesIO(payload))
    headers = [cell.value for cell in book["Companies"][1]]
    assert book["Companies"].cell(2, headers.index("Founded Year") + 1).value == "1992"


def test_workbook_labels_revenue_and_drops_repeated_columns():
    payload = workbook_bytes(
        [{
            "domain": "ssl.com",
            "name": "SSL",
            "industry": "Software Development",
            "employees": "57",
            "revenue": "$1M-$10M",
            "description": "Certificates",
            "city": "Houston",
            "fields": {
                "data.name": "SSL Corp",
                "data.city": "Houston",
                "data.industry": "Software Development",
                "data.description": "Certificates",
                "data.people_count": "57",
                "data.lang": "en",
                "data.mail_provider": "Google",
                "meta.took_ms": "120",
                "data.socials.crunchbase": "https://www.crunchbase.com/organization/ssl",
                "data.socials.github": "ssl-com",
                "data.socials.instagram": "sslcom",
                "credits.remainingPayg": "227021",
                "credits.transactionId": "abc",
                "data.companies.1.keywords": "a long keyword list",
            },
        }],
        [],
    )
    book = load_workbook(io.BytesIO(payload))
    headers = [cell.value for cell in book["Companies"][1]]
    assert "Revenue (estimate)" in headers
    for gone in (
        "Data / Name", "Data / Industry", "Data / Description", "Data / People Count", "Data / City",
        "Data / Lang", "Data / Mail Provider", "Meta / Took Ms",
        "Data / Socials / Crunchbase", "Data / Socials / Github", "Data / Socials / Instagram",
        "Credits / Remaining Payg", "Credits / Transaction Id", "Data / Companies / 1 / Keywords",
    ):
        assert gone not in headers
