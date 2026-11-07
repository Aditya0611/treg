"""Score the saved agent answers against benchmark/ground_truth.csv and write the comparison workbook.

Examples:
  python scripts/score_benchmark.py
  python scripts/score_benchmark.py --verified-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmark_report import write_report
from app.benchmark_score import SCORED_FIELDS, is_verified, load_answers, load_truth, score_all


def _pct(value) -> str:
    return "  -  " if value is None else f"{value * 100:4.0f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--truth", type=Path, default=ROOT / "benchmark" / "ground_truth.csv")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "benchmark" / "raw")
    parser.add_argument("--out", type=Path, default=ROOT / "benchmark" / "agent_scores.xlsx")
    parser.add_argument("--verified-only", action="store_true", help="score only rows marked verified=yes")
    args = parser.parse_args()

    truth = load_truth(args.truth)
    if args.verified_only:
        truth = [row for row in truth if is_verified(row)]
    if not truth:
        print("No ground-truth rows to score.")
        return 1
    answers = load_answers(args.raw_dir)
    if not answers:
        print(f"No saved answers in {args.raw_dir}. Run scripts/run_benchmark.py first.")
        return 1
    scored = score_all(truth, answers)

    unverified = sum(1 for row in truth if not is_verified(row))
    if unverified:
        print(f"Warning: {unverified} of {len(truth)} rows are not hand-checked yet, so these scores are provisional.")
    header = f"{'agent':40} {'answered':>8} " + " ".join(f"{field[:8]:>8}" for field in SCORED_FIELDS) + f" {'overall':>8} {'spent':>8}"
    print(header)
    for item in sorted(scored["summaries"], key=lambda s: -(s["overall"] or 0)):
        fields = " ".join(f"{_pct(item['fields'][field]['correct']):>8}" for field in SCORED_FIELDS)
        print(f"{item['agent']:40} {_pct(item['coverage']):>8} {fields} {_pct(item['overall']):>8} ${item['spent_usd']:.4f}")
    print("Most accurate per field:")
    for pick in scored["best"]:
        best = f"{pick['best']} ({_pct(pick['correct']).strip()} of {pick['known']})" if pick["best"] else "not enough known answers"
        print(f"  {pick['field']:10} {best}")
    path = write_report(scored, truth, args.out, verified_only=args.verified_only)
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
