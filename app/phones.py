"""See whether a company phone belongs in the place the search asked for."""

from __future__ import annotations

import re

from app.places import country_code, country_label, state_name

_DIGITS = re.compile(r"\D")
_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}
_CALLING_CODES = (
    ("1868", "TT"),
    ("1876", "JM"),
    ("1809", "DO"),
    ("1829", "DO"),
    ("1849", "DO"),
    ("971", "AE"),
    ("966", "SA"),
    ("972", "IL"),
    ("974", "QA"),
    ("965", "KW"),
    ("968", "OM"),
    ("973", "BH"),
    ("852", "HK"),
    ("886", "TW"),
    ("880", "BD"),
    ("358", "FI"),
    ("353", "IE"),
    ("351", "PT"),
    ("420", "CZ"),
    ("380", "UA"),
    ("254", "KE"),
    ("234", "NG"),
    ("233", "GH"),
    ("91", "IN"),
    ("92", "PK"),
    ("94", "LK"),
    ("86", "CN"),
    ("81", "JP"),
    ("82", "KR"),
    ("65", "SG"),
    ("61", "AU"),
    ("64", "NZ"),
    ("60", "MY"),
    ("63", "PH"),
    ("62", "ID"),
    ("66", "TH"),
    ("84", "VN"),
    ("55", "BR"),
    ("54", "AR"),
    ("52", "MX"),
    ("49", "DE"),
    ("48", "PL"),
    ("47", "NO"),
    ("46", "SE"),
    ("45", "DK"),
    ("44", "GB"),
    ("43", "AT"),
    ("41", "CH"),
    ("39", "IT"),
    ("34", "ES"),
    ("33", "FR"),
    ("32", "BE"),
    ("31", "NL"),
    ("27", "ZA"),
    ("20", "EG"),
    ("7", "RU"),
    ("1", "US"),
)
_CANADA = {
    "204", "226", "236", "249", "250", "263", "289", "306", "343", "354", "365", "367",
    "368", "382", "403", "416", "418", "428", "431", "437", "438", "450", "468", "474",
    "506", "514", "519", "548", "579", "581", "584", "587", "604", "613", "639", "647",
    "672", "683", "705", "709", "742", "753", "778", "780", "782", "807", "819", "825",
    "867", "873", "879", "902", "905",
}
_AREA = {
    "205": "Alabama", "251": "Alabama", "256": "Alabama", "334": "Alabama", "659": "Alabama", "938": "Alabama",
    "907": "Alaska",
    "480": "Arizona", "520": "Arizona", "602": "Arizona", "623": "Arizona", "928": "Arizona",
    "479": "Arkansas", "501": "Arkansas", "870": "Arkansas",
    "209": "California", "213": "California", "279": "California", "310": "California", "323": "California",
    "408": "California", "415": "California", "424": "California", "442": "California", "510": "California",
    "530": "California", "559": "California", "562": "California", "619": "California", "626": "California",
    "628": "California", "650": "California", "657": "California", "661": "California", "669": "California",
    "707": "California", "714": "California", "747": "California", "760": "California", "805": "California",
    "818": "California", "831": "California", "858": "California", "909": "California", "916": "California",
    "925": "California", "949": "California", "951": "California",
    "303": "Colorado", "719": "Colorado", "720": "Colorado", "970": "Colorado", "983": "Colorado",
    "203": "Connecticut", "475": "Connecticut", "860": "Connecticut", "959": "Connecticut",
    "302": "Delaware",
    "202": "District of Columbia",
    "239": "Florida", "305": "Florida", "321": "Florida", "352": "Florida", "386": "Florida", "407": "Florida",
    "561": "Florida", "689": "Florida", "727": "Florida", "754": "Florida", "772": "Florida", "786": "Florida",
    "813": "Florida", "850": "Florida", "863": "Florida", "904": "Florida", "941": "Florida", "954": "Florida",
    "229": "Georgia", "404": "Georgia", "470": "Georgia", "478": "Georgia", "678": "Georgia", "706": "Georgia",
    "762": "Georgia", "770": "Georgia", "912": "Georgia",
    "808": "Hawaii",
    "208": "Idaho", "986": "Idaho",
    "217": "Illinois", "224": "Illinois", "309": "Illinois", "312": "Illinois", "331": "Illinois", "618": "Illinois",
    "630": "Illinois", "708": "Illinois", "773": "Illinois", "779": "Illinois", "815": "Illinois", "847": "Illinois", "872": "Illinois",
    "219": "Indiana", "260": "Indiana", "317": "Indiana", "463": "Indiana", "574": "Indiana", "765": "Indiana", "812": "Indiana", "930": "Indiana",
    "319": "Iowa", "515": "Iowa", "563": "Iowa", "641": "Iowa", "712": "Iowa",
    "316": "Kansas", "620": "Kansas", "785": "Kansas", "913": "Kansas",
    "270": "Kentucky", "364": "Kentucky", "502": "Kentucky", "606": "Kentucky", "859": "Kentucky",
    "225": "Louisiana", "318": "Louisiana", "337": "Louisiana", "504": "Louisiana", "985": "Louisiana",
    "207": "Maine",
    "240": "Maryland", "301": "Maryland", "410": "Maryland", "443": "Maryland", "667": "Maryland",
    "339": "Massachusetts", "351": "Massachusetts", "413": "Massachusetts", "508": "Massachusetts", "617": "Massachusetts",
    "774": "Massachusetts", "781": "Massachusetts", "857": "Massachusetts", "978": "Massachusetts",
    "231": "Michigan", "248": "Michigan", "269": "Michigan", "313": "Michigan", "517": "Michigan", "586": "Michigan",
    "616": "Michigan", "734": "Michigan", "810": "Michigan", "906": "Michigan", "947": "Michigan", "989": "Michigan",
    "218": "Minnesota", "320": "Minnesota", "507": "Minnesota", "612": "Minnesota", "651": "Minnesota", "763": "Minnesota", "952": "Minnesota",
    "228": "Mississippi", "601": "Mississippi", "662": "Mississippi", "769": "Mississippi",
    "314": "Missouri", "417": "Missouri", "573": "Missouri", "636": "Missouri", "660": "Missouri", "816": "Missouri",
    "406": "Montana",
    "308": "Nebraska", "402": "Nebraska", "531": "Nebraska",
    "702": "Nevada", "725": "Nevada", "775": "Nevada",
    "603": "New Hampshire",
    "201": "New Jersey", "551": "New Jersey", "609": "New Jersey", "732": "New Jersey", "848": "New Jersey",
    "856": "New Jersey", "862": "New Jersey", "908": "New Jersey", "973": "New Jersey",
    "505": "New Mexico", "575": "New Mexico",
    "212": "New York", "315": "New York", "332": "New York", "347": "New York", "516": "New York", "518": "New York",
    "585": "New York", "607": "New York", "631": "New York", "646": "New York", "716": "New York", "718": "New York",
    "845": "New York", "914": "New York", "917": "New York", "929": "New York", "934": "New York",
    "252": "North Carolina", "336": "North Carolina", "704": "North Carolina", "743": "North Carolina", "828": "North Carolina",
    "910": "North Carolina", "919": "North Carolina", "980": "North Carolina", "984": "North Carolina",
    "701": "North Dakota",
    "216": "Ohio", "220": "Ohio", "234": "Ohio", "330": "Ohio", "380": "Ohio", "419": "Ohio", "440": "Ohio",
    "513": "Ohio", "567": "Ohio", "614": "Ohio", "740": "Ohio", "937": "Ohio",
    "405": "Oklahoma", "539": "Oklahoma", "580": "Oklahoma", "918": "Oklahoma",
    "458": "Oregon", "503": "Oregon", "541": "Oregon", "971": "Oregon",
    "215": "Pennsylvania", "223": "Pennsylvania", "267": "Pennsylvania", "272": "Pennsylvania", "412": "Pennsylvania",
    "484": "Pennsylvania", "570": "Pennsylvania", "610": "Pennsylvania", "717": "Pennsylvania", "724": "Pennsylvania",
    "814": "Pennsylvania", "878": "Pennsylvania",
    "401": "Rhode Island",
    "803": "South Carolina", "843": "South Carolina", "854": "South Carolina", "864": "South Carolina",
    "605": "South Dakota",
    "423": "Tennessee", "615": "Tennessee", "629": "Tennessee", "731": "Tennessee", "865": "Tennessee", "901": "Tennessee", "931": "Tennessee",
    "210": "Texas", "214": "Texas", "254": "Texas", "281": "Texas", "325": "Texas", "346": "Texas", "361": "Texas",
    "409": "Texas", "430": "Texas", "432": "Texas", "469": "Texas", "512": "Texas", "682": "Texas", "713": "Texas",
    "726": "Texas", "737": "Texas", "806": "Texas", "817": "Texas", "830": "Texas", "832": "Texas", "903": "Texas",
    "915": "Texas", "936": "Texas", "940": "Texas", "945": "Texas", "956": "Texas", "972": "Texas", "979": "Texas",
    "385": "Utah", "435": "Utah", "801": "Utah",
    "802": "Vermont",
    "276": "Virginia", "434": "Virginia", "540": "Virginia", "571": "Virginia", "703": "Virginia", "757": "Virginia", "804": "Virginia",
    "206": "Washington", "253": "Washington", "360": "Washington", "425": "Washington", "509": "Washington", "564": "Washington",
    "304": "West Virginia", "681": "West Virginia",
    "262": "Wisconsin", "414": "Wisconsin", "534": "Wisconsin", "608": "Wisconsin", "715": "Wisconsin", "920": "Wisconsin",
    "307": "Wyoming",
}


