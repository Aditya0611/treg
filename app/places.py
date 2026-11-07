"""Place names used to turn a typed query into country, state, and city filters."""

import re

US_STATES = {
    "alabama": "Alabama",
    "al": "Alabama",
    "alaska": "Alaska",
    "ak": "Alaska",
    "arizona": "Arizona",
    "az": "Arizona",
    "arkansas": "Arkansas",
    "ar": "Arkansas",
    "california": "California",
    "ca": "California",
    "colorado": "Colorado",
    "co": "Colorado",
    "connecticut": "Connecticut",
    "ct": "Connecticut",
    "delaware": "Delaware",
    "de": "Delaware",
    "district of columbia": "District of Columbia",
    "washington dc": "District of Columbia",
    "washington, dc": "District of Columbia",
    "dc": "District of Columbia",
    "florida": "Florida",
    "fl": "Florida",
    "georgia": "Georgia",
    "ga": "Georgia",
    "hawaii": "Hawaii",
    "hi": "Hawaii",
    "idaho": "Idaho",
    "id": "Idaho",
    "illinois": "Illinois",
    "il": "Illinois",
    "indiana": "Indiana",
    "in": "Indiana",
    "iowa": "Iowa",
    "ia": "Iowa",
    "kansas": "Kansas",
    "ks": "Kansas",
    "kentucky": "Kentucky",
    "ky": "Kentucky",
    "louisiana": "Louisiana",
    "la": "Louisiana",
    "maine": "Maine",
    "me": "Maine",
    "maryland": "Maryland",
    "md": "Maryland",
    "massachusetts": "Massachusetts",
    "ma": "Massachusetts",
    "michigan": "Michigan",
    "mi": "Michigan",
    "minnesota": "Minnesota",
    "mn": "Minnesota",
    "mississippi": "Mississippi",
    "ms": "Mississippi",
    "missouri": "Missouri",
    "mo": "Missouri",
    "montana": "Montana",
    "mt": "Montana",
    "nebraska": "Nebraska",
    "ne": "Nebraska",
    "nevada": "Nevada",
    "nv": "Nevada",
    "new hampshire": "New Hampshire",
    "nh": "New Hampshire",
    "new jersey": "New Jersey",
    "nj": "New Jersey",
    "new mexico": "New Mexico",
    "nm": "New Mexico",
    "new york": "New York",
    "ny": "New York",
    "north carolina": "North Carolina",
    "nc": "North Carolina",
    "north dakota": "North Dakota",
    "nd": "North Dakota",
    "ohio": "Ohio",
    "oh": "Ohio",
    "oklahoma": "Oklahoma",
    "ok": "Oklahoma",
    "oregon": "Oregon",
    "or": "Oregon",
    "pennsylvania": "Pennsylvania",
    "pa": "Pennsylvania",
    "rhode island": "Rhode Island",
    "ri": "Rhode Island",
    "south carolina": "South Carolina",
    "sc": "South Carolina",
    "south dakota": "South Dakota",
    "sd": "South Dakota",
    "tennessee": "Tennessee",
    "tn": "Tennessee",
    "texas": "Texas",
    "tx": "Texas",
    "utah": "Utah",
    "ut": "Utah",
    "vermont": "Vermont",
    "vt": "Vermont",
    "virginia": "Virginia",
    "va": "Virginia",
    "washington": "Washington",
    "wa": "Washington",
    "west virginia": "West Virginia",
    "wv": "West Virginia",
    "wisconsin": "Wisconsin",
    "wi": "Wisconsin",
    "wyoming": "Wyoming",
    "wy": "Wyoming",
}

# Full names and common aliases. Two-letter codes are accepted separately.
COUNTRIES = {
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "us": "US",
    "united kingdom": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "canada": "CA",
    "mexico": "MX",
    "brazil": "BR",
    "brasil": "BR",
    "argentina": "AR",
    "chile": "CL",
    "colombia": "CO",
    "peru": "PE",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "netherlands": "NL",
    "holland": "NL",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "ireland": "IE",
    "portugal": "PT",
    "poland": "PL",
    "czech republic": "CZ",
    "czechia": "CZ",
    "romania": "RO",
    "hungary": "HU",
    "greece": "GR",
    "ukraine": "UA",
    "turkey": "TR",
    "israel": "IL",
    "united arab emirates": "AE",
    "uae": "AE",
    "saudi arabia": "SA",
    "india": "IN",
    "pakistan": "PK",
    "bangladesh": "BD",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "philippines": "PH",
    "thailand": "TH",
    "vietnam": "VN",
    "japan": "JP",
    "south korea": "KR",
    "korea": "KR",
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "australia": "AU",
    "new zealand": "NZ",
    "south africa": "ZA",
    "nigeria": "NG",
    "kenya": "KE",
    "egypt": "EG",
    "morocco": "MA",
}

