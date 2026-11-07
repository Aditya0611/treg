import csv
import json
import sys
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_sheet import CORRECT, build, read_back  # noqa: E402


def _truth(path: Path) -> None:
    columns = ["domain", "canonical_domain", "name", "linkedin", "employees_min", "employees_max", "city", "state",
               "country", "industry", "is_for_profit", "case", "verified", "notes"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow({"domain": "steelco.com", "name": "Steelco", "industry": "metal fabrication", "case": "metal"})


def test_sheet_marks_disagreement_and_reads_back(tmp_path):
    truth = tmp_path / "truth.csv"
    _truth(truth)
    raw = tmp_path / "raw"
    raw.mkdir()
    for agent, employees, state in (("dropleads.companies.enrich", 40, "Ohio"), ("hunter.companies.enrich", 300, "OH")):
        output = {"domain": "steelco.com", "employees": employees, "state": state, "country": "US"}
        (raw / f"{agent}.jsonl").write_text(
            json.dumps({"agent": agent, "domain": "steelco.com", "status": "ok", "output": output, "raw": output}) + "\n",
            encoding="utf-8",
        )
    sheet_path = build(truth, raw, tmp_path / "check.xlsx")

    book = load_workbook(sheet_path)
    sheet = book["Check"]
    rows = list(sheet.iter_rows(min_row=2))
    assert rows[0][3].value == CORRECT
    agent_rows = [row for row in rows[1:] if row[3].value]
    assert len(agent_rows) == 2
    assert all(row[4].fill.fgColor.rgb.endswith("F8CBAD") for row in agent_rows)
    assert not any(row[6].fill.fgColor.rgb.endswith("F8CBAD") for row in agent_rows)

    for cell, value in zip(rows[0][4:10], ("51-200", "Cleveland", "Ohio", "US", "https://www.linkedin.com/company/steelco", "yes")):
        cell.value = value
    book.save(sheet_path)
    assert read_back(sheet_path, truth) == 1
    saved = next(csv.DictReader(truth.open(encoding="utf-8")))
    assert (saved["employees_min"], saved["employees_max"], saved["state"], saved["verified"]) == ("51", "200", "Ohio", "yes")