def phone_place(phone: str) -> tuple[str, str]:
    """Country code and US state implied by the number. A toll-free number has no state."""
    digits = _DIGITS.sub("", phone or "")
    if not digits:
        return "", ""
    for prefix, code in _CALLING_CODES:
        if digits.startswith(prefix):
            rest = digits[len(prefix):]
            if code == "US" and len(rest) >= 3:
                area = rest[:3]
                if area in _TOLL_FREE:
                    return "US", ""
                if area in _CANADA:
                    return "CA", ""
                return "US", _AREA.get(area, "")
            return code, ""
    return "", ""


def phone_location_flag(phone: str, *, state: str = "", country: str = "") -> str:
    """A phone whose country or area code is somewhere else than the search."""
    found_country, found_state = phone_place(phone)
    if not found_country:
        return ""
    wanted_country = country_code(country)
    wanted_state = state_name(state)
    if wanted_country and found_country != wanted_country:
        return f"phone country {country_label(found_country)} does not match {country_label(wanted_country)}"
    if wanted_state and found_state and found_state != wanted_state:
        return f"phone area code is {found_state}, not {wanted_state}"
    return ""


def foreign_phone_reason(phone: str, *, state: str = "", country: str = "") -> str:
    """A non-US calling code is a reason to drop the row. Another US area code is not."""
    flag = phone_location_flag(phone, state=state, country=country)
    if not flag.startswith("phone country"):
        return ""
    digits = _DIGITS.sub("", phone or "")
    found, _found_state = phone_place(phone)
    for prefix, code in _CALLING_CODES:
        if not digits.startswith(prefix):
            continue
        if found == "CA" and prefix == "1":
            return "phone +1 Canada"
        if code == found and code != "US":
            return f"phone +{prefix}"
    return flag
