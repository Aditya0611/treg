"""Compare company search agents on one query. Dry run by default.

    python scripts/compare_search.py --preset metal              # plan and balance, nothing spent
    python scripts/compare_search.py --preset metal --live       # call each agent once, judge, score
    python scripts/compare_search.py --preset metal --live --check-location   # + Dropleads country check

Each agent is called on its own with Treg's auto-routing off. Answers are saved under
benchmark/search/<preset>/ and reused, so a rerun only pays for agents not saved yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app.search_agents import PRESETS, SEARCH_AGENTS, read_hits
from app.search_judge import hit_key, judge, outcome, score
from app.treg_client import TregClient, TregError


def _write_book(path: Path, rows: list[dict], results: dict, verdicts: dict, spec) -> Path:
    book = Workbook()
    book.remove(book.active)
    readme = book.create_sheet("Read me")
    for line in (
        f"Query: {spec.text()}",
        f"Keywords: {', '.join(spec.keywords)}",
        f"Countries: {', '.join(spec.countries)}   Employees: {spec.employees_min}-{spec.employees_max}",
        "Each agent was called once on its own, with Treg's auto-routing off.",
        "Every returned company was judged without using that agent's fields:",
        "  identity = the site's redirect plus Crustdata's identity lookup",
        "  size = Crustdata's LinkedIn employee range (edge = the range straddles the ask)",
        "  industry = AI check of Crustdata's company description",
        "fits = identified, size inside, industry match.  wrong = parked, size outside, wrong industry, or wrong country.",
        "precision = fits / returned.  known precision = fits / (fits + wrong).",
    ):
        readme.append([line])
    readme.column_dimensions["A"].width = 120
    headers = list(rows[0].keys()) if rows else []
    agents = book.create_sheet("Agents")
    agents.append(headers)
    for row in rows:
        agents.append([row[key] for key in headers])
    detail = book.create_sheet("Every result")
    columns = ["agent", "rank", "searched", "owner", "name (Crustdata)", "linkedin", "employees range", "size", "industry", "industry reason", "country", "location", "parked", "outcome", "agent reported"]
    detail.append(columns)
    for agent, hits in results.items():
        for rank, hit in enumerate(hits, 1):
            verdict = verdicts[hit_key(hit)]
            detail.append([
                agent, rank, hit_key(hit), verdict.get("owner", ""), verdict.get("name", ""), verdict.get("linkedin", ""),
                verdict.get("employees_range", ""), verdict.get("size", ""), verdict.get("industry", ""),
                verdict.get("industry_reason", ""), verdict.get("country", ""), verdict.get("location", "not checked"),
                verdict.get("parked", ""), outcome(verdict), json.dumps(hit.get("reported") or {}, default=str)[:300],
            ])
    for sheet in (agents, detail):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="DDEBF7")
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in list(column)[:200])
            sheet.column_dimensions[column[0].column_letter].width = min(max(10, width + 2), 50)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        book.save(path)
    except PermissionError:
        path = path.with_name(f"{path.stem}_{time.strftime('%H%M%S')}{path.suffix}")
        book.save(path)
    return path


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="metal", choices=sorted(PRESETS))
    parser.add_argument("--agents", default=",".join(SEARCH_AGENTS))
    parser.add_argument("-n", type=int, default=25, help="companies to ask each agent for")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="ask agents again even when an answer is saved")
    parser.add_argument("--check-location", action="store_true", help="Dropleads country check, about $0.0018 per company")
    parser.add_argument("--max-usd", type=float, default=0.10)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    spec = PRESETS[args.preset]
    picked = [SEARCH_AGENTS[name.strip()] for name in args.agents.split(",") if name.strip()]
    folder = ROOT / "benchmark" / "search" / spec.label
    folder.mkdir(parents=True, exist_ok=True)

    print(f"Query: {spec.text()}")
    plan_total = 0.0
    for agent in picked:
        saved = (folder / f"{agent.id}.json").exists() and not args.refresh
        price = 0.0 if saved else agent.price(args.n)
        plan_total += price
        print(f"  {agent.id:34} {'saved' if saved else f'${price:.4f}':>9}  {agent.note}")
    print(f"Search cost: about ${plan_total:.4f}. Judging is free (Crustdata identity + AI).")
    if args.check_location:
        print(f"Location check adds about $0.0018 per company (up to ${0.0018 * args.n * len(picked):.2f}).")

    token = (os.environ.get("TREG_TOKEN") or "").strip()
    if not token:
        print("TREG_TOKEN is not set in .env")
        return
    async with TregClient(
        token,
        base_url=os.environ.get("TREG_BASE_URL") or "https://treg.to",
        org=(os.environ.get("TREG_ORG") or "").strip(),
        max_cost_usd=0.05,
        waterfall=False,
    ) as client:
        balance = await client.account_balance()
        print(f"Current balance: ${balance:.4f}" if balance is not None else "Balance could not be read")
        if not args.live:
            print("Dry run: nothing was called. Add --live to spend.")
            return
        if plan_total > args.max_usd:
            print(f"Planned ${plan_total:.4f} is above --max-usd {args.max_usd}. Nothing was called.")
            return

        results: dict[str, list] = {}
        costs: dict[str, float] = {}
        for agent in picked:
            path = folder / f"{agent.id}.json"
            if path.exists() and not args.refresh:
                stored = json.loads(path.read_text(encoding="utf-8"))
            else:
                try:
                    answer = await client.call(agent.id, agent.request(spec, args.n), method=agent.method, as_query=agent.as_query)
                except TregError as exc:
                    print(f"  {agent.id}: failed ({exc.status} {exc.message})")
                    continue
                stored = {"output": answer.output, "raw": answer.raw, "cost_micro": answer.cost_micro, "request": agent.request(spec, args.n)}
                path.write_text(json.dumps(stored, default=str), encoding="utf-8")
            hits = read_hits(agent.id, stored.get("output") or {}, stored.get("raw"))[: args.n]
            results[agent.id] = hits
            costs[agent.id] = (stored.get("cost_micro") or 0) / 1_000_000
            print(f"  {agent.id}: {len(hits)} companies, ${costs[agent.id]:.4f}")

        everything = {hit_key(hit): hit for hits in results.values() for hit in hits}
        verdicts = await judge(client, everything, spec, folder / "judged.json", check_location=args.check_location)

    rows = score(results, verdicts, costs)
    print()
    print(f"{'agent':34} {'returned':>8} {'fits':>5} {'wrong':>6} {'unclear':>8} {'precision':>10} {'only here':>10} {'cost':>8}")
    for row in rows:
        print(
            f"{row['agent']:34} {row['returned']:>8} {row['fits']:>5} {row['wrong']:>6} {row['unclear']:>8} "
            f"{row['precision']:>10.0%} {row['only_here']:>10} ${row['cost']:>7.4f}"
        )
    out = _write_book(ROOT / "benchmark" / f"search_scores_{spec.label}.xlsx", rows, results, verdicts, spec)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
