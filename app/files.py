"""Read a domain list from CSV or Excel."""

from __future__ import annotations

import csv
import io

from openpyxl import load_workbook

from app.normalize import normalize_domain

DOMAIN_HEADERS = {
    "domain",
    "company_domain",
    "website",
    "url",
    "company_website",
    "website_url",
    "web",
    "company_url",
    "site",
    "domain_name",
}
NAME_HEADERS = {
    "company_name",
    "company",
    "name",
    "account_name",
    "account",
    "organization",
    "organisation",
}


def _header_key(value) -> str:
    return " ".join(str(value or "").strip().lower().replace("-", " ").split()).replace(" ", "_")


def _rows_from_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect)]


def _rows_from_xlsx(data: bytes) -> list[list]:
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        return [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def parse_upload(filename: str, data: bytes) -> tuple[list[dict], str, int]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        table = _rows_from_xlsx(data)
    elif name.endswith(".xls"):
        raise ValueError("Save the workbook as .xlsx or .csv and upload it again.")
    else:
        table = _rows_from_csv(data)
    table = [row for row in table if any(str(cell or "").strip() for cell in row)]
    if not table:
        raise ValueError("The file has no rows.")

    headers = [_header_key(cell) for cell in table[0]]
    domain_index = next((index for index, header in enumerate(headers) if header in DOMAIN_HEADERS), None)
    name_index = next((index for index, header in enumerate(headers) if header in NAME_HEADERS and index != domain_index), None)
    start = 1
    column = headers[domain_index] if domain_index is not None else ""

    if domain_index is None and len(headers) == 1 and normalize_domain(str(table[0][0] or "")):
        domain_index = 0
        start = 0
        column = "column 1"
        name_index = None

    if domain_index is None:
        raise ValueError("No domain column found. Name the column domain, website, or company_domain.")

    rows: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for raw in table[start:]:
        cell = raw[domain_index] if domain_index < len(raw) else ""
        domain = normalize_domain(str(cell or ""))
        if not domain:
            skipped += 1
            continue
        if domain in seen:
            skipped += 1
            continue
        seen.add(domain)
        company_name = ""
        if name_index is not None and name_index < len(raw) and raw[name_index] is not None:
            company_name = str(raw[name_index]).strip()
        rows.append({"domain": domain, "company_name": company_name})
        if len(rows) > 1000:
            raise ValueError("The file has more than 1,000 companies. Split it and run each part on its own.")
    if not rows:
        raise ValueError("No usable domains were found in that column.")
    return rows, column, skipped
