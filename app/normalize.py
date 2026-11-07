"""Map Treg's provider-shaped payloads onto the sheet columns."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.places import (
    COUNTRIES,
    COUNTRY_NAMES,
    US_STATES,
    countries_label,
    country_code,
    country_codes,
    country_label,
    state_name,
)

_YEAR = re.compile(r"\b(18|19|20)\d{2}\b")
_EMP_RANGE = re.compile(r"(\d+)\s*([kK])?\s*(?:-|–|—|to)\s*(\d+)\s*([kK])?", re.I)
_EMP_NUM = re.compile(r"(\d[\d,]*)")
_ILLEGAL = re.compile(r"[\000-\010\013\014\016-\037]")


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = _ILLEGAL.sub("", str(value)).strip()
    if re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text[:32767]


def normalize_domain(value: str) -> str | None:
    raw = clean_text(value).lower()
    if not raw or raw in {"n/a", "na", "none", "-", "null"}:
        return None
    if "@" in raw and "://" not in raw and "/" not in raw:
        raw = raw.split("@", 1)[1]
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").removeprefix("www.").strip(".")
    if "." not in host or " " in host or len(host) > 253:
        return None
    return host


def clean_linkedin(value) -> str:
    text = clean_text(value)
    if "linkedin.com" not in text.lower():
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    return text


def _scalar(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return clean_text(value)
    return ""


def _first(blob: dict, keys: tuple[str, ...]) -> str:
    lowered = {str(key).lower().replace("-", "_"): value for key, value in blob.items()}
    for key in keys:
        text = _scalar(lowered.get(key))
        if text:
            return text
    return ""


def _key_token(key: str) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return text.lower().replace("-", "_").replace(" ", "_")


def _walk(value, keys: set[str], found: dict, depth: int = 0) -> None:
    if depth > 5 or len(found) >= len(keys):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            token = _key_token(key)
            if token in {"people", "contacts", "employees_list", "staff", "persons"}:
                continue
            if token in keys and token not in found:
                text = _scalar(item)
                if text:
                    found[token] = text
                    continue
            if token in {"address", "hq", "headquarters", "location", "geo", "headquarter"} and isinstance(item, dict):
                _walk(item, keys, found, depth + 1)
                continue
            if isinstance(item, (dict, list)) and token not in {"raw"}:
                _walk(item, keys, found, depth + 1)
    elif isinstance(value, list) and depth < 4:
        for item in value[:10]:
            _walk(item, keys, found, depth + 1)


def _deep(source, keys: tuple[str, ...]) -> dict[str, str]:
    found: dict[str, str] = {}
    _walk(source, set(keys), found)
    return found


def _scaled_count(number: str, suffix: str | None) -> int:
    count = int(number)
    if suffix and suffix.lower() == "k":
        count *= 1000
    return count


def employee_bounds(value) -> tuple[int | None, int | None]:
    text = _scalar(value)
    if not text:
        return None, None
    match = _EMP_RANGE.search(text.replace(",", ""))
    if match:
        low = _scaled_count(match.group(1), match.group(2))
        high = _scaled_count(match.group(3), match.group(4))
        return (low, high) if low <= high else (high, low)
    number = _EMP_NUM.search(text.replace(",", ""))
    if not number:
        return None, None
    count = int(number.group(1))
    if re.search(r"\+$", text) or "more than" in text.lower() or "over" in text.lower():
        return count, None
    return count, count


def _field_leaf(key: str) -> str:
    return _key_token(str(key).rsplit(".", 1)[-1])


def _exact_count(value) -> int | None:
    start, end = employee_bounds(value)
    if start is not None and end is not None and start == end and start > 0:
        return start
    return None


def size_evidence(employees, fields: dict | None) -> tuple[int | None, str]:
    """A positive headcount, a blank count, or no information.

    A listed band such as 11-50 is not a count. People Count 0 is blank, so that band is not used.
    """
    saw_blank = False
    saw_band = False
    for value in _count_values(fields):
        exact = _exact_count(value)
        if exact:
            return exact, "count"
        if _scalar(value) in {"0", "0.0"}:
            saw_blank = True
    exact = _exact_count(employees)
    if exact:
        return exact, "count"
    if employee_bounds(employees)[0] is not None:
        saw_band = True
    if saw_blank or saw_band:
        return None, "blank"
    return None, "unknown"


def _count_values(fields: dict | None) -> list:
    values = []
    for key, value in (fields or {}).items():
        if _field_leaf(key) in {"estimated_num_employees", "people_count", "employee_count", "headcount"}:
            values.append(value)
    return values


def reported_headcount(employees, fields: dict | None) -> str:
    """People Count is the headcount. A size label is used only when that count is missing."""
    counts = []
    others = []
    if employees not in (None, ""):
        others.append(employees)
    for key, value in (fields or {}).items():
        leaf = _field_leaf(key)
        if leaf == "estimated_num_employees":
            counts.insert(0, value)
        elif leaf in {"people_count", "employee_count", "headcount"}:
            counts.append(value)
        elif leaf in {"employees", "company_size", "size"}:
            others.append(value)
    for value in counts:
        exact = _exact_count(value)
        if exact is not None:
            return str(exact)
    return pick_headcount(*others)


def pick_headcount(*values) -> str:
    """Prefer a real count such as 4454 over a size label. Zero means the provider left it blank."""
    label = ""
    best: int | None = None
    for value in values:
        text = _scalar(value)
        if not text:
            continue
        start, end = employee_bounds(text)
        if start == 0 and (end or 0) == 0:
            continue
        if start is not None and end is not None and start == end and start > 0:
            if best is None or start > best:
                best = start
            continue
        if start is not None and not label:
            label = text
    if best is not None:
        return str(best)
    return label


def outside_employee_range(value, low: int | None, high: int | None) -> bool:
    if low is None and high is None:
        return False
    start, end = employee_bounds(value)
    if start is None and end is None:
        return False
    if (start or 0) == 0 and (end or 0) == 0:
        return False
    if start is None:
        start = end
    if end is None:
        end = 10**9 if high is not None else start
    if high is not None and start > high:
        return True
    if low is not None and end < low:
        return True
    return False


def split_person_name(full: str) -> tuple[str, str]:
    parts = [part for part in clean_text(full).split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def split_location(location: str) -> tuple[str, str, str]:
    parts = [part.strip() for part in clean_text(location).split(",") if part.strip()]
    city = state = country = ""
    leftover: list[str] = []
    for part in parts:
        named = state_name(part)
        coded = country_code(part)
        if coded and (len(part) > 2 or not named):
            country = country or coded
        elif named:
            state = state or named
            country = country or "US"
        elif coded:
            country = country or coded
        else:
            leftover.append(part)
    if leftover:
        city = leftover[0]
    return city, state, country


def _year(value: str) -> str:
    match = _YEAR.search(value or "")
    return match.group(0) if match else ""


_NON_SOFTWARE = (
    "landscaping",
    "dental",
    "marketing service",
    "media & telecommunication",
    "media and telecommunication",
    "apparel",
    "footwear",
    "restaurant",
    "hospitality",
    "staffing",
    "recruiting",
    "public safety",
)
_NON_SOFTWARE_TEXT = (
    "vacation home",
    "vacation rental",
    "it consulting",
    "consulting services",
    "consulting firm",
    "consultancy",
    "job board",
)
_CONSUMER = ("shoe", "dental", "lawn")
_POSTAL_CITIES = {"Texas": {"733": "Austin", "787": "Austin"}}
_COORD = re.compile(r"^-?\d{1,3}\.\d+$")
_STREET_WORDS = {
    "st": "St", "street": "Street", "ave": "Ave", "avenue": "Avenue",
    "blvd": "Blvd", "boulevard": "Boulevard", "dr": "Dr", "drive": "Drive",
    "ln": "Ln", "lane": "Lane", "rd": "Rd", "road": "Road",
    "pkwy": "Pkwy", "parkway": "Parkway", "fwy": "Fwy", "freeway": "Freeway",
    "hwy": "Hwy", "highway": "Highway", "ste": "Ste", "suite": "Suite",
    "ct": "Ct", "court": "Court", "pl": "Pl", "place": "Place",
    "trl": "Trl", "trail": "Trail", "cir": "Cir", "circle": "Circle",
    "expy": "Expy", "expressway": "Expressway",
}
_STREET_ACRONYMS = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "po", "lbj", "ih", "fm", "rr", "us"}
_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
_ZIP_RANGES = {
    "Texas": ((750, 799),),
    "Louisiana": ((700, 714),),
    "Oregon": ((970, 979),),
    "Georgia": ((300, 319),),
    "California": ((900, 961),),
}
def _folded(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _domain_stem(domain: str) -> str:
    host = normalize_domain(domain) or ""
    return host.split(".")[0]


def _brand_prefix(text: str, stem: str) -> str:
    words = [word.strip(" .") for word in clean_text(text).split() if word.strip(" .")]
    acc: list[str] = []
    for word in words:
        acc.append(word)
        folded = _folded("".join(acc))
        if folded == stem:
            return " ".join(acc)
        if len(folded) > len(stem):
            break
    return ""


def clean_phone(value) -> str:
    """One shape for a phone, without the leading apostrophe some providers add."""
    text = clean_text(value).lstrip("'`").strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+") and digits and not digits.startswith(("1", "91")):
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+1 {digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"+1 {digits[0:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return f"+91 {digits[2:7]} {digits[7:]}"
    if text.startswith("+") and digits:
        return f"+{digits}"
    return text


def clean_revenue(value) -> str:
    """A bare dollar total is still an estimate. Show millions rather than 121000000."""
    text = clean_text(value).strip()
    digits = text.replace(",", "").replace("$", "").split(".", 1)[0]
    if not re.fullmatch(r"\d+", digits):
        return text
    amount = int(digits)
    if amount >= 1_000_000_000:
        shown = amount / 1_000_000_000
        unit = "B"
    elif amount >= 1_000_000:
        shown = amount / 1_000_000
        unit = "M"
    else:
        return text
    rounded = f"{shown:.1f}".rstrip("0").rstrip(".")
    return f"${rounded}{unit}"


def clean_company_name(name: str, domain: str = "", linkedin: str = "") -> str:
    """Use the company name, not the page headline or a repeated title."""
    text = clean_text(name).strip(" .")
    if not text:
        return ""
    parts = [part.strip(" .") for part in re.split(r"\s+(?:-|–|—|\||•)\s+", text) if part.strip(" .")]
    if len(parts) >= 2 and parts[0].lower() == parts[1].lower():
        text = parts[0]
    elif len(parts) >= 2:
        text = parts[0]
    stem = _domain_stem(domain)
    brand = _brand_prefix(text, stem) if stem else ""
    if brand:
        return brand
    if stem and stem not in _folded(text):
        match = re.search(r"linkedin\.com/company/([^/?#]+)", linkedin or "", re.I)
        slug = match.group(1).strip("/") if match else ""
        words = [piece for piece in re.split(r"[-_]+", slug) if piece] if slug else []
        if words and (len(text.split()) >= 5 or _folded(slug) == stem):
            return " ".join(piece.capitalize() for piece in words)
    return text


def tidy_description(description: str, name: str) -> str:
    text = clean_text(description).strip()
    if not text or text.strip(" .").lower() == clean_text(name).strip(" .").lower():
        return ""
    return text


def _money(*values) -> str:
    """A printed revenue such as $2.4M. Zero means the provider left it blank."""
    for value in values:
        text = clean_text(value).strip()
        if text and text not in {"0", "0.0", "0.00", "$0", "$0.0"}:
            return text
    return ""


def _nested_phone(blob: dict) -> str:
    primary = blob.get("primary_phone") or blob.get("primaryPhone")
    if isinstance(primary, dict):
        return clean_text(primary.get("number") or primary.get("sanitized_number") or "")
    return clean_text(blob.get("sanitized_phone") or blob.get("sanitizedPhone") or "")


def believable_revenue(revenue: str, headcount: int | None = None) -> str:
    """Keep the provider's figure. The sheet labels the column as an estimate."""
    return clean_revenue(revenue)


