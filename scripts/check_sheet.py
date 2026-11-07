"""A sheet for hand-checking benchmark answers, and the step that reads it back.

    python scripts/check_sheet.py                      # write benchmark/check_metal.xlsx
    python scripts/check_sheet.py --import             # copy the filled CORRECT rows into the truth CSV

Each company has a yellow CORRECT row to fill from its LinkedIn page, with every agent's answer
underneath. Orange cells are fields where the agents disagree, so those need the closest look.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

from app.agents import extract, linkedin_slug
from app.benchmark_score import load_answers, load_truth
from app.normalize import employee_bounds
from app.places import country_code, state_name

FIELDS = ("employees", "city", "state", "country", "linkedin")
CORRECT = "CORRECT (fill in)"
YELLOW = PatternFill("solid", fgColor="FFF2CC")
ORANGE = PatternFill("solid", fgColor="F8CBAD")
BLUE = PatternFill("solid", fgColor="DDEBF7")


def _same(field: str, value: str) -> str:
    """A comparable form, so 'Ohio' and 'OH' or two LinkedIn URLs for one page count as agreeing."""
    text = (value or "").strip()
    if field == "country":
        return country_code(text) or text.lower()
    if field == "state":
        return state_name(text) or text.lower()
    if field == "linkedin":
        return linkedin_slug(text)
    if field == "employees":
        low, high = employee_bounds(text)
        if low is None:
            return ""
        return str(round(((low + (high or low)) / 2) / 50))
    return text.lower()


def build(truth_path: Path, raw_dir: Path, out: Path) -> Path:
    truth = load_truth(truth_path)
    answers = load_answers(raw_dir)
    book = Workbook()
    guide = book.active
    guide.title = "How to fill"
    for line in (
        "For each company, fill the yellow CORRECT row from the company's LinkedIn page (open the link in column C).",
        "employees: the range LinkedIn shows, such as 51-200 (or an exact number if the website states one).",
        "city / state / country: the headquarters on LinkedIn. state only for US and Canada.",
        "linkedin: the company page URL you checked.",
        "verified: type yes once the row is checked. Rows without yes are not scored.",
        "Orange cells: the agents disagree on that field, so check it carefully. Do not copy an agent's value without checking.",
        "Save the file, then run: python scripts/check_sheet.py --import",
    ):
        guide.append([line])
    guide.column_dimensions["A"].width = 120

    sheet = book.create_sheet("Check")
    sheet.append(["domain", "name", "open", "source", *FIELDS, "verified"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = BLUE
    for row in truth:
        domain = row["domain"]
        records = []
        for agent_id, by_domain in sorted(answers.items()):
            answer = by_domain.get(domain)
            if not answer or answer.get("status") != "ok":
                continue
            record = extract(agent_id, answer)
            if any(record.get(field) for field in FIELDS):
                records.append((agent_id.split(".")[0], record))
        link = row.get("linkedin") or next((r["linkedin"] for _a, r in records if r.get("linkedin")), "")
        sheet.append([
            domain, row.get("name") or "", link or f"https://{domain}", CORRECT,
            row.get("employees_min") and f"{row['employees_min']}-{row.get('employees_max') or ''}" or "",
            row.get("city") or "", row.get("state") or "", row.get("country") or "", row.get("linkedin") or "",
            row.get("verified") or "",
        ])
        top = sheet.max_row
        for cell in sheet[top]:
            cell.fill = YELLOW
        sheet.cell(top, 3).hyperlink = link or f"https://{domain}"
        disagree = {
            field: len({_same(field, r.get(field) or "") for _a, r in records if r.get(field)} - {""}) > 1
            for field in FIELDS
        }
        for short, record in records:
            sheet.append(["", "", "", short, *[record.get(field) or "" for field in FIELDS], ""])
            line = sheet.max_row
            for position, field in enumerate(FIELDS, start=5):
                cell = sheet.cell(line, position)
                cell.font = Font(color="595959")
                if disagree[field] and cell.value:
                    cell.fill = ORANGE
        sheet.append([])
    for letter, width in zip("ABCDEFGHIJ", (28, 30, 40, 18, 14, 18, 16, 16, 50, 10)):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A2"
    out.parent.mkdir(parents=True, exist_ok=True)
    book.save(out)
    return out


def read_back(sheet_path: Path, truth_path: Path) -> int:
    sheet = load_workbook(sheet_path, read_only=True)["Check"]
    filled: dict[str, dict] = {}
    for values in sheet.iter_rows(min_row=2, values_only=True):
        if not values or values[3] != CORRECT or not values[0]:
            continue
        domain, _name, _open, _source, employees, city, state, country, linkedin, verified = (list(values) + [None] * 10)[:10]
        low, high = employee_bounds(str(employees or ""))
        filled[str(domain).strip().lower()] = {
            "employees_min": "" if low is None else str(low),
            "employees_max": "" if high is None else str(high if high is not None else low),
            "city": str(city or "").strip(),
            "state": str(state or "").strip(),
            "country": str(country or "").strip(),
            "linkedin": str(linkedin or "").strip(),
            "verified": str(verified or "").strip(),
        }
    with truth_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        rows = list(reader)
    changed = 0
    for row in rows:
        update = filled.get((row.get("domain") or "").strip().lower())
        if update and any(update.values()):
            row.update(update)
            changed += 1
    with truth_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", type=Path, default=ROOT / "benchmark" / "ground_truth_metal.csv")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "benchmark" / "raw_metal")
    parser.add_argument("--sheet", type=Path, default=ROOT / "benchmark" / "check_metal.xlsx")
    parser.add_argument("--import", dest="read", action="store_true", help="copy the filled sheet into the truth CSV")
    args = parser.parse_args()
    if args.read:
        changed = read_back(args.sheet, args.truth)
        verified = sum(1 for row in load_truth(args.truth) if (row.get("verified") or "").lower() in {"yes", "y"})
        print(f"Updated {changed} companies in {args.truth}. {verified} are marked verified.")
        return 0
    if args.sheet.exists():
        print(f"{args.sheet} already exists; not overwriting your answers. Delete it first to rebuild.")
        return 1
    print(f"Wrote {build(args.truth, args.raw_dir, args.sheet)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
