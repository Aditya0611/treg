"""Run the company list: Hunter for the list, Dropleads for the facts, crawl4ai for the street."""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import unicodedata
import uuid
from pathlib import Path
from dataclasses import dataclass, field

import httpx

from app.cache import cached, remember
from app.filters import Filters, llm_target
from app.agents import extract, linkedin_slug
from app.identity import brand, crustdata_identity, identity_mismatch, locked_domain, redirect_identity
from app.search_agents import DEFAULT_SEARCH_AGENTS, SEARCH_AGENTS, read_hits, spec_from_filters
from app.search_judge import range_bounds, size_verdict
from app.normalize import (
    _field_leaf,
    believable_revenue,
    clean_company_name,
    clean_phone,
    clean_revenue,
    clean_text,
    _year,
    company_from_payload,
    email_status,
    employee_bounds,
    employee_gap,
    flatten_record,
    founded_gap,
    described_text,
    location_mismatch,
    _states_named,
    naics_codes,
    naics_mismatch,
    normalize_domain,
    software_mismatch,
    us_employee_count,
    tidy_company,
    outside_employee_range,
    people_from_payload,
    reported_headcount,
    size_evidence,
)
from app.phones import foreign_phone_reason, phone_location_flag
from app.places import country_code, country_codes, country_label, state_name
from app.treg_client import CallResult, TregError


def _nested_lists(value, depth: int = 0) -> list:
    if depth > 4 or not isinstance(value, dict):
        return []
    found: list = []
    for key, item in value.items():
        name = str(key).lower()
        if name in {"companies", "results", "organizations", "accounts", "data"} and isinstance(item, list):
            found.extend(item)
        elif isinstance(item, dict):
            found.extend(_nested_lists(item, depth + 1))
    return found


def failure_text(exc: BaseException) -> str:
    """A readable reason. A dropped Treg connection often arrives with an empty message."""
    name = exc.__class__.__name__
    if "Timeout" in name:
        return "Treg took too long to answer"
    if name in {"ReadError", "ConnectError", "WriteError", "RemoteProtocolError", "NetworkError", "CloseError"}:
        return "The connection to Treg dropped"
    text = str(exc).strip()
    return text or name


def _employee_span(low: int | None, high: int | None) -> str:
    if low is not None and high is not None:
        return f"{low}–{high}"
    if high is not None:
        return f"up to {high}"
    if low is not None:
        return f"{low} or more"
    return ""


def _leave_out(job: Job, searched: str, found: str, evidence: str, exact: int | None, company: dict) -> str:
    """A sized search keeps a row only when the lookup is the same company and the headcount fits."""
    if job.mode != "query" or (job.filters.employees_min is None and job.filters.employees_max is None):
        return ""
    left = normalize_domain(searched)
    right = normalize_domain(found)
    if left and right and left != right:
        return f"the lookup returned {right}, a different company from {left}"
    if evidence == "blank":
        return "employee count was not reported"
    if exact is not None and outside_employee_range(exact, job.filters.employees_min, job.filters.employees_max):
        shown = company.get("employees") or exact
        return f"employees {shown} outside {_employee_span(job.filters.employees_min, job.filters.employees_max)}"
    place = location_mismatch(
        company,
        city=job.filters.city,
        state=job.filters.state,
        country=job.filters.country,
    )
    if place:
        return place
    return software_mismatch(
        job.filters.industry,
        company.get("industry") or "",
        company.get("name") or "",
        company.get("domain") or "",
        described_text(company),
    )


def _reject_search_row(job: Job, seed: dict) -> str:
    """Drop a company using only the Apollo row. This part does not spend."""
    if job.mode != "query" or (job.filters.employees_min is None and job.filters.employees_max is None):
        return ""
    preview = seed.get("preview") or {}
    listed = _listed_in_range(seed, job.filters.employees_min, job.filters.employees_max)
    if listed is False:
        shown = preview.get("employees") or ""
        return f"employees {shown} outside {_employee_span(job.filters.employees_min, job.filters.employees_max)}"
    place = location_mismatch(
        preview,
        city=job.filters.city,
        state=job.filters.state,
        country=job.filters.country,
    )
    if place:
        return place
    if preview.get("phone"):
        flag = phone_location_flag(str(preview.get("phone")), state=job.filters.state, country=_phone_country(job))
        if flag.startswith("phone country"):
            return flag
    return ""


def _listed_in_range(seed: dict, low: int | None, high: int | None) -> bool | None:
    """True or False when the search row has a size. None when the size is missing."""
    employees = (seed.get("preview") or {}).get("employees")
    if employee_bounds(employees)[0] is None:
        return None
    return not outside_employee_range(employees, low, high)


def companies_from_search(output: dict, raw) -> list[dict]:
    rows = output.get("companies") if isinstance(output, dict) else None
    if not rows:
        rows = _nested_lists(raw) or _nested_lists(output if isinstance(output, dict) else None)
    found: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if isinstance(row, str):
            domain = normalize_domain(row)
            name = ""
            payload = None
        elif isinstance(row, dict):
            payload = company_from_payload(row, row)
            domain = payload["domain"]
            name = payload["name"] if payload["name"] != domain else ""
        else:
            continue
        if not domain or domain in seen:
            continue
        seen.add(domain)
        item = {"domain": domain, "company_name": name}
        if payload:
            item["preview"] = payload
        if isinstance(row, dict):
            item["search_fields"] = flatten_record(row)
        found.append(item)
    return found


@dataclass
class Options:
    contacts_per_company: int = 0
    verify_emails: bool = True
    lookup_phones: bool = False
    budget_usd: float = 25
    max_cost_per_call_usd: float = 0.05
    waterfall: bool = True
    concurrency: int = 4
    industry_check: bool = False

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Options":
        data = raw or {}
        raw_contacts = data.get("contacts_per_company", 0)
        if raw_contacts in (None, ""):
            raw_contacts = 0
        return cls(
            contacts_per_company=max(0, min(9, int(raw_contacts))),
            verify_emails=bool(data.get("verify_emails", True)),
            lookup_phones=bool(data.get("lookup_phones", False)),
            budget_usd=max(0.01, float(data.get("budget_usd") or 25)),
            max_cost_per_call_usd=max(0.0001, float(data.get("max_cost_per_call_usd") or 0.05)),
            waterfall=bool(data.get("waterfall", True)),
            concurrency=max(1, min(8, int(data.get("concurrency") or 4))),
            industry_check=bool(data.get("industry_check", False)),
        )


class Job:
    def __init__(self, *, mode: str, filters: Filters, options: Options):
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode
        self.filters = filters
        self.options = options
        self.status = "queued"
        self.phase = "queued"
        self.error = ""
        self.stop_reason = ""
        self.stop = False
        self.total = 0
        self.done = 0
        self.emails = 0
        self.skipped = 0
        self.spent_micro = 0
        self.search_micro = 0
        self.budget_micro = int(round(options.budget_usd * 1_000_000))
        self.companies: list[dict] = []
        self.removed: list[dict] = []
        self.accuracy: list[dict] = []
        self.accuracy_history: list[str] = []
        self.contacts: list[dict] = []
        self.events: list[str] = []
        self.identities: set[str] = set()
        self.lock = asyncio.Lock()

    def log(self, message: str) -> None:
        self.events.append(message)
        del self.events[:-40]

    async def allow(self) -> bool:
        async with self.lock:
            if self.stop:
                return False
            if self.spent_micro >= self.budget_micro:
                self.stop = True
                self.stop_reason = "Reached the cost cap"
                return False
            return True

    async def add_cost(self, micro: int) -> None:
        async with self.lock:
            self.spent_micro += max(0, micro)
            if self.spent_micro >= self.budget_micro:
                self.stop = True
                self.stop_reason = self.stop_reason or "Reached the cost cap"

    def public(self) -> dict:
        preview = []
        for company in self.companies[-8:]:
            preview.append(
                {
                    "domain": company.get("domain"),
                    "name": company.get("name"),
                    "status": company.get("status"),
                    "contacts_found": company.get("contacts_found", 0),
                    "emails_found": company.get("emails_found", 0),
                }
            )
        return {
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "phase": self.phase,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "total": self.total,
            "done": self.done,
            "contacts": len(self.contacts),
            "emails": self.emails,
            "spent_usd": round(self.spent_micro / 1_000_000, 6),
            "budget_usd": self.options.budget_usd,
            "events": list(self.events),
            "preview": preview,
            "export_ready": bool(self.companies or self.removed) and self.status in {"done", "stopped", "error"},
            "filters": self.filters.to_dict(),
        }


def _blank(domain: str, name: str, index: int) -> dict:
    return {
        "index": index,
        "domain": domain,
        "name": name or domain,
        "website": "https://" + domain if domain else "",
        "linkedin": "",
        "industry": "",
        "employees": "",
        "revenue": "",
        "founded_year": "",
        "phone": "",
        "city": "",
        "state": "",
        "country": "",
        "country_code": "",
        "location": "",
        "contacts_found": 0,
        "emails_found": 0,
        "status": "",
        "sources": [],
        "cost_micro": 0,
        "fields": {},
    }


def _apply_payload(company: dict, payload: dict) -> None:
    for key in (
        "name", "website", "linkedin", "industry", "employees", "revenue",
        "founded_year", "phone", "city", "state", "country", "country_code", "location", "domain",
    ):
        if payload.get(key) and (key != "name" or payload[key] != payload.get("domain")):
            if key == "domain":
                continue
            if not company.get(key) or key in {"name", "website", "industry", "city", "state", "country", "country_code", "location"}:
                company[key] = payload[key]


async def _remember(job: Job, result) -> None:
    await job.add_cost(result.cost_micro)
    if result.ignored_filters:
        job.log(f"Treg ignored filters on {result.endpoint}: {result.ignored_filters}")


def _search_agent_ids() -> list[str]:
    """Search agents in benchmark order. TREG_SEARCH_AGENTS overrides the measured default."""
    picked = [name.strip() for name in os.environ.get("TREG_SEARCH_AGENTS", "").split(",") if name.strip()]
    return [name for name in picked or DEFAULT_SEARCH_AGENTS if name in SEARCH_AGENTS]


async def _domain_from_linkedin(client, url: str) -> str:
    identify = getattr(client, "identify_linkedin", None)
    if identify is None:
        return ""
    try:
        result = await identify(url)
    except (TregError, httpx.TransportError):
        return ""
    found = crustdata_identity(result.raw if result.raw is not None else result.output, "")
    return normalize_domain(found.get("primary_domain") or "") or ""