def employee_gap(primary, other) -> str:
    """Flag when crawl4ai and Dropleads disagree by more than about 20%."""
    left = _exact_count(primary)
    right = _exact_count(other)
    if not left or not right or left == right:
        return ""
    if abs(left - right) / min(left, right) > 0.20:
        return f"employee counts differ by more than 20% (crawl4ai {left}, dropleads {right})"
    return ""


def founded_gap(primary, other) -> str:
    """Keep crawl4ai's year, and say so when Dropleads names a different one."""
    left = _year(clean_text(primary))
    right = _year(clean_text(other))
    if left and right and left != right:
        return f"founded year differs (crawl4ai {left}, dropleads {right})"
    return ""


def described_text(company: dict) -> str:
    """The company description, including a copy that only exists in a side column."""
    parts = [clean_text(company.get("description"))]
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    for key, value in fields.items():
        if _field_leaf(key) in {"description", "keywords"}:
            text = clean_text(value)
            if text and text not in parts:
                parts.append(text)
    return " ".join(part for part in parts if part)


def software_mismatch(query_industry: str, industry: str, name: str, domain: str, description: str = "") -> str:
    """SaaS and software searches should not keep a landscaper, a retailer, or a dental office."""
    key = (query_industry or "").strip().lower()
    if key not in {"saas", "software", "software as a service", "information technology", "it"} and "software" not in key:
        return ""
    industry_text = (industry or "").lower()
    if "software" not in industry_text:
        for word in _NON_SOFTWARE:
            if word in industry_text:
                return f"industry {industry} is not software"
    if "software" in industry_text:
        return ""
    blob = f"{name} {domain} {description}".lower()
    for word in _CONSUMER:
        if word in blob:
            return "the company is not a software business"
    for phrase in _NON_SOFTWARE_TEXT:
        if phrase in blob:
            return "the company is not a software business"
    return ""


