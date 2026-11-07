"""Write the agent comparison workbook."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.benchmark_score import SCORED_FIELDS

_HEADER = Font(bold=True)
_FILL = PatternFill("solid", fgColor="DDEBF7")
_LABELS = {
    "identity": "Identity",
    "linkedin": "LinkedIn",
    "employees": "Employees",
    "location": "Location",
    "industry": "Industry",
}


def _sheet(book: Workbook, title: str, headers: list[str], rows: list[list], *, percent_cols: set[int] = frozenset(), money_cols: set[int] = frozenset()):
    sheet = book.create_sheet(title)
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = _HEADER
        cell.fill = _FILL
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        sheet.append(row)
    for column in range(1, len(headers) + 1):
        letter = get_column_letter(column)
        width = max(len(str(sheet.cell(row=r, column=column).value or "")) for r in range(1, min(sheet.max_row, 200) + 1))
        sheet.column_dimensions[letter].width = min(max(10, width + 2), 60)
        if column in percent_cols:
            for r in range(2, sheet.max_row + 1):
                sheet.cell(row=r, column=column).number_format = "0%"
        if column in money_cols:
            for r in range(2, sheet.max_row + 1):
                sheet.cell(row=r, column=column).number_format = "$0.0000"
    sheet.freeze_panes = "B2"
    return sheet


def write_report(scored: dict, truth: list[dict], path: Path, *, verified_only: bool) -> Path:
    book = Workbook()
    book.remove(book.active)

    summaries = sorted(scored["summaries"], key=lambda item: (item["overall"] is None, -(item["overall"] or 0)))
    agent_headers = ["Agent", "Price per call", "Companies run", "Answered", "Coverage"]
    agent_headers += [_LABELS[field] for field in SCORED_FIELDS]
    agent_headers += ["Overall", "Spent", "Cost per answer", "Errors", "Notes"]
    agent_rows = []
    for item in summaries:
        notes = [item["note"]] if item["note"] else []
        if item["from_cache"]:
            notes.append("scored from saved answers")
        agent_rows.append(
            [item["agent"], item["price_usd"], item["ran"], item["answered"], item["coverage"]]
            + [item["fields"][field]["correct"] for field in SCORED_FIELDS]
            + [item["overall"], item["spent_usd"], item["cost_per_answer"], item["errors"], "; ".join(notes)]
        )
    first_field = 6
    percent = {5} | set(range(first_field, first_field + len(SCORED_FIELDS) + 1))
    money = {2, first_field + len(SCORED_FIELDS) + 1, first_field + len(SCORED_FIELDS) + 2}
    _sheet(book, "Agents", agent_headers, agent_rows, percent_cols=percent, money_cols=money)

    best_rows = [
        [_LABELS[item["field"]], item["best"], item["correct"], item["known"], item["runner_up"], item["runner_up_correct"]]
        for item in scored["best"]
    ]
    _sheet(
        book,
        "Best per field",
        ["Field", "Most accurate agent", "Correct", "Companies with a known answer", "Runner-up", "Runner-up correct"],
        best_rows,
        percent_cols={3, 6},
    )

    field_rows = []
    for item in summaries:
        for field in SCORED_FIELDS:
            stats = item["fields"][field]
            field_rows.append(
                [item["agent"], _LABELS[field], stats["pass"], stats["fail"], stats["missing"], stats["n/a"],
                 stats["correct"], stats["accuracy_when_given"], stats["coverage"]]
            )
    _sheet(
        book,
        "Field detail",
        ["Agent", "Field", "Pass", "Fail", "Missing", "No known answer", "Correct", "Right when it answered", "Answered when known"],
        field_rows,
        percent_cols={7, 8, 9},
    )

    detail_rows = []
    for row in scored["details"]:
        for result in row["results"]:
            detail_rows.append(
                [row["agent"], row["domain"], row["case"], "yes" if row["verified"] else "", row["call_status"],
                 _LABELS.get(result["field"], result["field"]), result["status"], str(result["expected"]),
                 str(result["got"]), result["why"]]
            )
    _sheet(
        book,
        "Every check",
        ["Agent", "Domain", "Case", "Verified", "Call", "Field", "Result", "Known answer", "Agent said", "Why"],
        detail_rows,
    )

    truth_headers = list(truth[0].keys()) if truth else ["domain"]
    _sheet(book, "Ground truth", truth_headers, [[row.get(key, "") for key in truth_headers] for row in truth])

    note = book.create_sheet("Read me", 0)
    lines = [
        "Each agent was called directly by its Treg id, by domain only, with no filtering.",
        "Correct = right answers / companies where we know the answer (a blank answer counts as not correct).",
        "Right when it answered = right answers / answers given. Coverage = how often it answered at all.",
        "Identity passes when the agent returned the domain we asked for, or the company's real domain (canonical_domain).",
        "Employees passes when the count (or the middle of a size band) is inside the known range.",
        "Location passes when the state and country match the known HQ.",
        f"Rows scored: {len(truth)} ({'hand-checked rows only' if verified_only else 'all rows, including ones not yet hand-checked'}).",
    ]
    for line in lines:
        note.append([line])
    note.column_dimensions["A"].width = 120

    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path
