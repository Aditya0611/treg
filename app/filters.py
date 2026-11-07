"""Turn a sentence such as 'SaaS companies in Texas, 50-200 employees' into filters."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

import httpx

from app.places import countries_label, countries_value, country_code, country_codes, state_name

_RANGE = re.compile(
    r"(?:between\s+)?(\d[\d,]*)\s*(?:-|–|—|\bto\b|\band\b)\s*(\d[\d,]*)\s*(?:employees|employee|people|staff|headcount)?",
    re.I,
)
_UNDER = re.compile(
    r"(?:under|less than|fewer than|<)\s*(\d[\d,]*)\s*(?:employees|employee|people|staff|headcount)?",
    re.I,
)
_OVER = re.compile(
    r"(?:over|more than|at least|>)\s*(\d[\d,]*)\s*(?:employees|employee|people|staff|headcount)?",
    re.I,
)
_EXACT = re.compile(
    r"(?:having|with|of)?\s*(?:around|about|approximately|approx\.?)?\s*(\d[\d,]*)\s+(?:employees|employee|people|staff|headcount)\b",
    re.I,
)
_LIMIT_TOP = re.compile(r"\btop\s+(\d{1,4})\b", re.I)
_LIMIT_COUNT = re.compile(r"\b(\d{1,4})\s+(?:companies|startups|firms|results|accounts)\b", re.I)
_COMPANY_WORDS = re.compile(
    r"\b(companies|company|startups|startup|firms|firm|businesses|business|organizations|organisations)\b",
    re.I,
)
_BASED = re.compile(r"\b(?:based|headquartered|located|hq(?:'d)?)\s+in\b", re.I)
_PLACE_WORD = re.compile(r"\b(?:in|of|from|across)\b", re.I)
_LEADING_THE = re.compile(r"^the\s+", re.I)
_INDUSTRY_ALIASES = {
    "it": "information technology",
    "tech": "information technology",
    "technology": "information technology",
}
# Tomba company-search size labels. A single number such as 20 falls in one band.
# Labels the company search actually stores: 11-50, 51-250, 251-1K, 1K-5K.
SIZE_BANDS = (
    (1, 10, "1-10"),
    (11, 50, "11-50"),
    (51, 250, "51-250"),
    (251, 1000, "251-1K"),
    (1001, 5000, "1K-5K"),
    (5001, 10000, "5K-10K"),
    (10001, 50000, "10K-50K"),
    (50001, 10**12, "50K+"),
)


@dataclass
class Filters:
    q: str = ""
    industry: str = ""
    technology: str = ""
    country: str = ""
    state: str = ""
    city: str = ""
    employees_min: int | None = None
    employees_max: int | None = None
    limit: int = 100
    title: str = ""
    parser: str = "rules"
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _hunter_kind(filters) -> str:
    """A clear for-profit industry phrase Hunter can turn into structured filters."""
    industry = (filters.industry or "").strip().lower()
    raw = " ".join((filters.industry or filters.q or "companies").split())
    if industry in {"saas", "software", "software as a service"} or "saas" in industry:
        kind = "for-profit SaaS software companies"
    elif industry in {"information technology", "it", "tech", "technology"}:
        kind = "for-profit information technology companies"
    else:
        kind = raw
        if kind and not kind.lower().startswith("for-profit"):
            kind = f"for-profit {kind}"
        if not _COMPANY_WORDS.search(kind):
            kind = f"{kind} companies"
    return " ".join(kind.split())


def _hunter_place(filters) -> str:
    city = (filters.city or "").strip()
    state = (filters.state or "").strip()
    country = (filters.country or "").strip().upper()
    if city and state:
        place = f"{city}, {state}"
    elif state:
        place = state
    elif country in {"US", "USA", "UNITED STATES"}:
        place = "the United States"
    elif "," in country or country == "EU":
        place = countries_label(country)
    elif country:
        place = filters.country.strip()
    else:
        place = ""
    if place and country in {"US", "USA", "UNITED STATES"} and "United States" not in place and not state:
        place = "the United States"
    return f" headquartered in {place}" if place else ""


def _hunter_size(filters) -> str:
    low, high = filters.employees_min, filters.employees_max
    if low is not None and high is not None:
        for start, end, label in SIZE_BANDS:
            if low == start and high == end:
                return f" with {label} employees"
        return f" with {low} to {high} employees"
    if low is not None:
        return f" with at least {low} employees"
    if high is not None:
        return f" with at most {high} employees"
    return ""


def hunter_sentence(filters) -> str:
    """The sentence Hunter turns into industry, place, and headcount filters."""
    return f"{_hunter_kind(filters)}{_hunter_place(filters)}{_hunter_size(filters)}"[:300]


def size_sentence(filters) -> str:
    """Sentence Tomba can read when its structured size filter is ignored."""
    labels = size_labels(filters.employees_min, filters.employees_max)
    base = " ".join((getattr(filters, "q", "") or getattr(filters, "industry", "") or "companies").split())
    if labels:
        base = f"{base} with {labels[0]} employees"
    return base[:100]


def search_industries(industry: str) -> list[str]:
    """Tomba has no industry named SaaS. Use the labels its search actually matches."""
    key = (industry or "").strip().lower()
    if key in {"saas", "software", "software as a service"}:
        return ["computer software", "Information Technology and Services"]
    if key == "information technology":
        return ["information technology", "technology", "Information Technology and Services", "computer software"]
    return [industry] if industry else []


def band_for_headcount(count: int) -> str:
    """The size band that contains a real headcount. 182 is 51-250, not the search label."""
    for band_start, band_end, label in SIZE_BANDS:
        if band_start <= count <= band_end:
            return label
    return ""


def size_labels(low: int | None, high: int | None) -> list[str]:
    if low is None and high is None:
        return []
    start = 1 if low is None else low
    end = 10**12 if high is None else high
    return [label for band_start, band_end, label in SIZE_BANDS if band_end >= start and band_start <= end]


def align_range(low: int | None, high: int | None) -> tuple[int | None, int | None, str]:
    """50 is the last headcount in the 11–50 band, so a 50–200 search keeps 51–200."""
    if low is None or high is None or high <= low:
        return low, high, ""
    for _start, end, label in SIZE_BANDS:
        if low == end and high > end:
            return low + 1, high, f"{low} is the top of the {label} band, so this search keeps {low + 1}–{high}."
    return low, high, ""


def _expand_exact_headcount(low: int | None, high: int | None) -> tuple[int | None, int | None, str]:
    """Company databases store a size band, not an exact headcount. 20 means 11–50."""
    if low is None or high is None or low != high:
        return low, high, ""
    for band_start, band_end, label in SIZE_BANDS:
        if band_start <= low <= band_end:
            return (
                band_start,
                band_end,
                f"{low} employees is searched as the {label} company-size band. Edit the min and max if you want a tighter range.",
            )
    return low, high, ""


def _num(value: str) -> int:
    return int(value.replace(",", ""))


def _strip_employees(text: str) -> tuple[str, int | None, int | None]:
    low = None
    high = None
    match = _RANGE.search(text)
    if match:
        low, high = _num(match.group(1)), _num(match.group(2))
        if low > high:
            low, high = high, low
        text = (text[: match.start()] + " " + text[match.end() :]).strip()
    else:
        under = _UNDER.search(text)
        over = _OVER.search(text)
        if under:
            high = _num(under.group(1))
            text = (text[: under.start()] + " " + text[under.end() :]).strip()
        if over:
            low = _num(over.group(1))
            text = (text[: over.start()] + " " + text[over.end() :]).strip()
        exact = _EXACT.search(text)
        if exact and low is None and high is None:
            low = high = _num(exact.group(1))
            text = (text[: exact.start()] + " " + text[exact.end() :]).strip()
    if low is not None or high is not None:
        text = re.sub(r"\b(?:with|having|that have)\s*(?=$|,|\bin\b|\bbased\b)", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ,;"), low, high


def _parse_place(place: str) -> tuple[str, str, str]:
    several = countries_value(place)
    if len(country_codes(place)) > 1 and all(part.isalpha() and part.isupper() for part in several.split(",")):
        return "", "", several
    parts = [part.strip(" .") for part in place.split(",") if part.strip(" .")]
    city = state = country = ""
    unknown: list[str] = []
    for part in parts:
        named = state_name(part)
        coded = country_code(part)
        # A full country name wins over a state abbreviation that shares letters ("India", "Germany").
        if coded and (len(part.strip()) > 2 or not named):
            country = country or coded
        elif named:
            state = state or named
            country = country or "US"
        elif coded:
            country = country or coded
        else:
            unknown.append(part)
    if unknown:
        city = ", ".join(unknown)
    if len(parts) == 1 and not state and not country and not city:
        city = parts[0]
    return city, state, country


def _split_place(text: str) -> tuple[str, str, str, str, str]:
    """Split 'IT companies of the USA' into the industry words and a place."""
    for match in reversed(list(_PLACE_WORD.finditer(text))):
        head = text[: match.start()].strip(" ,;")
        place = _LEADING_THE.sub("", text[match.end() :].strip(" ,;"))
        if not head or not place:
            continue
        city, state, country = _parse_place(place)
        if state or country:
            return head, place, city, state, country
    return text, "", "", "", ""


def parse_query_rules(query: str, limit: int | None = None) -> Filters:
    original = " ".join((query or "").strip().split())
    filters = Filters(parser="rules")
    if not original:
        filters.note = "Enter a query first."
        return filters

    if limit is not None:
        filters.limit = max(1, min(1000, int(limit)))

    text, low, high = _strip_employees(original)
    limit_match = _LIMIT_TOP.search(text) or _LIMIT_COUNT.search(text)
    if limit is None and limit_match:
        filters.limit = max(1, min(1000, int(limit_match.group(1))))
        text = re.sub(r"\s+", " ", (text[: limit_match.start()] + " " + text[limit_match.end() :])).strip(" ,;")
    low, high, band_note = _expand_exact_headcount(low, high)
    low, high, edge_note = align_range(low, high)
    filters.employees_min = low
    filters.employees_max = high
    filters.note = edge_note or band_note
    text = _BASED.sub(" in ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,;")

    head, _place, filters.city, filters.state, filters.country = _split_place(text)

    industry = _COMPANY_WORDS.sub(" ", head)
    industry = re.sub(r"\s+", " ", industry).strip(" ,.;-")
    if len(industry) > (400 if "," in industry else 80):
        industry = ""
    filters.industry = _INDUSTRY_ALIASES.get(industry.lower(), industry)
    # Keep the sentence, not just the industry word. "IT" alone matches nothing.
    filters.q = text or original
    if not filters.industry and not filters.city and not filters.state and not filters.country:
        filters.q = original
        filters.note = "No place or industry was detected. Edit the filters or search by the free-text query."
    return filters


def filters_from_dict(data: dict | None) -> Filters:
    raw = data or {}
    def optional_int(key: str) -> int | None:
        value = raw.get(key)
        if value is None or value == "":
            return None
        return int(value)

    limit = optional_int("limit") or 100
    filters = Filters(
        q=str(raw.get("q") or "").strip(),
        industry=str(raw.get("industry") or "").strip(),
        technology=str(raw.get("technology") or "").strip(),
        country=countries_value(str(raw.get("country") or "")).upper(),
        state=str(raw.get("state") or "").strip(),
        city=str(raw.get("city") or "").strip(),
        employees_min=optional_int("employees_min"),
        employees_max=optional_int("employees_max"),
        limit=max(1, min(1000, limit)),
        title=str(raw.get("title") or "").strip(),
        parser=str(raw.get("parser") or "edited"),
        note=str(raw.get("note") or "").strip(),
    )
    low, high, edge_note = align_range(filters.employees_min, filters.employees_max)
    filters.employees_min = low
    filters.employees_max = high
    if edge_note:
        filters.note = edge_note
    return filters


def _llm_payload(query: str) -> dict:
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract company-search filters from the user sentence. "
                    "Reply with JSON only, keys: q, industry, technology, country, state, city, "
                    "employees_min, employees_max, limit, title. "
                    "country is ISO 3166-1 alpha-2, or a comma-separated list such as US,CA,EU when several "
                    "countries are asked (EU means Europe), or empty. state is a full US state name or empty. "
                    "industry keeps every keyword the user listed, comma-separated. "
                    "employees_min and employees_max are integers or null. limit is an integer, default 100, max 1000. "
                    "industry is the sector words (SaaS, fintech, IT), not the word companies. "
                    "q is a short free-text search. title is a job title only if the user asked for one. "
                    "For a single headcount such as 20, set employees_min and employees_max to that number. "
                    "For a range, copy both numbers exactly. 51-200 means employees_min 51 and employees_max 200. "
                    "Do not round either end to a size band. "
                    "Use null for unknown numbers and empty string for unknown text."
                ),
            },
            {"role": "user", "content": query},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "openai/gpt-oss-120b"


def llm_target(api_key: str, base_url: str = "", model: str = "") -> tuple[str, str]:
    """Groq keys use Groq's OpenAI-compatible endpoint unless a base URL is set."""
    key = (api_key or "").strip()
    if (base_url or "").strip():
        url = base_url.strip().rstrip("/")
        chosen = (model or "").strip() or ("gpt-4o-mini" if "groq.com" not in url else GROQ_MODEL)
        return url, chosen
    if key.startswith("gsk_"):
        return GROQ_BASE_URL, (model or "").strip() or GROQ_MODEL
    return "https://api.openai.com/v1", (model or "").strip() or "gpt-4o-mini"