def naics_codes(payload) -> list[tuple[str, str]]:
    """(code, name) rows from a NAICS lookup."""
    found: list[tuple[str, str]] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            code = node.get("code") or node.get("naics") or node.get("naics_code")
            name = node.get("name") or node.get("title") or ""
            if code and (name or node.get("confidence")):
                found.append((clean_text(code), clean_text(name)))
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:40]:
                walk(item, depth + 1)

    walk(payload)
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for code, name in found:
        if code and code not in seen:
            seen.add(code)
            unique.append((code, name))
    return unique


def naics_mismatch(query_industry: str, rows: list[tuple[str, str]]) -> str:
    """A SaaS search keeps software publishers. IT consulting codes are left out."""
    key = (query_industry or "").strip().lower()
    if key not in {"saas", "software", "software as a service"} and "saas" not in key:
        return ""
    if not rows:
        return ""
    digits = [re.sub(r"\D", "", code) for code, _name in rows]
    if any(code.startswith(("5112", "5132", "5182")) for code in digits if code):
        return ""
    shown = ", ".join(f"{code} {name}".strip() for code, name in rows[:3])
    return f"NAICS {shown} is not a software publisher"


def us_employee_count(payload) -> int | None:
    """The United States headcount from a by-country employee breakdown."""
    counts: list[int] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            country = ""
            count = None
            for key, value in node.items():
                token = str(key).lower().replace("-", "_")
                if token in {"country", "country_name", "country_code"} and isinstance(value, str):
                    country = value
                elif token in {"code", "name", "location"} and isinstance(value, str) and not country:
                    country = value
                if token in {"employee_count", "employees", "count", "headcount", "value"} and isinstance(value, (int, float)) and not isinstance(value, bool):
                    count = int(value)
            if count is not None and _is_united_states(country):
                counts.append(count)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:200]:
                walk(item, depth + 1)

    walk(payload)
    return max(counts) if counts else None