async def _agent_page(job: Job, client, agent_ids: list[str], spec, page: int, want: int, live: dict, seen: set) -> list[dict]:
    """One page from every agent still answering, merged in turn so no single agent dominates."""
    answers: list[list[dict]] = []
    for agent_id in agent_ids:
        agent = SEARCH_AGENTS[agent_id]
        if not live.get(agent_id) or (page > 1 and not agent.pages):
            live[agent_id] = False
            continue
        short = agent_id.split(".")[0]
        try:
            result = await client.search_with_agent(agent, spec, want, page)
        except TregError as exc:
            live[agent_id] = False
            job.log(f"{short} search page {page} failed ({exc.message})")
            if exc.status in {401, 402}:
                job.stop = True
                job.stop_reason = exc.message
            continue
        except httpx.TransportError as exc:
            live[agent_id] = False
            job.log(f"{short} search page {page} failed ({failure_text(exc)})")
            continue
        await _remember(job, result)
        job.search_micro += result.cost_micro
        hits = read_hits(agent_id, result.output, result.raw)
        if not hits:
            live[agent_id] = False
        rows = []
        for hit in hits:
            domain = hit["domain"] or (await _domain_from_linkedin(client, hit["linkedin"]) if hit.get("linkedin") else "")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            preview = {key: value for key, value in (hit.get("reported") or {}).items() if isinstance(value, (str, int, float))}
            if hit.get("linkedin"):
                preview["linkedin"] = hit["linkedin"]
            rows.append({"domain": domain, "company_name": hit.get("name") or "", "preview": preview, "search_agent": agent_id})
        job.log(f"{short} search page {page}: {len(hits)} companies, {len(rows)} new")
        answers.append(rows)
    merged = []
    for turn in range(max((len(rows) for rows in answers), default=0)):
        merged.extend(rows[turn] for rows in answers if turn < len(rows))
    return merged


async def _search_by_agents(job: Job, client, agent_ids: list[str], span: str) -> str:
    """Ask the benchmark's best search agents directly, then enrich until the limit is kept."""
    spec = spec_from_filters(job.filters)
    job.log(f"Searching {', '.join(name.split('.')[0] for name in agent_ids)} for: {spec.text()}")
    live = {name: True for name in agent_ids}
    seen: set[str] = set()
    queue: list[dict] = []
    cursor = 0
    page = 0
    while len(job.companies) < job.filters.limit and not job.stop:
        if not queue:
            if page >= 10 or not any(live.values()) or (page and not await job.allow()):
                break
            page += 1
            want = min(100, max(25, 2 * (job.filters.limit - len(job.companies))))
            queue = await _agent_page(job, client, agent_ids, spec, page, want, live, seen)
            if not queue:
                break
        remaining = job.filters.limit - len(job.companies)
        chunk, queue = queue[:remaining], queue[remaining:]
        job.total = job.filters.limit
        job.phase = "enriching"
        await _enrich_seeds(job, client, chunk, cursor)
        cursor += len(chunk)
        job.done = len(job.companies)
        job.log(f"{len(job.companies)} kept inside {span}, {job.skipped} left out so far.")
    if not job.companies and not seen:
        return "The search agents returned no companies for that query."
    return ""


async def _enrich_seeds(job: Job, client, seeds: list[dict], start: int = 0) -> None:
    semaphore = asyncio.Semaphore(job.options.concurrency)

    async def worker(index: int, seed: dict) -> None:
        async with semaphore:
            if job.stop:
                return
            await _one_company(job, client, start + index, seed)

    await asyncio.gather(*(worker(index, seed) for index, seed in enumerate(seeds)))
    job.companies.sort(key=lambda row: row["index"])
    job.contacts.sort(key=lambda row: (row["index"], row["first_name"], row["last_name"]))


def _accuracy_sample(companies: list[dict]) -> list[dict]:
    pool = [row for row in companies if row.get("domain")]
    if len(pool) > 10:
        seed = "|".join(sorted(str(row.get("domain") or "") for row in pool))
        pool = random.Random(seed).sample(pool, 10)
    return pool


def _plain_page(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:4000]


async def _read_page(url: str) -> str:
    if not url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as http:
            response = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    return _plain_page(response.text)


