"""Pick benchmark companies from a search comparison, with the facts left blank for hand-checking.

    python scripts/make_truth_from_search.py --preset metal --n 30

Only companies the search judge marked as fitting are picked, spread across the agents that
found them. Enrich facts (headcount, location, LinkedIn) are left blank on purpose so the
benchmark is not tilted toward any agent; they are filled by hand from LinkedIn and the website.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.search_agents import PRESETS, read_hits
from app.search_judge import hit_key, outcome

COLUMNS = ["domain", "canonical_domain", "name", "linkedin", "employees_min", "employees_max", "city", "state",
           "country", "industry", "is_for_profit", "case", "verified", "notes"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="metal", choices=sorted(PRESETS))
    parser.add_argument("--n", type=int, default=30)
    args = parser.parse_args()

    spec = PRESETS[args.preset]
    folder = ROOT / "benchmark" / "search" / spec.label
    verdicts = json.loads((folder / "judged.json").read_text(encoding="utf-8"))
    by_agent: dict[str, list[str]] = {}
    for path in sorted(folder.glob("*.json")):
        if path.name == "judged.json":
            continue
        stored = json.loads(path.read_text(encoding="utf-8"))
        keys = [hit_key(hit) for hit in read_hits(path.stem, stored.get("output") or {}, stored.get("raw"))]
        by_agent[path.stem] = [
            key for key in keys
            if key in verdicts and outcome(verdicts[key]) == "fits" and verdicts[key].get("owner")
        ]

    picked: list[str] = []
    owners: set[str] = set()
    turn = 0
    while len(picked) < args.n and any(turn < len(keys) for keys in by_agent.values()):
        for keys in by_agent.values():
            if turn < len(keys) and len(picked) < args.n:
                owner = verdicts[keys[turn]].get("owner") or keys[turn]
                if owner not in owners:
                    owners.add(owner)
                    picked.append(keys[turn])
        turn += 1

    out = ROOT / "benchmark" / f"ground_truth_{spec.label}.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for key in picked:
            verdict = verdicts[key]
            writer.writerow({
                "domain": verdict.get("owner") or key,
                "name": verdict.get("name") or "",
                "industry": "metal fabrication",
                "is_for_profit": "yes",
                "case": spec.label,
                "notes": "fill LinkedIn URL, headcount range, and HQ city/state/country from the LinkedIn page",
            })
    print(f"Wrote {len(picked)} companies to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
