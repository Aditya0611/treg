"""Judge every company a search agent returned, without trusting that agent's own fields.

Identity comes from the site's redirect and Crustdata's free identity lookup. Size is Crustdata's
LinkedIn employee range. Industry is the AI check reading Crustdata's description. Location is
optional and costs a Dropleads lookup per company.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx

from app.identity import crustdata_identity, redirect_identity
from app.normalize import clean_linkedin
from app.places import country_code
from app.search_agents import SearchSpec
from app.treg_client import TregError


def range_bounds(text: str) -> tuple[int | None, int | None]:
    """(low, high) from Crustdata's employee range: "51-200", "10001+", "myself only"."""
    value = (text or "").lower().replace(",", "")
    if "myself" in value:
        return 1, 1
    match = re.search(r"(\d+)\s*-\s*(\d+)", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+)\s*\+", value)
    if match:
        return int(match.group(1)), None
    return None, None


def size_verdict(text: str, low: int, high: int) -> str:
    """pass when the range sits inside the ask, fail when it misses it, edge when it straddles."""
    first, last = range_bounds(text)
    if first is None:
        return "unknown"
    top = last if last is not None else first * 10
    if top <= first:
        return "pass" if low <= first <= high else "fail"
    share = (min(top, high) - max(first, low)) / (top - first)
    if share >= 0.999:
        return "pass"
    if share <= 0.05:
        return "fail"
    return "edge"


def outcome(verdict: dict) -> str:
    if verdict.get("parked") or verdict.get("size") == "fail" or verdict.get("industry") == "no" or verdict.get("location") == "fail":
        return "wrong"
    if verdict.get("identified") and verdict.get("size") == "pass" and verdict.get("industry") == "match":
        return "fits"
    return "unclear"


def hit_key(hit: dict) -> str:
    return hit.get("domain") or hit.get("linkedin") or ""


async def _owners(keys: list[str], hits: dict[str, dict]) -> dict[str, tuple[str, str]]:
    from app.pipeline import _final_host

    limit = asyncio.Semaphore(8)

    async def one(key: str):
        domain = hits[key].get("domain") or ""
        if not domain:
            return key, ("", "")
        async with limit:
            final = await _final_host(domain)
        return key, redirect_identity(domain, final)

    return dict(await asyncio.gather(*(one(key) for key in keys)))


async def _identities(client, owners: dict[str, str], linkedins: dict[str, str], log) -> dict[str, dict]:
    found: dict[str, dict] = {}
    domains = sorted({owner for owner in owners.values() if owner})
    for start in range(0, len(domains), 10):
        batch = domains[start : start + 10]
        try:
            result = await client.identify_companies(batch)
        except (TregError, httpx.TransportError) as exc:
            log(f"identity lookup failed for {len(batch)} domains ({exc}); they stay unclear")
            continue
        payload = result.raw if result.raw is not None else result.output
        for domain in batch:
            found[domain] = crustdata_identity(payload, domain)
    for key, url in linkedins.items():
        try:
            result = await client.identify_linkedin(url)
        except (TregError, httpx.TransportError) as exc:
            log(f"identity lookup failed for {url} ({exc})")
            continue
        found[url] = crustdata_identity(result.raw if result.raw is not None else result.output, "")
    return found


async def _location(client, domain: str, spec: SearchSpec) -> tuple[str, str]:
    from app.pipeline import _dropleads_record

    try:
        result = await client.fill_company(domain)
    except (TregError, httpx.TransportError):
        return "unknown", ""
    facts = _dropleads_record(result.output, result.raw)
    code = country_code(facts.get("country_code") or facts.get("country") or "")
    if not code:
        return "unknown", ""
    return ("pass" if code in spec.countries else "fail"), code