async def _judge_accuracy(company: dict, website_text: str, linkedin_text: str) -> tuple[str, str]:
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return "needs review", "the accuracy check had no model key"
    if not website_text and not linkedin_text:
        return "needs review", "website and LinkedIn were not readable"
    url, model = llm_target(key, os.environ.get("LLM_BASE_URL") or "", os.environ.get("LLM_MODEL") or "")
    row = " ".join(
        f"{name}: {company.get(name) or ''}"
        for name in ("name", "domain", "employees", "city", "state", "industry")
    )
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Check a company row against its website and LinkedIn text. "
                    "Reply with JSON {\"correct\":\"yes\"|\"no\"|\"needs review\",\"note\":\"one short sentence\"}. "
                    "yes means this is the same company and the place is not clearly a different state or country. "
                    "no means the site is a different company or the place is clearly wrong. "
                    "A LinkedIn login wall is not evidence the row is wrong. "
                    "needs review when the text is too thin to decide."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Row: {row}\n\nWebsite:\n{website_text[:2500]}\n\nLinkedIn:\n{linkedin_text[:1500]}"
                )[:4000],
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=25) as http:
            response = await http.post(
                url + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = json.loads(response.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return "needs review", "the accuracy check did not answer"
    correct = str(data.get("correct") or "").strip().lower()
    note = clean_text(data.get("note") or "")
    if correct not in {"yes", "no", "needs review"}:
        return "needs review", note or "the accuracy check did not answer"
    return correct, note


def _remember_accuracy(rows: list[dict]) -> list[str]:
    path = Path(__file__).resolve().parent.parent / "runs" / "accuracy-history.json"
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        history = []
    if not isinstance(history, list):
        history = []
    yes = sum(1 for row in rows if row.get("correct") == "yes")
    label = f"run {len(history) + 1}: {yes}/{len(rows)}"
    history.append({"label": label, "yes": yes, "of": len(rows)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history), encoding="utf-8")
    except OSError:
        return [label]
    return [str(item.get("label") or "") for item in history if isinstance(item, dict) and item.get("label")]


async def _check_accuracy(job: Job) -> None:
    """List up to 10 kept companies. Correct is filled in by hand."""
    rows = []
    for company in _accuracy_sample(job.companies):
        rows.append(
            {
                "domain": company.get("domain") or "",
                "name": company.get("name") or "",
                "website": company.get("website") or "",
                "linkedin": company.get("linkedin") or "",
                "employees": company.get("employees") or "",
                "city": company.get("city") or "",
                "state": company.get("state") or "",
                "correct": "",
                "note": "",
            }
        )
    job.accuracy = rows
    job.log(f"Accuracy sheet lists {len(rows)} companies. Fill Correct by hand.")


async def run_job(job: Job, client, seeds: list[dict]) -> None:
    job.status = "running"
    try:
        sized_checked = False
        if job.mode == "query":
            job.phase = "searching"
            job.log(
                "Searching Treg for "
                + ", ".join(
                    part
                    for part in (
                        f"industry {job.filters.industry}" if job.filters.industry else "",
                        f"country {job.filters.country}" if job.filters.country else "",
                        f"“{job.filters.q}”" if job.filters.q else "",
                    )
                    if part
                )
            )
            if not await job.allow():
                job.status = "stopped"
                return
            seeds = []
            result = None
            if job.filters.employees_min is not None or job.filters.employees_max is not None:
                span = _employee_span(job.filters.employees_min, job.filters.employees_max)
                needed = job.filters.limit * 0.0028
                balance = getattr(client, "account_balance", None)
                if balance is not None:
                    available = await balance()
                    if available is not None and available < needed:
                        job.status = "done"
                        job.phase = "done"
                        job.error = (
                            f"Balance is ${available:.2f}. This run can spend about ${needed:.2f} "
                            "before it finishes. Add funds, then run again."
                        )
                        job.log(job.error)
                        return
                agent_ids = _search_agent_ids() if hasattr(client, "search_with_agent") else []
                seen: set[str] = set()
                page_error = ""
                cursor = 0
                offset = 0
                if agent_ids:
                    page_error = await _search_by_agents(job, client, agent_ids, span)
                    sized_checked = True
                else:
                    job.log(f"Searching Hunter for companies with {span} employees")
                for page in range(1, 16) if not agent_ids else ():
                    remaining = job.filters.limit - len(job.companies)
                    if remaining <= 0:
                        break
                    if page > 1 and not await job.allow():
                        break
                    try:
                        result = await client.search_companies_sized(
                            job.filters,
                            page=page,
                            offset=offset,
                            page_limit=min(100, remaining),
                        )
                    except TregError as exc:
                        if exc.status in {403, 429}:
                            job.log(f"Page {page} hit HTTP {exc.status}; waiting and trying once more.")
                            await asyncio.sleep(3)
                            try:
                                result = await client.search_companies_sized(
                                    job.filters,
                                    page=page,
                                    offset=offset,
                                    page_limit=min(100, remaining),
                                )
                            except Exception as retry_exc:
                                page_error = (
                                    retry_exc.message
                                    if isinstance(retry_exc, TregError)
                                    else failure_text(retry_exc)
                                ) or failure_text(retry_exc)
                                job.log(f"Page {page} failed ({page_error}).")
                                break
                        else:
                            page_error = exc.message or failure_text(exc)
                            job.log(f"Page {page} failed ({page_error}).")
                            break
                    except Exception as exc:
                        page_error = failure_text(exc)
                        job.log(f"Page {page} failed ({page_error}).")
                        break
                    await _remember(job, result)
                    job.search_micro += result.cost_micro
                    batch = companies_from_search(result.output, result.raw)
                    if not batch:
                        job.log(f"Search page {page} returned no companies")
                        break
                    offset += len(batch)
                    page_seeds = []
                    for seed in batch:
                        domain = seed.get("domain") or ""
                        if not domain or domain in seen:
                            continue
                        seen.add(domain)
                        page_seeds.append(seed)
                    if not page_seeds:
                        break
                    page_seeds = page_seeds[:remaining]
                    job.log(f"Search via {result.served_by or 'hunter'} returned {len(page_seeds)} companies")
                    job.total = job.filters.limit
                    job.phase = "enriching"
                    await _enrich_seeds(job, client, page_seeds, cursor)
                    cursor += len(page_seeds)
                    sized_checked = True
                    job.done = len(job.companies)
                    job.log(
                        f"Page {page}: {len(job.companies)} kept inside {span}. "
                        f"{job.skipped} left out because the lookup was a different company or the headcount was outside the range."
                    )
                    if len(job.companies) >= job.filters.limit or job.stop:
                        break
                if sized_checked:
                    seeds = []
                    if not job.companies and not page_error:
                        job.status = "done"
                        job.phase = "done"
                        job.error = (
                            f"No companies had a real headcount inside {span}. "
                            "A lookup that pointed at a different company was left out."
                        )
                        job.log(job.error)
                        return
                    if not job.companies and page_error:
                        job.status = "done"
                        job.phase = "done"
                        job.error = page_error
                        return
                elif page_error:
                    job.status = "done"
                    job.phase = "done"
                    job.error = page_error
                    return
            if not sized_checked and not seeds and (job.filters.employees_min is not None or job.filters.employees_max is not None):
                job.status = "done"
                job.phase = "done"
                job.error = "No companies matched that industry, place, and company size."
                job.log(job.error)
                return
            if not sized_checked and not seeds:
                result = await client.search_companies(job.filters)
                await _remember(job, result)
                job.search_micro += result.cost_micro
                seeds = companies_from_search(result.output, result.raw)[: job.filters.limit]
                job.log(f"Search via {result.served_by or 'treg'} returned {len(seeds)} companies")
            if not sized_checked and not seeds and job.filters.q and result is not None and await job.allow():
                excluded = (result.served_by or "").split(".")[0]
                job.log("No companies from that provider. Searching again from the full sentence.")
                result = await client.search_companies(job.filters, exclude=excluded, query_only=True)
                await _remember(job, result)
                job.search_micro += result.cost_micro
                seeds = companies_from_search(result.output, result.raw)[: job.filters.limit]
                job.log(f"Search via {result.served_by or 'treg'} returned {len(seeds)} companies")
            if not sized_checked and not seeds:
                job.status = "done"
                job.phase = "done"
                job.error = "Treg returned no companies for those filters."
                return
        if not sized_checked:
            job.total = len(seeds)
            job.phase = "enriching"
            await _enrich_seeds(job, client, seeds)
        job.done = len(job.companies)
        job.phase = "done"
        job.status = "stopped" if job.stop else "done"
        if job.stop and not job.stop_reason:
            job.stop_reason = "Stopped"
        if client.__class__.__name__ == "TregClient" and job.companies:
            try:
                await _check_accuracy(job)
            except Exception as exc:
                job.log(f"Accuracy check failed ({failure_text(exc)}).")
        left_out = f" Left out {job.skipped}." if job.skipped else ""
        checked = len(job.accuracy)
        sample = f" Accuracy sheet lists {checked} companies." if checked else ""
        flagged = sum(1 for row in job.companies if "flagged:" in str(row.get("status") or "") or str(row.get("status") or "").startswith("needs review"))
        hand = f" Review {flagged} flagged or needs-review rows by hand." if flagged else ""
        job.log(f"Finished {job.done} of {job.total} companies.{left_out}{sample}{hand}")
    except TregError as exc:
        job.status = "error"
        job.phase = "error"
        job.error = exc.message
        if exc.status == 401:
            job.error = "Treg rejected the token. Check TREG_TOKEN, or paste an org token in the form."
        elif exc.status == 402:
            job.error = "Treg balance is too low to continue."
        job.log(job.error)
    except Exception as exc:
        job.status = "error"
        job.phase = "error"
        job.error = failure_text(exc)[:500]
        job.log(job.error)


_MONEY_LEAVES = {
    "phone", "phone_number", "company_phone", "revenue", "annual_revenue",
    "revenue_range", "estimated_revenue", "total_funding", "funding",
    "street", "street_address", "address", "address1", "postal_code", "postal", "zip", "zip_code",
}


def _lookup_empty(result) -> bool:
    """True when crawl4ai returned no company. A search name alone is not a lookup."""
    blobs = []
    output = result.output if isinstance(result.output, dict) else {}
    blobs.append(output)
    nested = output.get("data") if isinstance(output.get("data"), dict) else None
    if nested:
        blobs.append(nested)
    raw = result.raw if isinstance(result.raw, dict) else {}
    blobs.append(raw)
    raw_data = raw.get("data") if isinstance(raw.get("data"), dict) else None
    if raw_data:
        blobs.append(raw_data)
    for blob in blobs:
        if any(clean_text(blob.get(key)) for key in ("name", "industry", "employees", "people_count", "linkedin", "city", "description")):
            return False
    return True


def _strip_provider_money(company: dict) -> None:
    """Phone and street come from Openmart. Revenue comes from Dropleads."""
    for key in ("phone", "revenue", "funding"):
        company.pop(key, None)
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    for key in list(fields):
        if _field_leaf(key) in _MONEY_LEAVES:
            fields.pop(key, None)


def _dropleads_facts(row: dict) -> dict:
    payload = company_from_payload(row, row)
    funding = ""
    for key, value in {**flatten_record(row), **row}.items():
        if _field_leaf(str(key)) in {"total_funding", "funding"} and clean_text(value):
            funding = value
            break
    return {
        "domain": payload.get("domain") or "",
        "phone": payload.get("phone") or "",
        "revenue": payload.get("revenue") or "",
        "funding": funding,
        "employees": payload.get("employees") or "",
        "founded_year": payload.get("founded_year") or "",
    }


async def _fill_money(job: Job, client, company: dict, domain: str) -> list[str]:
    """Call Dropleads only for a company crawl4ai kept. Compare employees and founded year."""
    fill = getattr(client, "fill_company", None)
    if fill is None or not await job.allow():
        return []
    extra = await fill(domain)
    await _remember(job, extra)
    company["cost_micro"] += extra.cost_micro
    if extra.source:
        company["sources"].append(extra.source)
    facts = _dropleads_facts(extra.output if isinstance(extra.output, dict) else {})
    flags = []
    found = normalize_domain(facts["domain"])
    if found and found != normalize_domain(domain):
        flags.append(f"dropleads returned {found}, a different company")
    else:
        if facts["phone"] and not company.get("phone"):
            company["phone"] = clean_phone(facts["phone"])
        if facts["revenue"] and not company.get("revenue"):
            company["revenue"] = believable_revenue(facts["revenue"])
        if facts["funding"] and not company.get("funding"):
            company["funding"] = clean_revenue(str(facts["funding"]))
        gap = employee_gap(company.get("employees"), facts["employees"])
        if gap:
            flags.append(gap)
        year = founded_gap(company.get("founded_year"), facts["founded_year"])
        if year:
            flags.append(year)
    return flags


def _phone_country(job: Job) -> str:
    """The one country phone checks compare against. Several countries skip the phone checks."""
    codes = country_codes(job.filters.country or "")
    return codes[0] if len(codes) == 1 else ""


def _wants_us_count(job: Job) -> bool:
    code = country_code(job.filters.country or "")
    return code in {"", "US"} and bool(job.filters.country or job.filters.state or job.filters.city)


async def _drop_uncovered(job: Job, client, seeds: list[dict]) -> list[dict]:
    """Skip domains Ocean does not cover, before any paid enrich call."""
    check = getattr(client, "covered_domains", None)
    if check is None or not seeds:
        return seeds
    domains = [seed.get("domain") or "" for seed in seeds]
    try:
        covered = await check(domains)
    except TregError as exc:
        job.log(f"Coverage check failed ({exc.message}). Continuing with the page.")
        return seeds
    if covered is None:
        job.log("Coverage check returned nothing readable. Continuing with the page.")
        return seeds
    kept = [seed for seed in seeds if normalize_domain(seed.get("domain") or "") in covered]
    job.log(f"Coverage: {len(kept)} of {len(seeds)} domains are already covered")
    return kept


async def _apply_us_headcount(job: Job, client, company: dict) -> None:
    count_for = getattr(client, "us_headcount", None)
    linkedin = company.get("linkedin") or ""
    if count_for is None or not linkedin or not _wants_us_count(job) or not await job.allow():
        return
    result = await count_for(linkedin)
    await _remember(job, result)
    company["cost_micro"] += result.cost_micro
    if result.source:
        company["sources"].append(result.source)
    count = us_employee_count(result.output)
    if count is None:
        count = us_employee_count(result.raw)
    if count:
        company["employees"] = str(count)
        fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
        for key in list(fields):
            if _field_leaf(key) in {"people_count", "employee_count", "headcount", "employees", "company_size", "size"}:
                fields.pop(key, None)


async def _apply_naics(job: Job, client, company: dict, domain: str) -> str:
    """Leave the company out when NAICS says it is not a software publisher."""
    key = (job.filters.industry or "").strip().lower()
    lookup = getattr(client, "naics_for", None)
    if lookup is None or not await job.allow():
        return ""
    if key not in {"saas", "software", "software as a service"} and "saas" not in key:
        return ""
    result = await lookup(domain)
    await _remember(job, result)
    company["cost_micro"] += result.cost_micro
    if result.source:
        company["sources"].append(result.source)
    rows = naics_codes(result.output) or naics_codes(result.raw)
    if rows:
        code, name = rows[0]
        if name:
            company["industry"] = name
        company.setdefault("fields", {})["naics"] = ", ".join(f"{item_code} {item_name}".strip() for item_code, item_name in rows[:4])
    elif (job.filters.industry or "").strip().lower() in {"saas", "software", "software as a service"}:
        return "NAICS was not returned"
    return naics_mismatch(job.filters.industry, rows)


def _business_rows(payload) -> list[dict]:
    found: list[dict] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in {"businesses", "results", "companies", "data"} and isinstance(value, list):
                    found.extend(item for item in value if isinstance(item, dict))
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list) and depth < 3:
            for item in node[:20]:
                walk(item, depth + 1)

    walk(payload)
    return found


def _listing_for_domain(payload, domain: str) -> dict | None:
    wanted = normalize_domain(domain)
    for row in _business_rows(payload):
        score = row.get("match_score", row.get("matchScore"))
        try:
            if score is not None and float(score) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        if normalize_domain(company_from_payload(row, row).get("domain") or "") == wanted:
            return row
    return None


async def _fill_place(job: Job, client, company: dict, domain: str) -> list[str]:
    """Openmart supplies the phone and the street, and only when the domain matches."""
    find = getattr(client, "find_business", None)
    if find is None or not await job.allow():
        return []
    query = ", ".join(part for part in (company.get("name"), company.get("city"), company.get("state")) if part) or domain
    result = await find(query)
    await _remember(job, result)
    company["cost_micro"] += result.cost_micro
    if result.source:
        company["sources"].append(result.source)
    row = _listing_for_domain(result.output, domain) or _listing_for_domain(result.raw, domain)
    if not row:
        return ["openmart had no listing on this domain"]
    facts = company_from_payload(row, row)
    if facts.get("phone"):
        company["phone"] = clean_phone(facts["phone"])
    flat = flatten_record(row)
    street = ""
    postal = ""
    for key, value in flat.items():
        leaf = _field_leaf(key)
        text = clean_text(value)
        if leaf in {"street", "street_address", "address1"} and text and not street:
            street = text
        elif leaf == "address" and text and re_search_digit(text) and not street:
            street = text
        elif leaf in {"postal_code", "postal", "zip", "zip_code"} and text and not postal:
            postal = text
    if street and re_search_digit(street):
        company.setdefault("fields", {})["street_address"] = street
    if postal:
        company["postal_code"] = postal
    return []


def re_search_digit(text: str) -> bool:
    return any(character.isdigit() for character in text)


def _apply_saved_money(company: dict, saved: dict) -> list[str]:
    if saved.get("phone") and not company.get("phone"):
        company["phone"] = clean_phone(saved["phone"])
    if saved.get("revenue") and not company.get("revenue"):
        company["revenue"] = believable_revenue(str(saved["revenue"]))
    return []


def _wants_saas(job: Job) -> bool:
    key = (job.filters.industry or "").strip().lower()
    return key in {"saas", "software", "software as a service"} or "saas" in key


async def _llm_saas(company: dict) -> str:
    """yes, no, or unsure. An empty string means the model was not asked."""
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return ""
    text = " ".join(
        part for part in (company.get("name"), company.get("domain"), company.get("industry"), described_text(company)) if part
    ).strip()
    if not text:
        return "unsure"
    url, model = llm_target(key, os.environ.get("LLM_BASE_URL") or "", os.environ.get("LLM_MODEL") or "")
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Decide if this company sells SaaS software. "
                    "Reply with JSON {\"verdict\":\"yes\"|\"no\"|\"unsure\"}. "
                    "yes means a software product sold as a service. "
                    "no means consulting, IT services, a job board, a marketplace, or another business that is not SaaS. "
                    "unsure only when the text is too thin to tell."
                ),
            },
            {"role": "user", "content": text[:4000]},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=25) as http:
            response = await http.post(
                url + "/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return "unsure"
    verdict = str(data.get("verdict") or "").strip().lower()
    return verdict if verdict in {"yes", "no", "unsure"} else "unsure"


async def _saas_check(job: Job, client, company: dict, domain: str, saved: dict) -> str:
    if not _wants_saas(job):
        return ""
    verdict = str(saved.get("saas") or "")
    if verdict not in {"yes", "no", "unsure"}:
        judge = getattr(client, "judge_saas", None)
        verdict = str(await judge(company) or "") if judge is not None else await _llm_saas(company)
        if verdict in {"yes", "no", "unsure"} and client.__class__.__name__ == "TregClient":
            remember(domain, saas=verdict)
    if verdict == "no":
        return "leave: the description is not SaaS"
    if verdict != "unsure":
        return ""
    if saved.get("naics_called"):
        note = str(saved.get("naics_note") or "")
    else:
        note = await _apply_naics(job, client, company, domain)
        if client.__class__.__name__ == "TregClient":
            remember(domain, naics_called=True, naics_note=note)
    if note.endswith("is not a software publisher"):
        return f"leave: {note}"
    return note


def _page_facts(output, raw) -> dict:
    """Street, description, and headcount from crawl4ai. Headcount is used only near the edge of the range."""
    blobs = []
    if isinstance(output, dict):
        blobs.append(output)
        nested = output.get("data")
        if isinstance(nested, dict):
            blobs.append(nested)
    if isinstance(raw, dict):
        blobs.append(raw)
        nested = raw.get("data")
        if isinstance(nested, dict):
            blobs.append(nested)
    flat: dict = {}
    domain = ""
    employees = ""
    exact_employees = ""
    state = ""
    for blob in blobs:
        payload = company_from_payload(blob, {})
        domain = domain or payload.get("domain") or ""
        count, _evidence = size_evidence(payload.get("employees"), {})
        if count and not exact_employees:
            exact_employees = str(count)
        elif payload.get("employees") and not employees:
            employees = payload.get("employees")
        state = state or payload.get("state") or ""
        for key, value in flatten_record(blob).items():
            flat.setdefault(key, value)
    employees = exact_employees or employees
    street = ""
    description = ""
    for key, value in flat.items():
        leaf = _field_leaf(str(key))
        text = clean_text(value)
        if not text:
            continue
        if leaf in {"street", "street_address", "address1"} and not street:
            street = text
        elif leaf == "address" and any(character.isdigit() for character in text) and not street:
            street = text
        elif leaf == "description" and not description:
            description = text
    return {"domain": domain, "street": street, "description": description, "employees": employees, "state": state}


def _dropleads_record(output, raw) -> dict:
    """Employees, place, LinkedIn, phone, and revenue. The Hunter name stays the name."""
    row = output if isinstance(output, dict) else {}
    payload = company_from_payload(row, raw if isinstance(raw, dict) else {})
    funding = ""
    flat = dict(flatten_record(row))
    if isinstance(raw, dict):
        for key, value in flatten_record(raw).items():
            flat.setdefault(key, value)
    keywords = ""
    for key, value in flat.items():
        leaf = _field_leaf(str(key))
        text = clean_text(value)
        if not text:
            continue
        if leaf in {"total_funding", "funding"} and not funding:
            funding = text
        elif leaf in {"keywords", "keyword"} and not keywords:
            keywords = text
    payload["funding"] = funding
    payload["keywords"] = keywords
    return payload


def _title_like_name(name: str) -> bool:
    text = clean_text(name)
    return len(text) > 48 or len(text.split()) >= 6


def _apply_dropleads(company: dict, facts: dict) -> None:
    if facts.get("employees"):
        company["employees"] = str(facts["employees"])
    for key in ("city", "state", "country", "country_code", "location", "linkedin"):
        if facts.get(key):
            company[key] = facts[key]
    if facts.get("phone"):
        company["phone"] = clean_phone(facts["phone"])
    if facts.get("revenue"):
        company["revenue"] = believable_revenue(facts["revenue"])
    if facts.get("funding"):
        company["funding"] = clean_revenue(str(facts["funding"]))
    if facts.get("founded_year"):
        company["founded_year"] = _year(str(facts["founded_year"]))
    if facts.get("industry"):
        company["industry"] = facts["industry"]
        company.setdefault("fields", {})["dropleads_industry"] = facts["industry"]
    if facts.get("keywords"):
        company.setdefault("fields", {})["keywords"] = facts["keywords"]
    listed = clean_company_name(str(facts.get("name") or ""), company.get("domain") or "", company.get("linkedin") or "")
    if listed:
        company.setdefault("fields", {})["dropleads_name"] = listed
        current = clean_text(company.get("name") or "")
        if not current or _title_like_name(current):
            company["name"] = listed
    company["name"] = clean_company_name(
        company.get("name") or "",
        company.get("domain") or "",
        company.get("linkedin") or "",
    )


_MANUAL_REVIEW = ("straviso",)


def _trust_crawl_headcount(low: int | None, high: int | None) -> bool:
    """crawl4ai people_count is unreliable for small size bands such as 11–50."""
    if high is not None and high <= 50:
        return False
    if low is not None and high is not None and high <= 50:
        return False
    return True


def _headcount_band(count: int | None, low: int | None, high: int | None) -> str:
    """inside, edge, or outside.

    For 51–200: inside-near edges are 51–60 and 180–200; outside-near edges are 40–50 and 201–250.
    Small bands (max ≤ 50) stay inside when Dropleads is in range — crawl4ai is not consulted.
    """
    if count is None:
        return "missing"
    if low is None and high is None:
        return "inside"
    if low is not None and high is not None and low <= count <= high:
        if _trust_crawl_headcount(low, high) and (count <= low + 9 or count >= high - 20):
            return "edge"
        return "inside"
    if not _trust_crawl_headcount(low, high):
        return "outside"
    if low is not None and (high is None or count < low):
        under = max(10, int(round((low or 0) * 0.22)))
        if high is None:
            return "inside" if count >= low else ("edge" if count >= low - under else "outside")
        return "edge" if count >= low - under else "outside"
    over = max(10, int(round((high or 0) * 0.25)))
    if high is not None and count > high:
        return "edge" if count <= high + over else "outside"
    if low is not None and count >= low:
        return "inside"
    return "outside"


def _missing_location_note(company: dict, *, state: str = "", country: str = "") -> str:
    """When the query asks for a place, a blank location cannot be treated as a match."""
    if not (state or country):
        return ""
    text = " ".join(
        clean_text(company.get(key))
        for key in ("city", "state", "country", "country_code", "location")
    ).strip()
    if text:
        return ""
    return "location was not reported"


def _non_business_domain(domain: str) -> str:
    host = (normalize_domain(domain) or "").lower()
    if not host:
        return ""
    if host.endswith((".edu", ".gov", ".mil")) or ".k12." in host:
        return "not a for-profit company domain"
    return ""


def _headcount_reason(dropleads_count, crawl_count, span: str) -> str:
    """employees 39 (Dropleads), 45 (crawl4ai) – outside 51–200."""
    left = dropleads_count if dropleads_count is not None else "not reported"
    if crawl_count is None and dropleads_count is not None:
        return f"employees {left} (Dropleads) – outside {span}"
    right = crawl_count if crawl_count is not None else "not reported"
    return f"employees {left} (Dropleads), {right} (crawl4ai) – outside {span}"


def _money_amount(value) -> int | None:
    text = clean_text(value).replace(",", "").replace("$", "").strip()
    if not text:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmb])?", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    factor = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[unit]
    return int(number * factor)


