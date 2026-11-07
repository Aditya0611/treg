"""Score each agent's saved answers against the hand-checked ground truth.

Every field gets one of four results:
  pass     the agent's value matches the known answer
  fail     the agent gave a value and it is wrong
  missing  the agent gave nothing for a field we know
  n/a      we have no known answer for this field, or the agent never offers it
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from app.agents import AGENTS_BY_ID, NOT_OFFERED, extract, linkedin_slug
from app.normalize import normalize_domain
from app.places import country_code, state_name

SCORED_FIELDS = ("identity", "linkedin", "employees", "location", "industry")

INDUSTRY_WORDS: dict[str, tuple[str, ...]] = {
    "information technology": ("information technology", "it service", "computer", "software", "internet", "technology", "network"),
    "software": ("software", "saas", "internet", "information technology", "computer"),
    "saas": ("software", "saas", "internet"),
    "telecom": ("telecom", "internet service", "wireless", "isp", "communications"),
    "government": ("government", "public administration", "public policy"),
    "library": ("librar",),
    "education": ("education", "school", "university", "college", "e-learning"),
    "nonprofit": ("non-profit", "nonprofit", "civic", "philanthrop", "social services"),
    "media": ("media", "publishing", "news", "broadcast", "entertainment", "music"),
    "metal fabrication": ("metal", "fabricat", "machin", "weld", "steel", "manufactur"),
}


def load_truth(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.DictReader(handle) if (row.get("domain") or "").strip()]
    for row in rows:
        row["domain"] = normalize_domain(row["domain"]) or row["domain"].strip().lower()
    return rows


def is_verified(row: dict) -> bool:
    return (row.get("verified") or "").strip().lower() in {"yes", "y", "true", "1"}


def load_answers(raw_dir: Path) -> dict[str, dict[str, dict]]:
    """{agent_id: {domain: saved answer}}. The last saved line for a domain wins."""
    answers: dict[str, dict[str, dict]] = {}
    if not raw_dir.exists():
        return answers
    for path in sorted(raw_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            agent = item.get("agent") or path.stem
            domain = normalize_domain(item.get("domain") or "")
            if domain:
                answers.setdefault(agent, {})[domain] = item
    return answers


def _int(value) -> int | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _result(field: str, status: str, expected="", got="", why: str = "") -> dict:
    return {"field": field, "status": status, "expected": expected, "got": got, "why": why}


def _identity(truth: dict, record: dict, offered: bool) -> dict:
    seed = truth["domain"]
    canonical = normalize_domain(truth.get("canonical_domain") or "") or ""
    expected = " or ".join(dict.fromkeys(item for item in (seed, canonical) if item))
    if not offered:
        return _result("identity", "n/a", expected, "", "agent does not return a domain")
    got = record.get("domain") or ""
    if not got:
        return _result("identity", "missing", expected, "", "no domain returned")
    if got in {seed, canonical}:
        return _result("identity", "pass", expected, got)
    return _result("identity", "fail", expected, got, f"returned {got}, a different company")


def _linkedin(truth: dict, record: dict, offered: bool) -> dict:
    want = linkedin_slug(truth.get("linkedin") or "")
    if not want:
        return _result("linkedin", "n/a", "", record.get("linkedin") or "", "no known LinkedIn")
    if not offered:
        return _result("linkedin", "n/a", want, "", "agent does not return LinkedIn")
    got = linkedin_slug(record.get("linkedin") or "")
    if not got:
        return _result("linkedin", "missing", want, "", "no LinkedIn returned")
    if got == want:
        return _result("linkedin", "pass", want, got)
    if got.isdigit() != want.isdigit():
        return _result("linkedin", "n/a", want, got, "numeric LinkedIn id cannot be compared with a page name")
    return _result("linkedin", "fail", want, got, "different LinkedIn page")


def _employees(truth: dict, record: dict, offered: bool) -> dict:
    low, high = _int(truth.get("employees_min")), _int(truth.get("employees_max"))
    expected = f"{low if low is not None else '?'}–{high if high is not None else '∞'}"
    if low is None and high is None:
        return _result("employees", "n/a", "", record.get("employees") or "", "no known headcount")
    if not offered:
        return _result("employees", "n/a", expected, "", "agent does not return headcount")
    got_low, got_high = record.get("employees_low"), record.get("employees_high")
    shown = record.get("employees") or ""
    if got_low is None:
        return _result("employees", "missing", expected, shown, "no headcount returned")
    if got_high is not None and got_high != got_low:
        point, kind = (got_low + got_high) / 2, "band midpoint"
    else:
        point, kind = got_low, "count"
    inside = (low is None or point >= low) and (high is None or point <= high)
    if inside:
        return _result("employees", "pass", expected, shown, kind)
    return _result("employees", "fail", expected, shown, f"{kind} {point:g} outside {expected}")


def _location(truth: dict, record: dict, offered: bool) -> dict:
    want_country = country_code(truth.get("country") or "") or (truth.get("country") or "").strip()
    want_state = state_name(truth.get("state") or "") or (truth.get("state") or "").strip()
    expected = ", ".join(part for part in (want_state, want_country) if part)
    if not (want_country or want_state):
        return _result("location", "n/a", "", "", "no known location")
    if not offered:
        return _result("location", "n/a", expected, "", "agent does not return location")
    got_country = country_code(record.get("country") or "") or (record.get("country") or "").strip()
    got_state = state_name(record.get("state") or "") or (record.get("state") or "").strip()
    shown = ", ".join(part for part in (record.get("city"), got_state, got_country) if part)
    if not (got_country or got_state):
        return _result("location", "missing", expected, shown, "no state or country returned")
    problems = []
    if want_country and got_country and want_country.lower() != got_country.lower():
        problems.append(f"country {got_country}")
    if want_state:
        if not got_state:
            problems.append("no state")
        elif want_state.lower() != got_state.lower():
            problems.append(f"state {got_state}")
    if want_country and not got_country and not want_state:
        problems.append("no country")
    if problems:
        return _result("location", "fail", expected, shown, "; ".join(problems))
    return _result("location", "pass", expected, shown)


def _industry(truth: dict, record: dict, offered: bool) -> dict:
    want = (truth.get("industry") or "").strip().lower()
    if not want:
        return _result("industry", "n/a", "", record.get("industry") or "", "no known industry")
    if not offered:
        return _result("industry", "n/a", want, "", "agent does not return industry")
    got = (record.get("industry") or "").strip()
    if not got:
        return _result("industry", "missing", want, "", "no industry returned")
    words = INDUSTRY_WORDS.get(want, (want,))
    if any(word in got.lower() for word in words):
        return _result("industry", "pass", want, got)
    return _result("industry", "fail", want, got, "label does not match")


_FIELD_TO_RECORD = {
    "identity": ("domain",),
    "linkedin": ("linkedin",),
    "employees": ("employees",),
    "location": ("city", "state", "country"),
    "industry": ("industry",),
}


def score_company(agent_id: str, truth: dict, answer: dict | None) -> dict:
    """One agent on one company: the extracted record and a result per field."""
    answer = answer or {}
    status = answer.get("status") or ("ok" if answer else "not run")
    record = extract(agent_id, answer) if status == "ok" else extract(agent_id, {})
    hidden = NOT_OFFERED.get(agent_id, frozenset()) | frozenset(answer.get("not_saved") or ())
    offered = {field: not all(part in hidden for part in parts) for field, parts in _FIELD_TO_RECORD.items()}
    if status == "not run":
        results = [_result(field, "not run") for field in SCORED_FIELDS]
    else:
        results = [
            _identity(truth, record, offered["identity"]),
            _linkedin(truth, record, offered["linkedin"]),
            _employees(truth, record, offered["employees"]),
            _location(truth, record, offered["location"]),
            _industry(truth, record, offered["industry"]),
        ]
    return {
        "agent": agent_id,
        "domain": truth["domain"],
        "case": truth.get("case") or "",
        "verified": is_verified(truth),
        "call_status": status,
        "error": answer.get("error") or "",
        "cost_micro": int(answer.get("cost_micro") or 0),
        "from_cache": bool(answer.get("from_cache")),
        "answered": bool(record.get("answered")) and status == "ok",
        "record": record,
        "results": results,
    }


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def summarize_agent(agent_id: str, rows: list[dict]) -> dict:
    """Coverage, per-field accuracy, and cost for one agent."""
    ran = [row for row in rows if row["call_status"] != "not run"]
    answered = [row for row in ran if row["answered"]]
    fields = {}
    for field in SCORED_FIELDS:
        counts = {"pass": 0, "fail": 0, "missing": 0, "n/a": 0}
        for row in ran:
            for result in row["results"]:
                if result["field"] == field:
                    counts[result["status"]] = counts.get(result["status"], 0) + 1
        known = counts["pass"] + counts["fail"] + counts["missing"]
        given = counts["pass"] + counts["fail"]
        fields[field] = {
            **counts,
            "known": known,
            "correct": _rate(counts["pass"], known),
            "accuracy_when_given": _rate(counts["pass"], given),
            "coverage": _rate(given, known),
        }
    scored = [fields[field]["correct"] for field in SCORED_FIELDS if fields[field]["correct"] is not None]
    spent = sum(row["cost_micro"] for row in ran) / 1_000_000
    agent = AGENTS_BY_ID.get(agent_id)
    return {
        "agent": agent_id,
        "price_usd": agent.price_usd if agent else None,
        "note": agent.note if agent else "",
        "companies": len(rows),
        "ran": len(ran),
        "errors": sum(1 for row in ran if row["call_status"] == "error"),
        "answered": len(answered),
        "coverage": _rate(len(answered), len(ran)),
        "fields": fields,
        "overall": round(sum(scored) / len(scored), 4) if scored else None,
        "spent_usd": round(spent, 6),
        "cost_per_answer": round(spent / len(answered), 6) if answered else None,
        "from_cache": any(row["from_cache"] for row in ran),
    }


def best_per_field(summaries: list[dict], *, min_known: int = 5) -> list[dict]:
    """The most correct agent per field; ties go to the cheaper agent."""
    picks = []
    for field in SCORED_FIELDS:
        ranked = []
        for summary in summaries:
            stats = summary["fields"][field]
            if stats["correct"] is None or stats["known"] < min_known:
                continue
            price = summary["price_usd"] if summary["price_usd"] is not None else 1.0
            ranked.append((stats["correct"], -price, summary["agent"], stats))
        ranked.sort(reverse=True)
        if not ranked:
            picks.append({"field": field, "best": "", "correct": None, "runner_up": "", "runner_up_correct": None, "known": 0})
            continue
        best = ranked[0]
        second = ranked[1] if len(ranked) > 1 else None
        picks.append(
            {
                "field": field,
                "best": best[2],
                "correct": best[0],
                "known": best[3]["known"],
                "runner_up": second[2] if second else "",
                "runner_up_correct": second[0] if second else None,
            }
        )
    return picks


def score_all(truth: list[dict], answers: dict[str, dict[str, dict]], agent_ids: list[str] | None = None) -> dict:
    ids = agent_ids or [agent for agent in AGENTS_BY_ID if agent in answers] or list(answers)
    details: list[dict] = []
    summaries = []
    for agent_id in ids:
        saved = answers.get(agent_id, {})
        rows = [score_company(agent_id, row, saved.get(row["domain"])) for row in truth]
        details.extend(rows)
        summaries.append(summarize_agent(agent_id, rows))
    return {"summaries": summaries, "details": details, "best": best_per_field(summaries)}
