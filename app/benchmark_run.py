"""Call each agent on each benchmark domain and save exactly what it returned.

No filtering happens here: every answer is saved, including misses and errors.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Callable

from app.agents import Agent
from app.normalize import normalize_domain
from app.treg_client import TregError


def raw_path(raw_dir: Path, agent_id: str) -> Path:
    return raw_dir / f"{agent_id}.jsonl"


def saved_domains(raw_dir: Path, agent_id: str, *, retry_errors: bool = True) -> set[str]:
    """Domains this agent already answered. Errors are retried unless told otherwise."""
    path = raw_path(raw_dir, agent_id)
    if not path.exists():
        return set()
    done: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        domain = normalize_domain(item.get("domain") or "")
        if domain:
            done[domain] = item.get("status") or ""
    return {domain for domain, status in done.items() if status != "error" or not retry_errors}


def _append(raw_dir: Path, agent_id: str, item: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    with raw_path(raw_dir, agent_id).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, default=str) + "\n")


def plan(agents: list[Agent], domains: list[str], raw_dir: Path, *, retry_errors: bool = True) -> list[dict]:
    """What a live run would call, and the most it could cost at catalog prices."""
    rows = []
    for agent in agents:
        todo = [domain for domain in domains if domain not in saved_domains(raw_dir, agent.id, retry_errors=retry_errors)]
        rows.append(
            {
                "agent": agent.id,
                "price_usd": agent.price_usd,
                "todo": len(todo),
                "done": len(domains) - len(todo),
                "max_cost_usd": round(agent.price_usd * len(todo), 4),
            }
        )
    return rows


async def run_agents(
    client,
    agents: list[Agent],
    domains: list[str],
    raw_dir: Path,
    *,
    max_usd: float,
    retry_errors: bool = True,
    pause_s: float = 0.4,
    log: Callable[[str], None] = print,
) -> dict:
    """Call every agent on every domain it has not answered yet, stopping before max_usd."""
    spent_micro = 0
    calls = 0
    stopped = ""
    for agent in agents:
        done = saved_domains(raw_dir, agent.id, retry_errors=retry_errors)
        for domain in domains:
            if domain in done:
                continue
            if (spent_micro / 1_000_000) + agent.price_usd > max_usd:
                stopped = f"stopped before {agent.id} on {domain}: the ${max_usd:.2f} cap would be passed"
                log(stopped)
                return {"spent_usd": spent_micro / 1_000_000, "calls": calls, "stopped": stopped}
            item = {"agent": agent.id, "domain": domain, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            try:
                result = await client.call(agent.id, agent.body(domain), method=agent.method)
            except TregError as exc:
                item.update(status="miss" if exc.status == 404 else "error", http_status=exc.status, error=exc.message, cost_micro=0)
            except Exception as exc:  # network trouble: keep going, retry next run
                item.update(status="error", http_status=0, error=f"{exc.__class__.__name__}: {exc}", cost_micro=0)
            else:
                item.update(
                    status="ok",
                    http_status=200,
                    cost_micro=int(result.cost_micro or 0),
                    served_by=result.served_by,
                    call_id=result.call_id,
                    output=result.output,
                    raw=result.raw,
                )
                spent_micro += int(result.cost_micro or 0)
            calls += 1
            _append(raw_dir, agent.id, item)
            log(f"{agent.id} {domain}: {item['status']} ${item['cost_micro'] / 1_000_000:.4f}")
            if pause_s:
                await asyncio.sleep(pause_s)
    return {"spent_usd": spent_micro / 1_000_000, "calls": calls, "stopped": stopped}


def answers_from_cache(cache: dict, domains: list[str], raw_dir: Path) -> dict[str, int]:
    """Write Dropleads and crawl4ai answers the app already paid for, at no cost.

    The cache keeps the full Dropleads row, but only part of the crawl4ai page
    (domain, headcount, state, description), so crawl4ai scores from the cache are partial.
    """
    written = {"dropleads.companies.enrich": 0, "crawl4ai.companies.enrich": 0}
    for domain in domains:
        row = cache.get(domain)
        if not isinstance(row, dict):
            continue
        dropleads = row.get("dropleads_response")
        if isinstance(dropleads, dict) and dropleads and domain not in saved_domains(raw_dir, "dropleads.companies.enrich"):
            _append(
                raw_dir,
                "dropleads.companies.enrich",
                {"agent": "dropleads.companies.enrich", "domain": domain, "status": "ok", "cost_micro": 0,
                 "from_cache": True, "output": dropleads, "raw": None},
            )
            written["dropleads.companies.enrich"] += 1
        full = row.get("crawl4ai") if isinstance(row.get("crawl4ai"), dict) and row.get("crawl4ai") else None
        page = full or row.get("street_page")
        if isinstance(page, dict) and page and domain not in saved_domains(raw_dir, "crawl4ai.companies.enrich"):
            item = {"agent": "crawl4ai.companies.enrich", "domain": domain, "status": "ok", "cost_micro": 0,
                    "from_cache": True, "output": page, "raw": None}
            if full is None:
                item["not_saved"] = ["name", "linkedin", "city", "state", "country", "industry"]
            _append(raw_dir, "crawl4ai.companies.enrich", item)
            written["crawl4ai.companies.enrich"] += 1
    return written
