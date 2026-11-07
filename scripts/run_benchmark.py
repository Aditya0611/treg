"""Run Treg agents on the benchmark domains and save every raw answer.

Nothing is spent unless --live is given. A dry run prints what would be called and the
most it could cost. Answers are saved to benchmark/raw/<agent>.jsonl; a domain an agent
already answered is skipped, so a stopped run can be continued without paying twice.

Examples:
  python scripts/run_benchmark.py                       # dry run, all agents
  python scripts/run_benchmark.py --from-cache          # free: Dropleads and crawl4ai from saved answers
  python scripts/run_benchmark.py --live --max-usd 3    # spend up to $3
  python scripts/run_benchmark.py --live --agents hunter.companies.enrich,apollo.companies.enrich
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from app.agents import AGENTS, AGENTS_BY_ID
from app.benchmark_run import answers_from_cache, plan, run_agents
from app.benchmark_score import is_verified, load_truth
from app.treg_client import TregClient


def _agents(names: str):
    if not names or names == "all":
        return list(AGENTS)
    chosen = []
    for name in (part.strip() for part in names.split(",") if part.strip()):
        if name not in AGENTS_BY_ID:
            raise SystemExit(f"Unknown agent {name}. Known: {', '.join(AGENTS_BY_ID)}")
        chosen.append(AGENTS_BY_ID[name])
    return chosen


async def _balance(token: str, org: str, base_url: str) -> float | None:
    async with TregClient(token, org=org, base_url=base_url) as client:
        return await client.account_balance()


async def _live(args, agents, domains, token: str, org: str, base_url: str) -> dict:
    async with TregClient(token, org=org, base_url=base_url, max_cost_usd=0.30, waterfall=False) as client:
        before = await client.account_balance()
        if before is not None:
            print(f"Balance before: ${before:.4f}")
            if before < args.max_usd:
                print(f"Balance is below the ${args.max_usd:.2f} cap; the run stops when credit runs out.")
        outcome = await run_agents(client, agents, domains, args.raw_dir, max_usd=args.max_usd, retry_errors=not args.no_retry)
        after = await client.account_balance()
        if after is not None:
            print(f"Balance after: ${after:.4f}")
        return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--truth", type=Path, default=ROOT / "benchmark" / "ground_truth.csv")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "benchmark" / "raw")
    parser.add_argument("--agents", default="all", help="comma-separated agent ids, or all")
    parser.add_argument("--limit", type=int, default=0, help="only the first N domains")
    parser.add_argument("--verified-only", action="store_true", help="only rows marked verified=yes")
    parser.add_argument("--live", action="store_true", help="actually call Treg and spend credit")
    parser.add_argument("--max-usd", type=float, default=5.0, help="hard spending cap for this run")
    parser.add_argument("--no-retry", action="store_true", help="do not retry domains that errored before")
    parser.add_argument("--from-cache", action="store_true", help="write Dropleads/crawl4ai answers the app already saved (free)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    truth = load_truth(args.truth)
    if args.verified_only:
        truth = [row for row in truth if is_verified(row)]
    domains = [row["domain"] for row in truth]
    if args.limit:
        domains = domains[: args.limit]
    agents = _agents(args.agents)
    unverified = sum(1 for row in truth if not is_verified(row))
    print(f"{len(domains)} domains, {len(agents)} agents. {unverified} ground-truth rows are not hand-checked yet.")

    if args.from_cache:
        cache_path = ROOT / "runs" / "company-cache.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        written = answers_from_cache(cache, domains, args.raw_dir)
        for agent_id, count in written.items():
            print(f"{agent_id}: {count} answers written from the saved cache ($0)")
        return 0

    rows = plan(agents, domains, args.raw_dir, retry_errors=not args.no_retry)
    total = sum(row["max_cost_usd"] for row in rows)
    print(f"{'agent':45} {'price':>9} {'to call':>8} {'saved':>6} {'max cost':>9}")
    for row in rows:
        print(f"{row['agent']:45} ${row['price_usd']:<8.4f} {row['todo']:>8} {row['done']:>6} ${row['max_cost_usd']:>8.4f}")
    print(f"{'total':45} {'':>9} {sum(r['todo'] for r in rows):>8} {'':>6} ${total:>8.4f}")

    token = (os.environ.get("TREG_TOKEN") or "").strip()
    org = (os.environ.get("TREG_ORG") or "").strip()
    base_url = os.environ.get("TREG_BASE_URL") or "https://treg.to"
    if not args.live:
        if token:
            balance = asyncio.run(_balance(token, org, base_url))
            if balance is not None:
                print(f"Current balance: ${balance:.4f}")
        print("Dry run: nothing was called. Add --live to spend.")
        return 0
    if not token:
        print("TREG_TOKEN is not set in .env")
        return 1
    outcome = asyncio.run(_live(args, agents, domains, token, org, base_url))
    print(f"Done: {outcome['calls']} calls, ${outcome['spent_usd']:.4f} spent. {outcome['stopped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