def _is_united_states(value: str) -> bool:
    key = " ".join((value or "").strip().lower().replace(".", "").split())
    return key in {"us", "usa", "united states", "united states of america"}


def _zip_matches_state(postal: str, state: str) -> bool:
    ranges = _ZIP_RANGES.get(state_name(state) or state)
    if not ranges:
        return True
    digits = re.match(r"(\d{3})", postal or "")
    if not digits:
        return False
    prefix = int(digits.group(1))
    return any(low <= prefix <= high for low, high in ranges)


def _field_values(fields: dict, leaf: str) -> list[str]:
    found = []
    for key, value in fields.items():
        if _field_leaf(key) == leaf:
            text = clean_text(value)
            if text and text not in found:
                found.append(text)
    return found


def _real_city(value: str, state: str) -> str:
    text = clean_text(value)
    if not text or state_name(text) or text.lower() in {"usa", "us", "united states"}:
        return ""
    if state and text.lower() == state.lower():
        return ""
    return text


def tidy_street(value: str) -> str:
    """Capitalize a street line without turning a direction or short road name into a sentence."""
    text = clean_text(value)
    if not text or _COORD.match(text):
        return ""
    parts = []
    for token in text.split():
        match = re.match(r"^([^A-Za-z0-9]*)([A-Za-z0-9]+)(.*)$", token)
        if not match:
            parts.append(token)
            continue
        prefix, core, suffix = match.groups()
        key = core.lower()
        if key in _STREET_ACRONYMS or (len(key) == 1 and key.isalpha()):
            cased = key.upper()
        elif key in _STREET_WORDS:
            cased = _STREET_WORDS[key]
        elif key in {"of", "the", "and", "de"} and parts:
            cased = key
        elif key.startswith("mc") and len(key) > 2:
            cased = "Mc" + key[2:3].upper() + key[3:]
        else:
            cased = core[:1].upper() + core[1:].lower()
        parts.append(prefix + cased + suffix)
    return " ".join(parts)


