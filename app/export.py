"""Write the Companies, Contacts, Removed, Accuracy, and Summary sheets."""

import random
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from app.normalize import _field_leaf, _year, clean_text

_COMPANY_FIRST = (
    "domain", "name", "website", "linkedin", "industry", "employees", "revenue", "funding",
    "founded_year", "phone", "city", "state", "country", "location", "description",
)
_CONTACT_FIRST = (
    "company", "domain", "first_name", "last_name", "title", "linkedin",
    "email", "email_status", "phone",
)
_COMPANY_LAST = ("contacts_found", "emails_found", "linkedin_check", "status", "sources", "cost")
_CONTACT_LAST = ("sources", "cost")
_REMOVED_FIRST = ("domain", "name", "reason", "employees", "phone", "city", "state", "website", "cost")
_ACCURACY_FIRST = ("domain", "name", "website", "linkedin", "employees", "city", "state", "correct", "note")
_HIDDEN = {"fields", "cost_micro", "index", "preview", "search_fields"}
_DUPLICATES = {
    "founded_year": {"founded", "year_founded", "founded_year"},
    "linkedin": {"linkedin", "linkedin_url"},
    "website": {"website", "website_url", "company_website"},
    "name": {"name", "organization"},
    "domain": {"domain", "primary_domain"},
    "phone": {"phone", "sanitized_phone", "primary_phone"},
    "state": {"state"},
    "country": {"country"},
    "revenue": {"revenue", "annual_revenue", "organization_revenue", "organization_revenue_printed"},
    "city": {"city"},
    "industry": {"industry"},
    "description": {"description"},
    "employees": {
        "people_count", "employee_count", "headcount", "total_employees", "total_employees_exact",
        "employees", "estimated_num_employees",
    },
}
_DROP_LEAVES = {
    "took_ms", "lang", "mail_provider", "mysqlid", "mysql_id", "score", "syncedat", "synced_at",
    "crunchbase", "github", "instagram", "pinterest", "tiktok", "hiring_system",
    "keywords", "technologynames", "technology_names",
    "alexa_ranking", "logo_url", "num_languages", "num_retail_locations", "linkedin_uid",
    "organization_headcount_six_month_growth", "organization_headcount_twelve_month_growth",
    "organization_headcount_twenty_four_month_growth",
    "creditsdeducted", "credits_deducted", "remainingpayg", "remaining_payg",
    "remainingsubscription", "remaining_subscription", "sourceused", "source_used",
    "transactionid", "transaction_id", "totalfound", "total_found", "totalrequested", "total_requested",
}
_DROP_PATHS = ("credits.", "emails_count.", "emails count")


def _drop_duplicate_fields(data: dict) -> None:
    """The cleaned column stays. The raw copy of the same fact does not."""
    for keep, leaves in _DUPLICATES.items():
        if not data.get(keep):
            continue
        for key in list(data):
            if key != keep and _field_leaf(key) in leaves:
                data.pop(key, None)
    for key in list(data):
        folded = key.lower()
        if _field_leaf(key) in _DROP_LEAVES or folded.startswith(_DROP_PATHS):
            data.pop(key, None)


def _flat(row: dict) -> dict:
    data = {}
    extra = row.get("fields")
    if isinstance(extra, dict):
        data.update(extra)
    for key, value in row.items():
        if key in _HIDDEN or isinstance(value, (dict, list)):
            continue
        if value not in (None, ""):
            data[key] = value
    _drop_duplicate_fields(data)
    if "sources" in row:
        values = row.get("sources") or []
        data["sources"] = values if isinstance(values, str) else "; ".join(str(item) for item in values if item)
    data["cost"] = round(int(row.get("cost_micro") or 0) / 1_000_000, 6)
    return data


def _columns(rows: list[dict], first: tuple[str, ...], last: tuple[str, ...]) -> list[str]:
    present: set[str] = set()
    for row in rows:
        present.update(_flat(row))
    ordered = [key for key in first if key in present]
    middle = sorted(key for key in present if key not in ordered and key not in last)
    tail = [key for key in last if key in present]
    return ordered + middle + tail


def _header(key: str) -> str:
    if key == "revenue":
        return "Revenue (estimate)"
    return key.replace(".", " / ").replace("_", " ").strip().title()


def _cell(row: dict, key: str):
    return clean_text(_flat(row).get(key))


def _cost_amounts(companies: list[dict], removed: list[dict]) -> tuple[float, float, float]:
    kept = sum(int(row.get("cost_micro") or 0) for row in companies) / 1_000_000
    gone = sum(int(row.get("cost_micro") or 0) for row in removed) / 1_000_000
    return kept, gone, kept + gone