def _revenue_flags(company: dict) -> list[str]:
    """Flag a figure below $20K or above $1M per employee. The row stays."""
    count, _evidence = size_evidence(company.get("employees"), {})
    if not count:
        return []
    notes = []
    for label in ("revenue", "funding"):
        amount = _money_amount(company.get(label))
        if not amount:
            continue
        each = amount / count
        if each < 20_000 or each > 1_000_000:
            notes.append(f"{label} is ${each:,.0f} per employee")
    return notes


def _street_state_flag(street: str, page_state: str, listed_state: str) -> str:
    wanted = state_name(listed_state) or ""
    if not wanted:
        return ""
    found = set(_states_named(street or ""))
    named = state_name(page_state or "")
    if named:
        found.add(named)
    found.discard("")
    if not found or wanted in found:
        return ""
    return f"street address is in {', '.join(sorted(found))}, not {wanted}"


def _wants_industry_check(job: Job) -> bool:
    """The AI industry check has no ground truth to be scored against, so it runs only when asked for."""
    return job.options.industry_check and bool((job.filters.industry or "").strip())


def _verdict_pair(value) -> tuple[str, str]:
    if isinstance(value, dict):
        verdict = str(value.get("verdict") or "")
        reason = clean_text(value.get("reason") or "")
    else:
        verdict, reason = str(value or ""), ""
    key = verdict.strip().lower()
    if key in {"saas", "yes"}:
        return "SaaS", reason
    if key in {"services", "service", "no"}:
        return "services", reason
    if key == "other":
        return "other", reason
    if key == "unsure":
        return "unsure", reason
    return "", reason


def _match_pair(value) -> tuple[str, str]:
    """match keeps the row. no removes it. Used for every industry that is not SaaS."""
    if isinstance(value, dict):
        verdict = str(value.get("verdict") or "")
        reason = clean_text(value.get("reason") or "")
    else:
        verdict, reason = str(value or ""), ""
    key = verdict.strip().lower()
    if key in {"match", "yes", "saas"}:
        return "match", reason
    if key in {"no", "other", "services", "service"}:
        return "no", reason
    if key == "unsure":
        return "unsure", reason
    return "", reason


async def _final_host(domain: str) -> str:
    """The host after redirects. A failed request keeps the host we already have."""
    host = normalize_domain(domain) or ""
    if not host:
        return ""
    reached = host
    url = f"https://{host}"
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            for _hop in range(6):
                response = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
                reached = normalize_domain(str(response.url)) or reached
                if not response.is_redirect or response.next_request is None:
                    break
                url = str(response.next_request.url)
                reached = normalize_domain(url) or reached
    except httpx.HTTPError:
        pass
    return reached