def _zip_codes(text: str) -> list[str]:
    """Zip codes in an address. The leading street number is not a zip."""
    found = []
    skipped_street_number = False
    for match in re.finditer(r"\b(\d{5})(?:-\d{4})?\b", text):
        if not skipped_street_number and "," not in text[: match.start()]:
            skipped_street_number = True
            continue
        found.append(match.group(1))
    return found


def _address_score(text: str, city: str, state: str, postal: str) -> int:
    score = 0
    if city and city.lower() in text.lower():
        score += 3
    if postal and postal[:5] in text:
        score += 3
    for found in _zip_codes(text):
        if _zip_matches_state(found, state):
            score += 2
        else:
            score -= 4
    if not re.search(r"\d", text):
        score -= 3
    return score


def _street_line(address: str, city: str, state: str) -> str:
    """Drop a trailing city, state, or zip. Leave those words alone inside the road name."""
    text = clean_text(address)
    resolved = state_name(state) or state
    codes = [code.upper() for code, label in US_STATES.items() if label == resolved and len(code) == 2]
    trail = [piece for piece in (city, state, resolved, "United States", "USA", "US", *codes) if piece]
    for _ in range(6):
        updated = re.sub(r",?\s*\b\d{5}(?:-\d{4})?\b\s*$", "", text).strip(" ,")
        for piece in trail:
            updated = re.sub(rf",\s*\b{re.escape(piece)}\b\.?\s*$", "", updated, flags=re.I).strip(" ,")
        if updated == text:
            break
        text = updated
    return tidy_street(text)


def _choose_street(fields: dict, city: str, state: str, postal: str) -> tuple[str, str]:
    """Prefer the address that matches the city and state already on the row.

    The second value is the zip on that address, when it has one.
    """
    ranked = []
    for key, value in fields.items():
        leaf = _field_leaf(key)
        if leaf not in {"street_address", "street", "address"}:
            continue
        text = clean_text(value)
        if not text or _COORD.match(text) or not re.search(r"\d", text):
            continue
        score = _address_score(text, city, state, postal)
        consistent = bool(city and city.lower() in text.lower()) or bool(postal and postal[:5] in text)
        if consistent and leaf in {"street_address", "street"}:
            preference = 2
        elif leaf == "address":
            preference = 1
        else:
            preference = 0
        ranked.append((score, preference, text))
    if not ranked:
        return "", ""
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if ranked[0][0] < 0:
        return "", ""
    raw = ranked[0][2]
    zips = [code for code in _zip_codes(raw) if _zip_matches_state(code, state)]
    postal5 = (postal or "")[:5]
    chosen_zip = postal5 if postal5 and postal5 in zips else (zips[-1] if zips else "")
    return _street_line(raw, city, state), chosen_zip


def _city_from_postal(postal: str, state: str) -> str:
    table = _POSTAL_CITIES.get(state_name(state) or state, {})
    digits = re.match(r"(\d{3})", postal or "")
    return table.get(digits.group(1), "") if digits else ""


_PROMOTE = (
    ("industry", "industry"),
    ("state", "state"),
    ("country", "country"),
    ("phone", "phone"),
    ("linkedin_url", "linkedin"),
    ("linkedin", "linkedin"),
    ("founded_year", "founded_year"),
    ("year_founded", "founded_year"),
    ("annual_revenue", "revenue"),
    ("revenue", "revenue"),
)


def _promote_fields(company: dict) -> None:
    """Copy a fact out of Data / Companies / 1 / ... onto the standard column."""
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    for leaf, target in _PROMOTE:
        if company.get(target):
            continue
        values = _field_values(fields, leaf)
        if values:
            company[target] = values[0]
    if company.get("phone"):
        company["phone"] = clean_phone(company["phone"])
    if company.get("linkedin"):
        company["linkedin"] = clean_linkedin(company["linkedin"])