COUNTRY_NAMES = {code: name for name, code in {
    "United States": "US",
    "United Kingdom": "GB",
    "Canada": "CA",
    "Mexico": "MX",
    "Brazil": "BR",
    "Argentina": "AR",
    "Chile": "CL",
    "Colombia": "CO",
    "Peru": "PE",
    "Germany": "DE",
    "France": "FR",
    "Spain": "ES",
    "Italy": "IT",
    "Netherlands": "NL",
    "Belgium": "BE",
    "Switzerland": "CH",
    "Austria": "AT",
    "Sweden": "SE",
    "Norway": "NO",
    "Denmark": "DK",
    "Finland": "FI",
    "Ireland": "IE",
    "Portugal": "PT",
    "Poland": "PL",
    "Czechia": "CZ",
    "Romania": "RO",
    "Hungary": "HU",
    "Greece": "GR",
    "Ukraine": "UA",
    "Turkey": "TR",
    "Israel": "IL",
    "United Arab Emirates": "AE",
    "Saudi Arabia": "SA",
    "India": "IN",
    "Pakistan": "PK",
    "Bangladesh": "BD",
    "Singapore": "SG",
    "Malaysia": "MY",
    "Indonesia": "ID",
    "Philippines": "PH",
    "Thailand": "TH",
    "Vietnam": "VN",
    "Japan": "JP",
    "South Korea": "KR",
    "China": "CN",
    "Hong Kong": "HK",
    "Taiwan": "TW",
    "Australia": "AU",
    "New Zealand": "NZ",
    "South Africa": "ZA",
    "Nigeria": "NG",
    "Kenya": "KE",
    "Egypt": "EG",
    "Morocco": "MA",
}.items()}


def state_name(value: str) -> str:
    key = " ".join((value or "").strip().lower().replace(".", "").split())
    return US_STATES.get(key, "")


def country_code(value: str) -> str:
    raw = " ".join((value or "").strip().lower().split())
    if not raw:
        return ""
    if raw in COUNTRIES:
        return COUNTRIES[raw]
    compact = raw.replace(".", "")
    if compact in COUNTRIES:
        return COUNTRIES[compact]
    letters = compact.upper()
    if len(letters) == 2 and letters.isalpha() and letters in COUNTRY_NAMES:
        return letters
    return ""


def country_label(code: str) -> str:
    return COUNTRY_NAMES.get((code or "").upper(), code or "")


EUROPE = (
    "GB", "DE", "FR", "IT", "ES", "NL", "BE", "SE", "PL", "AT", "CH", "DK", "FI", "NO",
    "IE", "PT", "CZ", "RO", "HU", "SK", "SI", "HR", "BG", "GR", "LT", "LV", "EE", "LU",
)
_REGIONS = {"eu": EUROPE, "europe": EUROPE, "european union": EUROPE}
_LIST_SPLIT = re.compile(r"\s*(?:,|;|/|&|\band\b|\bor\b)\s*", re.I)


def _place_parts(value: str) -> list[str]:
    return [part for part in _LIST_SPLIT.split(value or "") if part.strip()]


def country_codes(value: str) -> list[str]:
    """"US,CA,EU" → US, CA, and every European code. One country gives one code."""
    codes: list[str] = []
    for part in _place_parts(value):
        key = " ".join(part.lower().replace(".", "").split())
        found = _REGIONS.get(key) or ((country_code(part),) if country_code(part) else ())
        for code in found:
            if code not in codes:
                codes.append(code)
    return codes


def countries_value(value: str) -> str:
    """The country filter as stored: "US", or "US,CA,EU" for several. Unknown text is kept as typed."""
    parts = _place_parts(value)
    picked: list[str] = []
    for part in parts:
        key = " ".join(part.lower().replace(".", "").split())
        code = "EU" if key in _REGIONS else country_code(part)
        if code and code not in picked:
            picked.append(code)
    if not picked:
        return (value or "").strip()
    return ",".join(picked)


def countries_label(value: str) -> str:
    """"US,CA,EU" → "United States, Canada or Europe"."""
    names = []
    for part in _place_parts(value):
        key = " ".join(part.lower().replace(".", "").split())
        names.append("Europe" if key in _REGIONS else country_label(country_code(part) or part))
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " or " + names[-1]