async def parse_query(
    query: str,
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    limit: int | None = None,
) -> Filters:
    rules = parse_query_rules(query, limit=limit)
    key = (api_key or "").strip()
    if not key:
        return rules
    url, chosen = llm_target(key, base_url, model)
    payload = _llm_payload(query)
    payload["model"] = chosen
    url = url + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        rules.note = f"Language model was unavailable ({exc.__class__.__name__}). These filters came from the built-in parser. Edit them before continuing."
        return rules

    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        rules.note = "The language model reply was not JSON. These filters came from the built-in parser."
        return rules

    parsed = filters_from_dict(data)
    if limit is not None:
        parsed.limit = max(1, min(1000, int(limit)))
    # Keep a rules result when the model returns an empty interpretation of a non-empty query.
    if not any((parsed.q, parsed.industry, parsed.country, parsed.state, parsed.city)):
        rules.note = "The language model returned empty filters. These came from the built-in parser."
        return rules
    return finalize_filters(parsed, rules)


def finalize_filters(parsed: Filters, rules: Filters) -> Filters:
    """Normalize a language-model draft and fill gaps from the built-in parser."""
    parsed.parser = "llm"
    parsed.industry = _INDUSTRY_ALIASES.get(parsed.industry.lower(), parsed.industry)
    parsed.country = countries_value(parsed.country)
    if parsed.state:
        parsed.state = state_name(parsed.state) or parsed.state
    for name in ("q", "industry", "technology", "country", "state", "city", "title"):
        if not getattr(parsed, name) and getattr(rules, name):
            setattr(parsed, name, getattr(rules, name))
    if rules.employees_min is not None or rules.employees_max is not None:
        parsed.employees_min = rules.employees_min
        parsed.employees_max = rules.employees_max
        if rules.note:
            parsed.note = rules.note
    elif parsed.employees_min is None and parsed.employees_max is None:
        parsed.employees_min = rules.employees_min
        parsed.employees_max = rules.employees_max
        parsed.note = rules.note
    else:
        if parsed.employees_min and parsed.employees_max and parsed.employees_min > parsed.employees_max:
            parsed.employees_min, parsed.employees_max = parsed.employees_max, parsed.employees_min
        low, high, note = _expand_exact_headcount(parsed.employees_min, parsed.employees_max)
        parsed.employees_min = low
        parsed.employees_max = high
        if note:
            parsed.note = note
    if not parsed.note:
        parsed.note = "Nothing is searched until you click Run."
    return parsed