def tidy_company(company: dict, *, headcount: int | None = None) -> None:
    """Repair the scraped fields that were landing in the wrong columns."""
    _promote_fields(company)
    if company.get("phone"):
        company["phone"] = clean_phone(company["phone"])
    company["name"] = clean_company_name(company.get("name") or "", company.get("domain") or "", company.get("linkedin") or "")
    company["description"] = tidy_description(company.get("description") or "", company["name"])
    company["revenue"] = believable_revenue(company.get("revenue") or "", headcount)
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    for key in list(fields):
        leaf = _field_leaf(key)
        if leaf == "name" and " - " in clean_text(fields[key]):
            fields[key] = company["name"]
        elif leaf == "description" and tidy_description(str(fields[key] or ""), company["name"]) == "":
            fields.pop(key, None)
    state = state_name(company.get("state") or "") or company.get("state") or ""
    cities = []
    for value in [company.get("city"), *_field_values(fields, "city")]:
        real = _real_city(str(value or ""), state)
        if real and real.lower() not in {item.lower() for item in cities}:
            cities.append(real)
    city = cities[0] if cities else ""
    if len(cities) > 1:
        for key in list(fields):
            if _field_leaf(key) in {"city", "address", "street_address", "street"}:
                fields.pop(key, None)
        company["location"] = ""
    company["city"] = city
    postal = clean_text(company.get("postal_code") or "")
    if not postal:
        for value in _field_values(fields, "postal_code"):
            if _ZIP.match(value):
                postal = value
                break
    if not _ZIP.match(postal) or not _zip_matches_state(postal, state):
        postal = ""
        for address in _field_values(fields, "address"):
            found = re.search(r"\b(\d{5})(?:-\d{4})?\b", address)
            if found and _zip_matches_state(found.group(1), state):
                postal = found.group(0)
                break
    if postal:
        company["postal_code"] = postal
    else:
        company.pop("postal_code", None)
    for key in list(fields):
        leaf = _field_leaf(key)
        value = clean_text(fields.get(key))
        if leaf == "postal_code" and (not _ZIP.match(value) or not _zip_matches_state(value, state)):
            fields.pop(key, None)
        elif leaf in {"street_address", "street", "address"} and _COORD.match(value):
            fields.pop(key, None)
        elif leaf == "city" and (not city or value.lower() != city.lower()):
            fields.pop(key, None)
    primary = []
    for key, value in fields.items():
        if _field_leaf(key) in {"street_address", "street"}:
            line = _street_line(clean_text(value), city, state)
            if line and re.search(r"\d", line):
                primary.append(line)
    street, street_zip = _choose_street(fields, city, state, company.get("postal_code") or "")
    if street:
        replaced = bool(primary) and all(_folded(street) != _folded(line) for line in primary)
        for key in list(fields):
            if _field_leaf(key) in {"street_address", "street", "address"}:
                fields.pop(key, None)
        fields["street_address"] = street
        current_postal = clean_text(company.get("postal_code") or "")
        if street_zip and not current_postal.startswith(street_zip):
            company["postal_code"] = street_zip
        elif replaced and not street_zip:
            company.pop("postal_code", None)
    else:
        for key in list(fields):
            if _field_leaf(key) not in {"street_address", "street"}:
                continue
            line = _street_line(clean_text(fields.get(key)), city, state)
            if not line or not re.search(r"\d", line):
                fields.pop(key, None)
            else:
                fields[key] = line
    if not company.get("city"):
        inferred = _city_from_postal(company.get("postal_code") or "", state)
        if inferred:
            company["city"] = inferred
    if headcount:
        from app.filters import band_for_headcount

        fields["company_size"] = band_for_headcount(headcount)
        for key in list(fields):
            if key != "company_size" and _field_leaf(key) in {"company_size", "size"}:
                fields.pop(key, None)
    company["fields"] = fields