async def _same_site(left: str, right: str) -> bool:
    """True when the two domains are the same company, including a www prefix or a redirect."""
    first = normalize_domain(left) or ""
    second = normalize_domain(right) or ""
    if not second:
        return True
    if not first or first == second:
        return first == second
    final_first = normalize_domain(await _final_host(first)) or first
    if final_first == second:
        return True
    final_second = normalize_domain(await _final_host(second)) or second
    return final_first == final_second


def _judgment_text(company: dict) -> str:
    """Name, domain, and description. Do not treat the provider industry label as proof."""
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    keywords = clean_text(fields.get("keywords") or "")
    description = described_text(company)
    parts = []
    if company.get("name"):
        parts.append(f"Name: {company.get('name')}")
    if company.get("domain"):
        parts.append(f"Domain: {company.get('domain')}")
    if keywords:
        parts.append(f"Keywords: {keywords}")
    if description:
        parts.append(f"Description: {description}")
    return "\n".join(parts).strip()


_JUDGMENT_VERSION = 4
_HESITANT_REASON = (
    "no clear",
    "unclear",
    "not clear",
    "likely",
    "probably",
    "possibly",
    "might be",
    "may be",
    "appears",
    "seems",
    "not enough",
    "insufficient",
    "cannot tell",
    "can't tell",
    "hard to tell",
    "guess",
)


def _industry_prompt(industry: str, saas: bool) -> str:
    for_profit = (
        "First decide if this is an operating for-profit business. "
        "If it is a nonprofit, school, university, government body, library, association, "
        "research center, or a shut-down / defunct company, it is not a match. "
    )
    if saas:
        return (
            "Classify the company from its name, domain, and description only. "
            "Ignore any provider industry label. "
            + for_profit
            + "Then classify as SaaS, Services, Other, or unsure. "
            "Answer SaaS ONLY when the text clearly shows the company's main product is subscription software "
            "delivered online (customers log in and use it over the internet on a recurring subscription). "
            "Answer Services ONLY when the text clearly shows consulting, IT services, agency, staffing, or outsourcing. "
            "Answer Other for hardware, marketplace, media, retail, education, news, "
            "one-time / perpetual / licensed software, nonprofits, schools, and government. "
            "If the description does not make the business model clear, answer unsure. Do not guess. "
            'Reply with JSON {"verdict":"SaaS"|"services"|"other"|"unsure","reason":"one short reason"}.'
        )
    asked = clean_text(industry) or "the requested industry"
    return (
        f"The user asked for {asked} companies. "
        "Judge from the name, domain, and description only. Ignore any provider industry label. "
        + for_profit
        + f"match only when the text clearly shows an operating for-profit company whose main business is {asked}. "
        "no when it is a different kind of organization or not for-profit. "
        "unsure when the description does not make it clear. Do not guess. "
        'Reply with JSON {"verdict":"match"|"no"|"unsure","reason":"one short reason"}.'
    )


def _soften_guess(verdict: str, reason: str) -> tuple[str, str]:
    """Hesitant Other/Services guesses become unsure so real SaaS companies are not removed."""
    if verdict not in {"services", "other", "no"}:
        return verdict, reason
    text = (reason or "").lower()
    if any(token in text for token in _HESITANT_REASON):
        return "unsure", reason or "the industry check was unsure"
    return verdict, reason


_LLM_LOCK = asyncio.Semaphore(1)
_LAST_LLM_AT = 0.0


def _parse_llm_json(content) -> dict:
    """Accept plain JSON, fenced JSON, or a first JSON object embedded in text."""
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        content = "".join(parts)
    text = clean_text(content)
    if not text:
        raise json.JSONDecodeError("empty content", "", 0)
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise TypeError("industry check JSON was not an object")
    return data