def _write(sheet, columns, rows) -> None:
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3D32")
    for col, key in enumerate(columns, start=1):
        cell = sheet.cell(1, col, _header(key))
        cell.font = header_font
        cell.fill = header_fill
    for offset, row in enumerate(rows, start=2):
        flat = _flat(row)
        for col, key in enumerate(columns, start=1):
            value = flat.get(key)
            if key == "founded_year":
                year = _year(clean_text(value)) or clean_text(value).split(".")[0]
                cell = sheet.cell(offset, col, year)
                cell.number_format = "@"
            elif key in {"contacts_found", "emails_found"}:
                text = clean_text(value)
                sheet.cell(offset, col, int(text) if text.isdigit() else text)
            else:
                sheet.cell(offset, col, value if key == "cost" else clean_text(value))
    last_col = get_column_letter(max(1, len(columns)))
    sheet.auto_filter.ref = f"A1:{last_col}{max(1, len(rows) + 1)}"
    sheet.freeze_panes = "A2"
    for col, key in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(col)].width = min(42, max(14, len(_header(key)) + 2))
    if "cost" in columns:
        cost_col = columns.index("cost") + 1
        for row in range(2, len(rows) + 2):
            sheet.cell(row, cost_col).number_format = "0.000000"
    for key in ("contacts_found", "emails_found"):
        if key not in columns:
            continue
        count_col = columns.index(key) + 1
        for row in range(2, len(rows) + 2):
            sheet.cell(row, count_col).number_format = "0"


def _accuracy_rows(companies: list[dict]) -> list[dict]:
    """Ten companies from the kept sheet, stable for the same domains, for a hand check."""
    pool = [row for row in companies if row.get("domain")]
    if len(pool) > 10:
        seed = "|".join(sorted(str(row.get("domain") or "") for row in pool))
        pool = random.Random(seed).sample(pool, 10)
    return [
        {
            "domain": row.get("domain") or "",
            "name": row.get("name") or "",
            "website": row.get("website") or "",
            "linkedin": row.get("linkedin") or "",
            "employees": row.get("employees") or "",
            "city": row.get("city") or "",
            "state": row.get("state") or "",
            "correct": "",
        }
        for row in pool
    ]


def workbook_bytes(
    companies: list[dict],
    contacts: list[dict],
    removed: list[dict] | None = None,
    accuracy: list[dict] | None = None,
    history: list[str] | None = None,
) -> bytes:
    removed_rows = removed or []
    kept_cost, removed_cost, total_cost = _cost_amounts(companies, removed_rows)
    book = Workbook()
    companies_sheet = book.active
    companies_sheet.title = "Companies"
    _write(companies_sheet, _columns(companies, _COMPANY_FIRST, _COMPANY_LAST), companies)
    contacts_sheet = book.create_sheet("Contacts")
    _write(contacts_sheet, _columns(contacts, _CONTACT_FIRST, _CONTACT_LAST), contacts)
    removed_sheet = book.create_sheet("Removed")
    _write(removed_sheet, list(_REMOVED_FIRST), removed_rows)
    accuracy_sheet = book.create_sheet("Accuracy")
    accuracy_rows = list(accuracy or []) or _accuracy_rows(companies)
    _write(accuracy_sheet, list(_ACCURACY_FIRST), accuracy_rows)
    summary_sheet = book.create_sheet("Summary")
    summary_sheet["A1"] = "Kept"
    summary_sheet["B1"] = f"${kept_cost:.4f}"
    summary_sheet["A2"] = "Removed"
    summary_sheet["B2"] = f"${removed_cost:.4f}"
    summary_sheet["A3"] = "Total"
    summary_sheet["B3"] = f"${total_cost:.4f}"
    checks = [str(row.get("linkedin_check") or "") for row in companies]
    if any(checks):
        summary_sheet["D1"] = "LinkedIn check"
        for offset, label in enumerate(("confirmed", "mismatch", "not checked")):
            summary_sheet.cell(2 + offset, 4, label)
            summary_sheet.cell(2 + offset, 5, sum(1 for value in checks if value.startswith(label)))
        summary_sheet.column_dimensions["D"].width = 16
    summary_sheet.column_dimensions["A"].width = 14
    summary_sheet.column_dimensions["B"].width = 14
    for offset, line in enumerate(history or []):
        summary_sheet.cell(6 + offset, 1, line)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()