async def judge(client, hits: dict[str, dict], spec: SearchSpec, path: Path, *, check_location: bool = False, log=print) -> dict[str, dict]:
    """Verdict per hit key. Saved to `path`, so a rerun only judges new companies."""
    from app.pipeline import _llm_industry

    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = [key for key in hits if key not in saved]
    if todo:
        log(f"judging {len(todo)} new companies (redirect, Crustdata identity, AI industry check)")
    owners = await _owners(todo, hits)
    linkedins = {key: hits[key]["linkedin"] for key in todo if not hits[key].get("domain") and hits[key].get("linkedin")}
    names = await _identities(client, {key: owners[key][0] for key in todo if not owners[key][1]}, linkedins, log)
    for count, key in enumerate(todo, 1):
        owner, parked = owners[key]
        verdict: dict = {"owner": owner, "parked": parked}
        if not parked:
            info = names.get(owner) or names.get(linkedins.get(key, "")) or {}
            verdict.update(
                identified=bool(info),
                name=info.get("name", ""),
                linkedin=clean_linkedin(info.get("linkedin", "")),
                employees_range=info.get("employees", ""),
                size=size_verdict(info.get("employees", ""), spec.employees_min, spec.employees_max),
            )
            if info.get("description"):
                company = {"name": info.get("name", ""), "domain": owner, "description": info["description"]}
                answer, reason, problem = await _llm_industry(company, spec.industry, saas=False)
                verdict["industry"] = answer or "unknown"
                verdict["industry_reason"] = reason or problem
            else:
                verdict["industry"] = "unknown"
                verdict["industry_reason"] = "no description to judge"
        saved[key] = verdict
        if count % 10 == 0:
            path.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    if check_location:
        for key in hits:
            verdict = saved[key]
            if verdict.get("location") or verdict.get("parked") or not verdict.get("owner"):
                continue
            verdict["location"], verdict["country"] = await _location(client, verdict["owner"], spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    return saved


def score(results: dict[str, dict], verdicts: dict[str, dict], costs: dict[str, float]) -> list[dict]:
    """One row per agent: how many of its results fit, were wrong, or stayed unclear."""
    fits_by_agent = {
        agent: {verdicts[hit_key(hit)].get("owner") or hit_key(hit) for hit in hits if outcome(verdicts[hit_key(hit)]) == "fits"}
        for agent, hits in results.items()
    }
    rows = []
    for agent, hits in results.items():
        counts = {"fits": 0, "wrong": 0, "unclear": 0}
        reasons = {"parked": 0, "not identified": 0, "size outside": 0, "size edge": 0, "wrong industry": 0, "industry unsure": 0, "wrong country": 0}
        owners_seen: set[str] = set()
        repeats = 0
        for hit in hits:
            verdict = verdicts[hit_key(hit)]
            owner = verdict.get("owner") or hit_key(hit)
            if owner in owners_seen:
                repeats += 1
            owners_seen.add(owner)
            counts[outcome(verdict)] += 1
            if verdict.get("parked"):
                reasons["parked"] += 1
                continue
            if not verdict.get("identified"):
                reasons["not identified"] += 1
            if verdict.get("size") == "fail":
                reasons["size outside"] += 1
            elif verdict.get("size") == "edge":
                reasons["size edge"] += 1
            if verdict.get("industry") == "no":
                reasons["wrong industry"] += 1
            elif verdict.get("industry") in {"unsure", "unknown"}:
                reasons["industry unsure"] += 1
            if verdict.get("location") == "fail":
                reasons["wrong country"] += 1
        others = set().union(*(found for name, found in fits_by_agent.items() if name != agent)) if len(fits_by_agent) > 1 else set()
        returned = len(hits)
        cost = costs.get(agent, 0.0)
        rows.append({
            "agent": agent,
            "returned": returned,
            **counts,
            "precision": counts["fits"] / returned if returned else 0.0,
            "known_precision": counts["fits"] / (counts["fits"] + counts["wrong"]) if counts["fits"] + counts["wrong"] else 0.0,
            "only_here": len(fits_by_agent[agent] - others),
            "repeats": repeats,
            "cost": cost,
            "cost_per_fit": cost / counts["fits"] if counts["fits"] else None,
            **reasons,
        })
    rows.sort(key=lambda row: (row["precision"], row["fits"]), reverse=True)
    return rows