def company_from_payload(output: dict | None, raw, *, domain: str = "", fallback_name: str = "") -> dict:
    output = output or {}
    keys = (
        "name", "company_name", "legal_name", "organization", "website", "domain", "primary_domain", "industry", "sector",
        "employees", "employee_count", "headcount", "people_count", "estimated_num_employees", "company_size", "size", "revenue",
        "organization_revenue", "organization_revenue_printed",
        "website_url", "websiteurl", "company_website",
        "annual_revenue", "revenue_range", "estimated_revenue", "founded", "founded_year",
        "year_founded", "phone", "phone_number", "company_phone", "city", "locality",
        "state", "region", "country", "country_code", "location", "linkedin_url", "linkedinurl", "linkedin",
    )
    deep = _deep(output, keys)
    if raw is not output:
        for key, value in _deep(raw, keys).items():
            deep.setdefault(key, value)
    name = (
        _first(output, ("name", "organization"))
        or deep.get("name")
        or deep.get("company_name")
        or deep.get("legal_name")
        or deep.get("organization")
        or fallback_name
    )
    website = (
        _first(output, ("website", "website_url", "websiteurl", "company_website"))
        or deep.get("website")
        or deep.get("website_url")
        or deep.get("websiteurl")
        or deep.get("company_website")
        or ""
    )
    found_domain = normalize_domain(
        _first(output, ("domain", "primary_domain"))
        or deep.get("domain")
        or deep.get("primary_domain")
        or website
        or domain
    ) or domain
    if not website and found_domain:
        website = "https://" + found_domain
    industry = _first(output, ("industry",)) or deep.get("industry") or deep.get("sector")
    employees = pick_headcount(
        _first(output, ("estimated_num_employees",)),
        deep.get("estimated_num_employees"),
        _first(output, ("people_count",)),
        _first(output, ("employee_count",)),
        _first(output, ("headcount",)),
        _first(output, ("employees",)),
        _first(output, ("company_size",)),
        _first(output, ("size",)),
        deep.get("people_count"),
        deep.get("employee_count"),
        deep.get("headcount"),
        deep.get("employees"),
        deep.get("company_size"),
        deep.get("size"),
    )
    revenue = _money(
        deep.get("organization_revenue_printed"),
        deep.get("organization_revenue"),
        deep.get("revenue"),
        deep.get("annual_revenue"),
        deep.get("revenue_range"),
        deep.get("estimated_revenue"),
    )
    founded = _year(_first(output, ("founded",)) or deep.get("founded") or deep.get("founded_year") or deep.get("year_founded"))
    phone = deep.get("phone") or deep.get("phone_number") or deep.get("company_phone") or _nested_phone(output) or _nested_phone(raw if isinstance(raw, dict) else {})
    linkedin = clean_linkedin(
        _first(output, ("linkedin_url", "linkedinurl"))
        or deep.get("linkedin_url")
        or deep.get("linkedinurl")
        or deep.get("linkedin")
    )
    city = _first(output, ("city", "locality")) or deep.get("city") or deep.get("locality") or ""
    state = _first(output, ("state", "region")) or deep.get("state") or deep.get("region") or ""
    state = state_name(state) or state
    country_raw = deep.get("country") or deep.get("country_code") or ""
    country = country_code(country_raw) or country_raw
    location = _first(output, ("location",)) or deep.get("location")
    if location and not (city and state and country):
        parsed_city, parsed_state, parsed_country = split_location(location)
        city = city or parsed_city
        state = state_name(state) or state or parsed_state
        country = country_code(country) or country or parsed_country
    else:
        state = state_name(state) or state
        country = country_code(country) or country
    return {
        "domain": found_domain,
        "name": name or found_domain,
        "website": website,
        "linkedin": linkedin,
        "industry": industry,
        "employees": employees,
        "revenue": revenue,
        "founded_year": founded,
        "phone": phone,
        "city": city,
        "state": state,
        "country": country_label(country) if len(country) == 2 else country,
        "country_code": country if len(country) == 2 else country_code(country),
        "location": location,
    }


_SKIP_LISTS = {"people", "contacts", "persons", "profiles", "staff", "employees"}


def flatten_record(value, prefix: str = "", depth: int = 0, found: dict | None = None) -> dict:
    """Every scalar Treg returned, as a column name. Nested lists of people are left for the Contacts sheet."""
    if found is None:
        found = {}
    if depth > 8 or len(found) > 2000:
        return found
    if isinstance(value, dict):
        for key, item in value.items():
            token = str(key).strip()
            if not token or token.startswith("_"):
                continue
            path = f"{prefix}.{token}" if prefix else token
            leaf = token.lower().replace("-", "_")
            if isinstance(item, list) and leaf in _SKIP_LISTS:
                continue
            if isinstance(item, (dict, list)):
                flatten_record(item, path, depth + 1, found)
            else:
                text = _scalar(item)
                if text and path not in found:
                    found[path] = text
    elif isinstance(value, list) and depth <= 8:
        for index, item in enumerate(value[:50]):
            flatten_record(item, f"{prefix}.{index + 1}" if prefix else str(index + 1), depth + 1, found)
    return found


def people_from_payload(output: dict | None, raw) -> list[dict]:
    output = output or {}
    rows = output.get("people") if isinstance(output.get("people"), list) else None
    if rows is None and isinstance(raw, dict):
        for key in ("people", "contacts", "results", "data"):
            if isinstance(raw.get(key), list):
                rows = raw[key]
                break
    people = []
    for row in rows or []:
        person = _person(row)
        if person:
            people.append(person)
    return people