async def _llm_industry(company: dict, industry: str, *, saas: bool) -> tuple[str, str, str]:
    """Industry verdict plus a short reason. The third value is why the check did not answer."""
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return "", "", "the industry check had no model key"
    text = _judgment_text(company)
    if not text:
        return "", "", "the description was too short to judge"
    url, model = llm_target(key, os.environ.get("LLM_BASE_URL") or "", os.environ.get("LLM_MODEL") or "")
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _industry_prompt(industry, saas)},
            {"role": "user", "content": text[:4000]},
        ],
    }
    last_problem = "the industry check did not answer"
    global _LAST_LLM_AT
    async with _LLM_LOCK:
        gap = time.monotonic() - _LAST_LLM_AT
        if _LAST_LLM_AT and gap < 1.5:
            await asyncio.sleep(1.5 - gap)
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=45) as http:
                    response = await http.post(
                        url + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                _LAST_LLM_AT = time.monotonic()
                if response.status_code == 429:
                    last_problem = "the industry check returned HTTP 429"
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                if response.status_code in {500, 502, 503, 504}:
                    last_problem = f"the industry check returned HTTP {response.status_code}"
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if response.status_code >= 400:
                    return "", "", f"the industry check returned HTTP {response.status_code}"
                message = response.json()["choices"][0]["message"]
                content = message.get("content")
                if not content and message.get("reasoning"):
                    content = message.get("reasoning")
                data = _parse_llm_json(content)
            except httpx.TimeoutException:
                last_problem = "the industry check timed out"
                _LAST_LLM_AT = time.monotonic()
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except httpx.HTTPError:
                last_problem = "the industry check connection failed"
                _LAST_LLM_AT = time.monotonic()
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                last_problem = "the industry check did not return usable JSON"
                _LAST_LLM_AT = time.monotonic()
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            verdict, reason = _verdict_pair(data) if saas else _match_pair(data)
            if not verdict:
                return "", "", "the industry check did not answer"
            return verdict, reason, ""
    return "", "", last_problem


def _description_is_thin(company: dict) -> bool:
    """True when description, listed industry, and keywords together are still too short to judge."""
    text = _judgment_text(company)
    return len(text) < 40 or len(text.split()) < 8


def _on_manual_review(company: dict) -> bool:
    blob = " ".join(str(company.get(key) or "") for key in ("name", "domain")).lower()
    return any(name in blob for name in _MANUAL_REVIEW)


def _ai_source(saas: bool) -> str:
    return "ai.saas" if saas else "ai.industry"


def _usable_street(street: str) -> bool:
    return bool(street) and any(character.isdigit() for character in street)


def _street_confirms_state(street: str, page_state: str, wanted_state: str) -> bool:
    wanted = state_name(wanted_state) or ""
    if not wanted:
        return False
    found = set(_states_named(street or ""))
    named = state_name(page_state or "")
    if named:
        found.add(named)
    return wanted in found


def _phone_hq_decision(
    phone: str,
    street: str,
    page_state: str,
    *,
    state: str,
    country: str,
) -> tuple[str, str]:
    """Out-of-state phone: keep when the street is in the asked state; review when the street is missing.

    A Texas street with an out-of-state phone is kept but flagged — Dropleads often lists an office, not HQ.
    """
    place = phone_location_flag(phone, state=state, country=country)
    if not place.startswith("phone area code"):
        return "", ""
    wanted = state_name(state) or state
    if state and _street_confirms_state(street, page_state, state):
        return f"{place}; street is in {wanted}", ""
    if not _usable_street(street):
        return "", "phone is out of state and street is missing"
    return place, ""


async def _naics_tiebreak(job: Job, client, company: dict, domain: str, saved: dict) -> tuple[str, str]:
    """For a few unsure SaaS rows only. Returns (keep|drop|review, note)."""
    if not _wants_saas(job):
        return "review", "the industry check was unsure"
    if saved.get("naics_called"):
        note = str(saved.get("naics_note") or "")
    else:
        note = await _apply_naics(job, client, company, domain)
        if client.__class__.__name__ == "TregClient":
            remember(domain, naics_called=True, naics_note=note)
    if note and "not a software publisher" in note:
        return "drop", note
    if note:
        return "review", note
    company["industry"] = "SaaS"
    return "keep", "NAICS confirmed software"


def _category_reason(verdict: str, why: str) -> str:
    if verdict == "no":
        label = "Not a match"
    elif verdict == "services":
        label = "Services"
    else:
        label = "Other"
    text = clean_text(why)
    if not text:
        return label
    if text.lower().startswith(label.lower()):
        return text
    return f"{label}: {text}"


def _cached_industry_verdict(saved: dict, industry: str, *, saas: bool, current_version_only: bool) -> tuple[str, str]:
    blob = saved.get("industry_judgment") if isinstance(saved.get("industry_judgment"), dict) else None
    if not blob:
        return "", ""
    if str(blob.get("industry") or "").strip().lower() != industry.strip().lower():
        return "", ""
    if current_version_only and int(blob.get("version") or 0) != _JUDGMENT_VERSION:
        return "", ""
    return _verdict_pair(blob) if saas else _match_pair(blob)


async def _industry_judgment(job: Job, client, company: dict, domain: str, saved: dict) -> tuple[str, str, str]:
    """Ask when the query names an industry. Reuse a current cached verdict; on failure fall back to any saved one."""
    industry = (job.filters.industry or "").strip()
    if not industry:
        return "", "", ""
    saas = _wants_saas(job)
    cached_verdict, cached_reason = _cached_industry_verdict(saved, industry, saas=saas, current_version_only=True)
    if cached_verdict:
        return cached_verdict, cached_reason, ""
    judge_industry = getattr(client, "judge_industry", None)
    judge_saas = getattr(client, "judge_saas", None)
    if judge_industry is not None:
        raw = await judge_industry(company, industry)
        verdict, reason = _verdict_pair(raw) if saas else _match_pair(raw)
        problem = "" if verdict else "the industry check did not answer"
    elif judge_saas is not None:
        raw = await judge_saas(company)
        verdict, reason = _verdict_pair(raw) if saas else _match_pair(raw)
        problem = "" if verdict else "the industry check did not answer"
    else:
        verdict, reason, problem = await _llm_industry(company, industry, saas=saas)
    if verdict:
        verdict, reason = _soften_guess(verdict, reason)
    if not verdict and problem:
        fallback, fallback_reason = _cached_industry_verdict(saved, industry, saas=saas, current_version_only=False)
        if fallback:
            job.log(f"{domain}: reused the saved industry answer after {problem}")
            return fallback, fallback_reason, ""
    if verdict and client.__class__.__name__ == "TregClient":
        remember(
            domain,
            industry_judgment={
                "verdict": verdict,
                "reason": reason,
                "industry": industry,
                "version": _JUDGMENT_VERSION,
            },
        )
    return verdict, reason, problem


def _street_page_needs_fetch(saved: dict, wants_description: bool) -> bool:
    """Fetch crawl4ai when there is no saved page, or once more when the saved page cannot be used."""
    page = saved.get("street_page") if isinstance(saved.get("street_page"), dict) else None
    if page is None:
        return True
    if saved.get("street_refetched"):
        return False
    street = str(page.get("street") or "")
    if not any(character.isdigit() for character in street):
        return True
    if wants_description and _description_is_thin({"description": page.get("description") or ""}):
        return True
    return False


def _review_note(company: dict, thin: bool) -> str:
    if _on_manual_review(company):
        return "on the manual review list"
    if thin:
        return "the description was too short to judge"
    return ""


async def _drop(job: Job, company: dict, reason: str) -> None:
    domain = company.get("domain") or ""
    name = clean_company_name(company.get("name") or "", domain, company.get("linkedin") or "")
    if not name or _title_like_name(name):
        listed = clean_text((company.get("fields") or {}).get("dropleads_name") or "")
        if listed:
            name = listed
    async with job.lock:
        job.skipped += 1
        job.removed.append(
            {
                "domain": domain,
                "name": name or domain,
                "reason": reason,
                "employees": company.get("employees") or "",
                "phone": company.get("phone") or "",
                "city": company.get("city") or "",
                "state": company.get("state") or "",
                "website": company.get("website") or "",
                "cost_micro": company.get("cost_micro") or 0,
            }
        )
    job.log(f"{company.get('domain')}: left out, {reason}")


def _json_safe(value):
    if not isinstance(value, dict):
        return {}
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def _cache_decision(saved: dict) -> tuple[dict | None, bool]:
    """Parsed Dropleads facts, and whether a stored row must be bought again once.

    An older cache that kept only a few fields is refetched. A full response that is
    still missing industry, founded year, or the source is refetched once.
    """
    if not isinstance(saved, dict) or not saved:
        return None, False
    raw = saved.get("dropleads_response")
    source = clean_text(saved.get("dropleads_source") or "")
    if isinstance(raw, dict) and raw:
        facts = _dropleads_record(raw, {})
        facts["source"] = source
        missing = not facts.get("founded_year") or not facts.get("industry") or not source
        if missing and not saved.get("dropleads_refetched"):
            return None, True
        return facts, False
    if saved.get("dropleads_full") or saved.get("dropleads"):
        return None, True
    return None, False


async def _resolve_identity(job: Job, client, domain: str) -> dict:
    """Who owns this domain, before any facts are bought for it.

    The site's redirect decides the owning domain. Crustdata's free identity lookup then names
    the company and its LinkedIn page; a weak or off-domain match is ignored.
    """
    final = normalize_domain(await _final_host(domain)) or domain
    owner, parked = redirect_identity(domain, final)
    if parked:
        return {"domain": domain, "parked": parked}
    out = {"domain": owner or domain}
    identify = getattr(client, "identify_company", None)
    if identify is None:
        return out
    live = client.__class__.__name__ == "TregClient"
    saved = cached(out["domain"]) if live else {}
    found = saved.get("identity") if isinstance(saved.get("identity"), dict) else None
    if found is None:
        try:
            result = await identify(out["domain"])
        except TregError as exc:
            job.log(f"{out['domain']}: identity lookup failed ({exc.message}), continuing without it")
            return out
        except httpx.TransportError as exc:
            job.log(f"{out['domain']}: identity lookup failed ({failure_text(exc)}), continuing without it")
            return out
        await _remember(job, result)
        found = crustdata_identity(result.raw if result.raw is not None else result.output, out["domain"])
        if live:
            remember(out["domain"], identity=found)
    if found:
        out.update({key: value for key, value in found.items() if key != "domain"})
    return out


def _linkedin_check(company: dict, identity: dict) -> str:
    """Compare Crustdata's LinkedIn page with the enrich source's. A disagreement is a review note."""
    named = identity.get("linkedin") or ""
    listed = company.get("linkedin") or ""
    if not named:
        return ""
    if not listed:
        company["linkedin"] = named
        return ""
    if linkedin_slug(named) != linkedin_slug(listed):
        company["fields"]["identity_linkedin"] = named
        return f"LinkedIn differs between sources ({named} vs {listed})"
    company["fields"]["identity_check"] = "Crustdata and Dropleads agree on LinkedIn"
    return ""


_LINK_SHORTENERS = {"bit.ly", "lnkd.in", "linktr.ee", "goo.gl", "tinyurl.com", "t.co", "ow.ly", "linkedin.com"}


def _place_key(value: str) -> str:
    """Place names compared without accents, case, or punctuation: Oiã and Oia are the same town."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _linkedin_row(output, raw) -> dict:
    """The one company row the LinkedIn page reader returned."""
    for source in (output, raw):
        rows = source.get("rows") if isinstance(source, dict) else source if isinstance(source, list) else None
        for row in rows or []:
            if isinstance(row, dict) and (row.get("size") or row.get("headquarters") or row.get("website")):
                return {key: clean_text(row.get(key) or "") for key in ("name", "size", "headquarters", "website", "industry")}
    return {}


async def _verify_on_linkedin(job: Job, client, company: dict, domain: str, saved: dict) -> tuple[str, list[str], str]:
    """(LinkedIn check column, size/HQ flags, review note) from the company's own LinkedIn page.

    This step only checks. It never removes a company: a different website is a review note,
    a different size or headquarters is a flag on the row.
    """
    read = getattr(client, "linkedin_page", None)
    if read is None:
        return "", [], ""
    url = company.get("linkedin") or ""
    if not url:
        return "not checked: no LinkedIn page", [], ""
    page = saved.get("linkedin_page") if isinstance(saved.get("linkedin_page"), dict) else None
    if page is None:
        if not await job.allow():
            return "not checked: budget reached", [], ""
        try:
            result = await read(url)
        except TregError as exc:
            if exc.status != 404:
                job.log(f"{domain}: LinkedIn page could not be read ({exc.message})")
                return "not checked: LinkedIn page could not be read", [], ""
            page = {}
        except httpx.TransportError as exc:
            job.log(f"{domain}: LinkedIn page could not be read ({failure_text(exc)})")
            return "not checked: LinkedIn page could not be read", [], ""
        else:
            await _remember(job, result)
            company["cost_micro"] += result.cost_micro
            page = _linkedin_row(result.output, result.raw)
        if client.__class__.__name__ == "TregClient":
            remember(domain, linkedin_page=page)
    if not page:
        return "not checked: LinkedIn page could not be read", [], ""
    if "crawl4ai.linkedin.company.profile" not in company["sources"]:
        company["sources"].append("crawl4ai.linkedin.company.profile")
    confirmed: list[str] = []
    flags: list[str] = []
    note = ""
    site = normalize_domain(page.get("website") or "") or ""
    if site in _LINK_SHORTENERS:
        site = ""
    if site:
        if site == domain or brand(site) == brand(domain):
            confirmed.append("website")
        else:
            note = f"LinkedIn page lists the website {site}, not {domain}"
    size_text = page.get("size") or ""
    low, high = range_bounds(size_text)
    ours_low, ours_high = employee_bounds(company.get("employees") or "")
    if size_text:
        company["fields"]["linkedin_page_size"] = size_text
    if low is not None and ours_low is not None:
        middle = (ours_low + (ours_high or ours_low)) / 2
        if low <= middle <= (high if high is not None else low * 10):
            confirmed.append("size")
        else:
            flags.append(f"LinkedIn page says {size_text}, not {company.get('employees')}")
    headquarters = page.get("headquarters") or ""
    if headquarters:
        company["fields"]["linkedin_page_hq"] = headquarters
    def same(a: str, b: str) -> bool:
        return bool(a and b) and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)))

    city = _place_key(company.get("city") or "")
    region = {_place_key(company.get(key) or "") for key in ("state", "country")}
    region.add(_place_key(country_label(country_code(company.get("country") or ""))))
    hq_parts = {_place_key(part) for part in headquarters.split(",")} - {""}
    if city and hq_parts:
        names_city = any(same(part, city) for part in hq_parts)
        only_region = all(any(same(part, place) for place in region) for part in hq_parts)
        if names_city or only_region:
            confirmed.append("HQ")
        else:
            flags.append(f"LinkedIn page HQ is {headquarters}, not {company.get('city')}")
    problems = ([note] if note else []) + flags
    if problems:
        return "mismatch: " + "; ".join(problems), flags, note
    if confirmed:
        return "confirmed: " + ", ".join(confirmed), [], ""
    return "not checked: LinkedIn page has no size, HQ, or website", [], ""


async def _leadocean_location(job: Job, client, company: dict, domain: str, saved: dict) -> None:
    """leadocean supplies city, state, and country. Dropleads' values stay when leadocean has none.

    Called only after the size check passed, so a company that is dropped for size costs nothing here.
    """
    locate = getattr(client, "locate_company", None)
    if locate is None:
        return
    place = saved.get("leadocean_place") if isinstance(saved.get("leadocean_place"), dict) else None
    if place is None:
        if not await job.allow():
            return
        try:
            result = await locate(domain)
        except TregError as exc:
            if exc.status != 404:
                job.log(f"{domain}: leadocean location failed ({exc.message}), keeping Dropleads")
                return
            place = {}
        except httpx.TransportError as exc:
            job.log(f"{domain}: leadocean location failed ({failure_text(exc)}), keeping Dropleads")
            return
        else:
            await _remember(job, result)
            company["cost_micro"] += result.cost_micro
            record = extract("leadocean.companies.enrich", {"output": result.output, "raw": result.raw})
            place = {key: record.get(key) or "" for key in ("city", "state", "country")}
        if client.__class__.__name__ == "TregClient":
            remember(domain, leadocean_place=place)
    if not company.get("location"):
        company["location"] = ", ".join(company.get(key) or "" for key in ("city", "state", "country") if company.get(key))
    if not any(place.values()):
        return
    place = dict(place)
    theirs = country_code(place.get("country") or "")
    ours = country_code(company.get("country") or "")
    if theirs and ours and theirs != ours:
        said = ", ".join(place.get(key) or "" for key in ("city", "state", "country") if place.get(key))
        company["fields"]["leadocean_location"] = said
        job.log(f"{domain}: leadocean says {said}, Dropleads says {country_label(ours)}; keeping Dropleads")
        return
    if theirs:
        place["country"] = country_label(theirs)
    before = ", ".join(company.get(key) or "" for key in ("city", "state", "country") if company.get(key))
    for key in ("city", "state", "country"):
        if place.get(key):
            company[key] = place[key]
    if place.get("country"):
        company["country_code"] = country_code(place["country"]) or ""
    company["location"] = ", ".join(company.get(key) or "" for key in ("city", "state", "country") if company.get(key))
    if before and before != company["location"]:
        company["fields"]["dropleads_location"] = before
    if "leadocean.companies.enrich" not in company["sources"]:
        company["sources"].append("leadocean.companies.enrich")


def _linkedin_headcount(job: Job, company: dict, identity: dict, exact: int | None) -> tuple[str, str, str]:
    """(verdict, LinkedIn range, review note). The LinkedIn range from Crustdata decides size first.

    pass: the whole range is inside the ask. fail: it misses it. edge: it crosses an end of the
    ask, so Dropleads' exact count decides; with no exact count the row is kept for review.
    """
    text = identity.get("employees") or ""
    low, high = range_bounds(text)
    if low is None or (job.filters.employees_min is None and job.filters.employees_max is None):
        return "unknown", "", ""
    ask_low = job.filters.employees_min or 0
    ask_high = job.filters.employees_max or 10**9
    verdict = size_verdict(text, ask_low, ask_high)
    company["fields"]["linkedin_employees"] = text
    top = high if high is not None else low * 10
    if verdict == "pass":
        if exact is not None and not (low <= exact <= top):
            company["fields"]["dropleads_employees"] = str(exact)
            company["employees"] = text
        return "pass", text, ""
    if verdict == "edge":
        if exact is None:
            company["employees"] = text
            return "edge", text, f"LinkedIn size {text} crosses the edge of the range and no exact count was found"
        if not (low <= exact <= top):
            return "edge", text, f"LinkedIn size {text} and Dropleads count {exact} disagree"
    return verdict, text, ""


async def _agree_company(job: Job, client, index: int, seed: dict) -> None:
    """Hunter keeps the name and domain. Dropleads fills the facts. crawl4ai fills the street.

    Identity is locked first: the site's redirect picks the owning domain (windows.com →
    microsoft.com), Crustdata names the company, and every later lookup uses that domain.
    """
    domain = locked_domain(seed.get("domain") or "") or (seed.get("domain") or "")
    company = _blank(domain, seed.get("company_name") or "", index)
    company["domain"] = domain
    company.setdefault("fields", {})["identity_domain"] = domain
    try:
        if not await job.allow():
            company["status"] = "stopped before enrich"
            job.companies.append(company)
            job.done += 1
            return
        identity = await _resolve_identity(job, client, domain)
        if identity.get("parked"):
            await _drop(job, company, identity["parked"])
            return
        owner = identity.get("domain") or domain
        if owner != domain:
            job.log(f"{domain}: the site redirects to {owner}, so this row is {identity.get('name') or owner}")
            company["fields"]["searched_domain"] = domain
            domain = owner
            company["domain"] = domain
            company["website"] = f"https://{domain}"
            company["name"] = identity.get("name") or ""
            company["fields"]["identity_domain"] = domain
        elif identity.get("name") and (not company.get("name") or _title_like_name(company["name"])):
            company["name"] = identity["name"]
        if identity.get("name"):
            company["fields"]["identity_name"] = identity["name"]
            company["sources"].append("crustdata.companies.identify")
        async with job.lock:
            repeat = domain in job.identities
            job.identities.add(domain)
        if repeat:
            await _drop(job, company, f"same company as an earlier row ({domain})")
            return
        saved = cached(domain) if client.__class__.__name__ == "TregClient" else {}
        facts, refetch = _cache_decision(saved)
        if facts is not None:
            job.log(f"{domain}: reused the saved company facts")
            if facts.get("source"):
                company["sources"].append(facts["source"])
        else:
            fill = getattr(client, "fill_company", None)
            if fill is None or not await job.allow():
                facts = {}
            else:
                if refetch:
                    job.log(f"{domain}: saved record is missing a field, fetching it again")
                extra = await fill(domain)
                await _remember(job, extra)
                company["cost_micro"] += extra.cost_micro
                if extra.source:
                    company["sources"].append(extra.source)
                facts = _dropleads_record(extra.output, extra.raw)
                facts["source"] = extra.source
                if client.__class__.__name__ == "TregClient":
                    remember(
                        domain,
                        dropleads_full=True,
                        dropleads_refetched=bool(refetch),
                        dropleads_source=extra.source,
                        dropleads_response=_json_safe(extra.output),
                    )
        found = normalize_domain((facts or {}).get("domain") or "")
        # Identity gate: do not copy parent-company facts onto the seed domain.
        if found and not await _same_site(domain, found):
            reason = identity_mismatch(domain, found) or (
                f"the lookup returned {found}, a different company from {normalize_domain(domain)}"
            )
            await _drop(job, company, reason)
            return
        _apply_dropleads(company, facts or {})
        company["domain"] = domain
        identity_note = _linkedin_check(company, identity)
        exact, _evidence = size_evidence(company.get("employees"), {})
        if exact:
            company["employees"] = str(exact)
        band = _headcount_band(exact, job.filters.employees_min, job.filters.employees_max)
        span = _employee_span(job.filters.employees_min, job.filters.employees_max)
        linkedin_size, linkedin_range, linkedin_note = _linkedin_headcount(job, company, identity, exact)
        if linkedin_size == "pass":
            band = "inside"
        elif linkedin_size == "edge" and exact is None:
            band = "inside"
        if _non_business_domain(domain) or _non_business_domain(found):
            reason = _non_business_domain(domain) or _non_business_domain(found)
        elif linkedin_size == "fail":
            reason = f"LinkedIn size {linkedin_range} is outside {span}"
        elif exact is None and linkedin_size not in {"pass", "edge"}:
            reason = _headcount_reason(None, None, span)
        elif band == "outside":
            reason = _headcount_reason(exact, None, span)
        else:
            await _leadocean_location(job, client, company, domain, saved)
            reason = location_mismatch(
                company,
                city=job.filters.city,
                state=job.filters.state,
                country=job.filters.country,
            ) or foreign_phone_reason(company.get("phone") or "", state=job.filters.state, country=_phone_country(job))
        if reason:
            await _drop(job, company, reason)
            return
        if linkedin_size == "pass" and exact is not None and company.get("employees") != str(exact):
            exact = None
        page = saved.get("street_page") if isinstance(saved.get("street_page"), dict) else None
        check_industry = _wants_industry_check(job)
        if page is not None and not _street_page_needs_fetch(saved, check_industry):
            if _usable_street(str(page.get("street") or "")):
                job.log(f"{domain}: reused the saved street")
            else:
                job.log(f"{domain}: reused the saved page (no street address)")
        else:
            was_refetch = page is not None
            if was_refetch:
                job.log(f"{domain}: saved page is missing a street or a description, fetching it again")
            enriched = await client.enrich(domain, company["name"])
            await _remember(job, enriched)
            company["cost_micro"] += enriched.cost_micro
            if enriched.source:
                company["sources"].append(enriched.source)
            page = _page_facts(enriched.output, enriched.raw)
            if client.__class__.__name__ == "TregClient":
                remember(domain, street_page=page, street_refetched=was_refetch)
        street = page.get("street") or ""
        phone_needs_street = bool(
            job.filters.state
            and company.get("phone")
            and phone_location_flag(
                company["phone"], state=job.filters.state, country=_phone_country(job)
            ).startswith("phone area code")
            and not _usable_street(street)
        )
        if phone_needs_street and not saved.get("street_hq_refetched") and await job.allow():
            job.log(f"{domain}: out-of-state phone, fetching street again for HQ check")
            enriched = await client.enrich(domain, company["name"])
            await _remember(job, enriched)
            company["cost_micro"] += enriched.cost_micro
            if enriched.source and enriched.source not in company["sources"]:
                company["sources"].append(enriched.source)
            page = _page_facts(enriched.output, enriched.raw)
            street = page.get("street") or street
            if client.__class__.__name__ == "TregClient":
                remember(domain, street_page=page, street_hq_refetched=True)
            saved = {**saved, "street_hq_refetched": True, "street_page": page}
        if (page.get("street") or page.get("description")) and "crawl4ai.companies.enrich" not in company["sources"]:
            company["sources"].append("crawl4ai.companies.enrich")
        crawl_domain = normalize_domain(page.get("domain") or "")
        if crawl_domain and not await _same_site(domain, crawl_domain):
            reason = identity_mismatch(domain, crawl_domain) or (
                f"the lookup returned {crawl_domain}, a different company from {normalize_domain(domain)}"
            )
            await _drop(job, company, reason)
            return
        company["domain"] = domain
        if page.get("description"):
            company["description"] = page["description"]
        street = page.get("street") or street
        if _usable_street(street):
            company["fields"]["street_address"] = street
        note = ""
        headcount_note = linkedin_note
        if band == "edge":
            crawl_count, _crawl_evidence = size_evidence(page.get("employees"), {})
            dropleads_inside = not outside_employee_range(exact, job.filters.employees_min, job.filters.employees_max)
            crawl_inside = crawl_count is not None and not outside_employee_range(
                crawl_count, job.filters.employees_min, job.filters.employees_max
            )
            if crawl_inside:
                job.log(f"{domain}: employees {exact} (Dropleads), {crawl_count} (crawl4ai) – keeping {crawl_count}")
                exact = crawl_count
                company["employees"] = str(exact)
            elif dropleads_inside:
                if crawl_count is None:
                    job.log(f"{domain}: employees {exact} (Dropleads); crawl4ai had no count, keeping Dropleads")
                else:
                    headcount_note = f"employees {exact} (Dropleads), {crawl_count} (crawl4ai) disagree"
                    job.log(f"{domain}: {headcount_note}")
            else:
                await _drop(job, company, _headcount_reason(exact, crawl_count, span))
                return
        location_note = _missing_location_note(
            company, state=job.filters.state, country=job.filters.country
        )
        # Still run the industry check when headcount only disagrees — otherwise schools/libraries stay on the sheet.
        saas = _wants_saas(job)
        review = _review_note(company, check_industry and _description_is_thin(company))
        if review:
            company["industry"] = "needs review"
            note = review
        elif check_industry:
            verdict, why, problem = await _industry_judgment(job, client, company, domain, saved)
            if verdict in {"services", "other", "no"}:
                await _drop(job, company, _category_reason(verdict, why))
                return
            if verdict == "SaaS":
                company["industry"] = "SaaS"
                source = _ai_source(True)
                if source not in company["sources"]:
                    company["sources"].append(source)
                if why:
                    company["fields"]["industry_reason"] = why
                job.log(f"{domain}: industry SaaS")
            elif verdict == "match":
                company["industry"] = (job.filters.industry or "").strip()
                source = _ai_source(False)
                if source not in company["sources"]:
                    company["sources"].append(source)
                if why:
                    company["fields"]["industry_reason"] = why
                job.log(f"{domain}: industry match")
            elif verdict == "unsure":
                # Unsure stays on the sheet for hand review. Do not spend NAICS ($0.025) on a guess.
                company["industry"] = "needs review"
                note = why or "the industry check was unsure"
                company["fields"]["industry_reason"] = note
                job.log(f"{domain}: needs review ({note})")
            elif problem:
                company["industry"] = "needs review"
                note = problem
                company["fields"]["industry_reason"] = note
                job.log(f"{domain}: {problem}")
        if headcount_note:
            note = f"{note}; {headcount_note}" if note else headcount_note
        if location_note:
            note = f"{note}; {location_note}" if note else location_note
        if identity_note:
            note = f"{identity_note}; {note}" if note else identity_note
        tidy_company(company, headcount=exact)
        if linkedin_range:
            company["fields"]["company_size"] = linkedin_range
        flags = []
        check, check_flags, check_note = await _verify_on_linkedin(job, client, company, domain, saved)
        if check:
            company["linkedin_check"] = check
        flags.extend(check_flags)
        if check_note:
            note = f"{note}; {check_note}" if note else check_note
        flags.extend(_revenue_flags(company))
        street_note = _street_state_flag(street, page.get("state") or "", company.get("state") or "")
        if street_note:
            flags.append(street_note)
        if company.get("phone"):
            phone_flag, phone_review = _phone_hq_decision(
                company["phone"],
                street,
                page.get("state") or "",
                state=job.filters.state,
                country=_phone_country(job),
            )
            if phone_flag:
                flags.append(phone_flag)
            if phone_review:
                note = note or phone_review
        if company.get("status") == "not found" or job.options.contacts_per_company == 0:
            contacts = []
        else:
            contacts = await _contacts(job, client, company)
        company["contacts_found"] = len(contacts)
        company["emails_found"] = sum(1 for row in contacts if row["email_status"] in {"found", "verified"})
        job.emails += company["emails_found"]
        others = [flag for flag in flags if flag]
        if note:
            company["status"] = "needs review: " + note if not others else "needs review: " + note + "; flagged: " + "; ".join(others)
        elif others:
            company["status"] = "enriched; flagged: " + "; ".join(others)
        else:
            company["status"] = "enriched"
        job.contacts.extend(contacts)
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}, {company['contacts_found']} contacts, {company['emails_found']} emails")
    except TregError as exc:
        company["status"] = f"error: {exc.message}"
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}")
        if exc.status in {401, 402}:
            job.stop = True
            job.stop_reason = exc.message
    except httpx.TransportError as exc:
        company["status"] = f"error: {failure_text(exc)}"
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}")


async def _one_company(job: Job, client, index: int, seed: dict) -> None:
    if job.mode == "query" and (job.filters.employees_min is not None or job.filters.employees_max is not None):
        await _agree_company(job, client, index, seed)
        return
    domain = seed.get("domain") or ""
    company = _blank(domain, seed.get("company_name") or "", index)
    if isinstance(seed.get("search_fields"), dict):
        company["fields"] = dict(seed["search_fields"])
    if seed.get("preview"):
        _apply_payload(company, seed["preview"])
    try:
        if not await job.allow():
            company["status"] = "stopped before enrich"
            job.companies.append(company)
            job.done += 1
            return
        saved = cached(domain) if client.__class__.__name__ == "TregClient" else {}
        apollo_exact, _apollo_evidence = size_evidence(company.get("employees"), {})
        apollo_employees = str(apollo_exact) if apollo_exact else ""
        apollo_revenue = company.get("revenue") or ""
        apollo_phone = company.get("phone") or ""
        apollo_founded = company.get("founded_year") or ""
        apollo_linkedin = company.get("linkedin") or ""
        if isinstance(saved.get("crawl4ai"), dict):
            enriched = CallResult(endpoint="saved", output=saved["crawl4ai"], cost_micro=0)
            job.log(f"{domain}: reused the saved company page")
        else:
            enriched = await client.enrich(domain, company["name"])
            if client.__class__.__name__ == "TregClient":
                remember(domain, crawl4ai=enriched.output if isinstance(enriched.output, dict) else {})
        await _remember(job, enriched)
        company["cost_micro"] += enriched.cost_micro
        if enriched.source:
            company["sources"].append(enriched.source)
        payload = company_from_payload(enriched.output, enriched.raw, domain=domain, fallback_name=company["name"])
        company["fields"] = flatten_record(enriched.output)
        for key, value in flatten_record(enriched.raw).items():
            company["fields"].setdefault(key, value)
        for key, value in (seed.get("search_fields") or {}).items():
            company["fields"].setdefault(key, value)
        _apply_payload(company, payload)
        if apollo_employees:
            company["employees"] = apollo_employees
        elif reported_headcount(company.get("employees"), company["fields"]):
            company["employees"] = reported_headcount(company.get("employees"), company["fields"])
        if apollo_revenue:
            company["revenue"] = believable_revenue(apollo_revenue)
        if apollo_phone:
            company["phone"] = clean_phone(apollo_phone)
        if apollo_founded:
            company["founded_year"] = apollo_founded
        if apollo_linkedin:
            company["linkedin"] = apollo_linkedin
        exact, evidence = size_evidence(company.get("employees"), company["fields"])
        if apollo_employees:
            exact, evidence = size_evidence(apollo_employees, {})
            company["employees"] = str(exact or apollo_employees)
        elif exact:
            company["employees"] = str(exact)
        tidy_company(company, headcount=exact)
        apollo_has_facts = any((apollo_employees, apollo_revenue, apollo_phone, apollo_founded, apollo_linkedin, company.get("city"), company.get("state")))
        if _lookup_empty(enriched) and not apollo_has_facts:
            company["status"] = "not found"
            job.companies.append(company)
            job.done += 1
            job.log(f"{domain}: not found")
            return
        if _lookup_empty(enriched):
            job.log(f"{domain}: crawl4ai had no page, keeping the Apollo record")
        reason = _leave_out(job, domain, str(payload.get("domain") or domain), evidence, exact, company)
        if reason:
            async with job.lock:
                job.skipped += 1
            job.log(f"{domain}: left out, {reason}")
            return
        saas_note = await _saas_check(job, client, company, domain, saved)
        if saas_note.startswith("leave:"):
            async with job.lock:
                job.skipped += 1
            job.log(f"{domain}: left out, {saas_note.removeprefix('leave: ')}")
            return
        flags = [saas_note] if saas_note else []
        if company.get("phone"):
            place = phone_location_flag(company["phone"], state=job.filters.state, country=_phone_country(job))
            if place.startswith("phone country"):
                async with job.lock:
                    job.skipped += 1
                job.log(f"{domain}: left out, {place}")
                return
            if place:
                flags.append(place)
        if not company.get("phone") or not company.get("revenue"):
            if saved.get("dropleads_called") and isinstance(saved.get("dropleads"), dict):
                flags.extend(_apply_saved_money(company, saved["dropleads"]))
                job.log(f"{domain}: reused the saved revenue lookup")
            else:
                flags.extend(await _fill_money(job, client, company, domain))
                if client.__class__.__name__ == "TregClient" and getattr(client, "fill_company", None) is not None:
                    remember(
                        domain,
                        dropleads_called=True,
                        dropleads={"phone": company.get("phone") or "", "revenue": company.get("revenue") or ""},
                    )
        missed = not company.get("name") or company.get("name") == domain
        if missed:
            company["status"] = "not found"
        if company.get("status") == "not found" or job.options.contacts_per_company == 0:
            contacts = []
        else:
            contacts = await _contacts(job, client, company)
        company["contacts_found"] = len(contacts)
        company["emails_found"] = sum(1 for row in contacts if row["email_status"] in {"found", "verified"})
        job.emails += company["emails_found"]
        if not company["status"]:
            company["status"] = "not found" if missed else "enriched"
        notes = [note for note in flags if note]
        if notes and company["status"] == "enriched":
            company["status"] = "enriched; flagged: " + "; ".join(notes)
        job.contacts.extend(contacts)
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}, {company['contacts_found']} contacts, {company['emails_found']} emails")
    except TregError as exc:
        company["status"] = f"error: {exc.message}"
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}")
        if exc.status in {401, 402}:
            job.stop = True
            job.stop_reason = exc.message
    except httpx.TransportError as exc:
        company["status"] = f"error: {failure_text(exc)}"
        job.companies.append(company)
        job.done += 1
        job.log(f"{domain}: {company['status']}")


async def _contacts(job: Job, client, company: dict) -> list[dict]:
    if not await job.allow():
        return []
    try:
        found = await client.search_people(
            company["domain"],
            limit=job.options.contacts_per_company,
            title=job.filters.title,
        )
    except TregError as exc:
        company["sources"].append(f"people.search error: {exc.message}")
        if exc.status in {401, 402}:
            raise
        return []
    except httpx.TransportError as exc:
        company["sources"].append(f"people.search error: {failure_text(exc)}")
        return []
    await _remember(job, found)
    company["cost_micro"] += found.cost_micro
    company["sources"].append(found.source)
    people = people_from_payload(found.output, found.raw)[: job.options.contacts_per_company]
    rows = []
    for person in people:
        if job.stop:
            break
        row = {
            "index": company["index"],
            "company": company["name"],
            "domain": company["domain"],
            "first_name": person["first_name"],
            "last_name": person["last_name"],
            "title": person["title"],
            "linkedin": person["linkedin"],
            "email": person.get("email") or "",
            "email_status": person.get("email_status") or "not found",
            "phone": person.get("phone") or "",
            "sources": [found.source],
            "cost_micro": 0,
            "fields": dict(person.get("fields") or {}),
        }
        if (person["first_name"] or person["last_name"] or person["linkedin"]) and await job.allow():
            try:
                email = await client.find_email(
                    domain=company["domain"],
                    first=person["first_name"],
                    last=person["last_name"],
                    linkedin=person["linkedin"],
                    verify=job.options.verify_emails,
                )
                await _remember(job, email)
                row["cost_micro"] += email.cost_micro
                row["sources"].append(email.source)
                verifier = (email.treg.get("verification") or {}).get("served_by")
                if verifier:
                    row["sources"].append(f"email.verify:{verifier}")
                row.setdefault("fields", {})
                for key, value in flatten_record(email.output).items():
                    row["fields"].setdefault(key, value)
                for key, value in flatten_record(email.raw).items():
                    row["fields"].setdefault(key, value)
                found_email, status = email_status(email.output, email.treg)
                if found_email:
                    row["email"] = found_email
                    row["email_status"] = status
                elif not row["email"]:
                    row["email_status"] = "not found"
            except TregError as exc:
                row["sources"].append(f"email.find error: {exc.message}")
                if not row["email"]:
                    row["email_status"] = "not found"
                if exc.status in {401, 402}:
                    job.stop = True
                    job.stop_reason = exc.message
            except httpx.TransportError as exc:
                row["sources"].append(f"email.find error: {failure_text(exc)}")
                if not row["email"]:
                    row["email_status"] = "not found"
        if job.options.lookup_phones and not row["phone"] and await job.allow() and not job.stop:
            try:
                phone = await client.find_phone(
                    domain=company["domain"],
                    first=person["first_name"],
                    last=person["last_name"],
                    linkedin=person["linkedin"],
                    email=row["email"],
                )
                await _remember(job, phone)
                row["cost_micro"] += phone.cost_micro
                row["sources"].append(phone.source)
                number = clean_text((phone.output or {}).get("phone"))
                if number:
                    row["phone"] = number
            except TregError as exc:
                row["sources"].append(f"phone.find error: {exc.message}")
                if exc.status in {401, 402}:
                    job.stop = True
                    job.stop_reason = exc.message
        rows.append(row)
    return rows