def _person(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    blobs = [row]
    for key, value in row.items():
        if isinstance(value, dict) and str(key).lower() in {"person", "profile", "contact", "member"}:
            blobs.append(value)
    first = last = title = linkedin = email = phone = ""
    verified = False
    for blob in blobs:
        first = first or _first(blob, ("first_name", "firstname", "first"))
        last = last or _first(blob, ("last_name", "lastname", "last"))
        title = title or _first(blob, ("title", "job_title", "jobtitle", "headline", "position", "current_title"))
        linkedin = linkedin or clean_linkedin(_first(blob, ("linkedin_url", "linkedin", "profile_url", "linkedinurl")))
        email = email or _first(blob, ("email", "work_email", "business_email"))
        phone = phone or _first(blob, ("phone", "phone_number", "mobile", "direct_phone"))
        if blob.get("verified") is True or str(blob.get("email_status") or "").lower() in {"verified", "valid"}:
            verified = True
        if not first and not last:
            full = _first(blob, ("full_name", "fullname", "name", "person_name"))
            first, last = split_person_name(full)
    if not any((first, last, linkedin, email)):
        return None
    return {
        "first_name": first,
        "last_name": last,
        "title": title,
        "linkedin": linkedin,
        "email": email,
        "email_status": "verified" if email and verified else ("found" if email else ""),
        "phone": phone,
        "fields": flatten_record(row),
    }


def email_status(output: dict | None, treg: dict | None) -> tuple[str, str]:
    output = output or {}
    email = clean_text(output.get("email"))
    verdict = str(((treg or {}).get("verification") or {}).get("verdict") or "").strip().lower()
    if verdict in {"valid", "deliverable", "verified", "ok"}:
        return email, "verified" if email else "not found"
    if email and output.get("verified") is True and verdict not in {"invalid", "undeliverable"}:
        return email, "verified"
    if email:
        return email, "found"
    return "", "not found"


def _states_named(text: str) -> set[str]:
    found: set[str] = set()
    masked = text.lower()
    phrases = sorted((key for key in US_STATES if len(key) > 2), key=len, reverse=True)
    for key in phrases:
        if key in masked:
            found.add(US_STATES[key])
            masked = masked.replace(key, " ")
    for key, name in US_STATES.items():
        if len(key) == 2 and re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", text.lower()):
            found.add(name)
    return found


def _named_countries(company: dict) -> set[str]:
    """Country codes written on the location, not the country column copied from the search."""
    parts = [company.get("location") or ""]
    fields = company.get("fields") if isinstance(company.get("fields"), dict) else {}
    for key, value in fields.items():
        lowered = str(key).lower()
        if "country" in lowered or _field_leaf(key) == "location":
            parts.append(clean_text(value))
    text = " ".join(clean_text(part) for part in parts).lower()
    found: set[str] = set()
    for name, code in COUNTRIES.items():
        if len(name) < 4:
            continue
        if re.search(rf"\b{re.escape(name)}\b", text):
            found.add(code)
    for match in re.findall(r"\|([a-z]{2})\b", text):
        if match.upper() in COUNTRY_NAMES:
            found.add(match.upper())
    return found


def location_mismatch(company: dict, *, city: str = "", state: str = "", country: str = "") -> str:
    """Return a reason when the company is clearly outside the confirmed place.

    A missing location is kept. A city with no state is kept for a state filter,
    because the record does not prove the company is outside that state.
    "US,CA,EU" accepts a company in any of those countries.
    """
    if country and ("," in country or country.upper() == "EU"):
        allowed = set(country_codes(country))
        found = (company.get("country_code") or country_code(company.get("country") or "")).upper()
        if not found and state_name(company.get("state") or ""):
            found = "US"
        if found and found not in allowed:
            return f"location {country_label(found) or found} is outside {countries_label(country)}"
        return ""
    if country:
        foreign = _named_countries(company) - {country.upper()}
        if foreign:
            return f"location does not match {country_label(country.upper()) or country}"
    text = " ".join(
        clean_text(company.get(key))
        for key in ("city", "state", "country", "country_code", "location")
    ).lower()
    if not text.strip():
        return ""
    if country:
        code = country.upper()
        company_code = (company.get("country_code") or country_code(company.get("country") or "")).upper()
        if company_code and company_code != code:
            return f"location does not match {country_label(code) or code}"
    if state:
        wanted = state_name(state) or state
        named = _states_named(text)
        company_state = state_name(company.get("state") or "")
        matched = wanted in named or company_state == wanted
        contradicted = (company_state and company_state != wanted) or (named and wanted not in named)
        if contradicted and not matched:
            return f"location does not match {wanted}"
    if city and city.lower() not in text:
        return f"location does not match {city}"
    return ""
