"""Shared text helpers, lookup tables and normalisers."""

import re
import unicodedata

COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "great britain": "GB", "london": "GB",
    "india": "IN", "canada": "CA", "germany": "DE", "france": "FR", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "belgium": "BE", "switzerland": "CH",
    "ireland": "IE", "portugal": "PT", "sweden": "SE", "norway": "NO", "denmark": "DK",
    "finland": "FI", "poland": "PL", "austria": "AT", "israel": "IL", "turkey": "TR",
    "türkiye": "TR", "ukraine": "UA", "australia": "AU", "new zealand": "NZ",
    "singapore": "SG", "japan": "JP", "china": "CN", "brazil": "BR", "mexico": "MX",
    "argentina": "AR", "south africa": "ZA", "nigeria": "NG", "kenya": "KE",
    "zambia": "ZM", "liberia": "LR", "ghana": "GH", "egypt": "EG",
    "united arab emirates": "AE", "uae": "AE", "dubai": "AE", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "philippines": "PH", "indonesia": "ID",
    "vietnam": "VN", "romania": "RO", "greece": "GR", "czech republic": "CZ",
    "czechia": "CZ", "hungary": "HU", "estonia": "EE", "lithuania": "LT", "latvia": "LV",
}

US_STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
    "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
    "va", "wa", "wv", "wi", "wy", "dc",
}
US_STATE_NAMES = {
    "california", "new york", "texas", "florida", "washington", "massachusetts", "illinois",
    "colorado", "georgia", "virginia", "north carolina", "new jersey", "pennsylvania", "ohio",
    "michigan", "arizona", "oregon", "utah", "minnesota", "tennessee", "maryland",
    "san francisco bay area", "greater seattle area", "new york city metropolitan area",
}

# timezone -> (country ISO2 or None, region)
TZ_COUNTRY = {
    "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Europe/London": "GB", "Europe/Brussels": "BE",
    "Europe/Paris": "FR", "Europe/Berlin": "DE", "Europe/Madrid": "ES", "Europe/Rome": "IT",
    "Europe/Amsterdam": "NL", "Europe/Dublin": "IE", "Europe/Lisbon": "PT", "Europe/Zurich": "CH",
    "Europe/Stockholm": "SE", "Europe/Warsaw": "PL", "Europe/Kiev": "UA", "Europe/Kyiv": "UA",
    "Europe/Istanbul": "TR", "Asia/Jerusalem": "IL", "Asia/Tel_Aviv": "IL", "Asia/Dubai": "AE",
    "Asia/Singapore": "SG", "Asia/Tokyo": "JP", "Asia/Shanghai": "CN", "Asia/Karachi": "PK",
    "Asia/Dhaka": "BD", "Asia/Manila": "PH", "Asia/Jakarta": "ID", "Australia/Sydney": "AU",
    "Australia/Melbourne": "AU", "Pacific/Auckland": "NZ", "America/Toronto": "CA",
    "America/Vancouver": "CA", "America/Sao_Paulo": "BR", "America/Mexico_City": "MX",
    "America/Argentina/Buenos_Aires": "AR", "Africa/Johannesburg": "ZA", "Africa/Lagos": "NG",
    "Africa/Nairobi": "KE", "Africa/Cairo": "EG",
}
US_TZ = {"America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
         "America/Phoenix", "America/Anchorage", "America/Detroit", "Pacific/Honolulu",
         "America/Boise", "America/Indiana/Indianapolis"}
# timezones that people commonly pick as "plain UTC" — too weak to use as a country prior
WEAK_TZ = {"Africa/Monrovia", "Africa/Abidjan", "Etc/UTC", "UTC", "Atlantic/Reykjavik", "GMT"}

COUNTRY_REGION = {
    "US": "NA", "CA": "NA", "MX": "LATAM", "BR": "LATAM", "AR": "LATAM",
    "GB": "EU", "IE": "EU", "FR": "EU", "DE": "EU", "ES": "EU", "IT": "EU", "NL": "EU",
    "BE": "EU", "CH": "EU", "PT": "EU", "SE": "EU", "NO": "EU", "DK": "EU", "FI": "EU",
    "PL": "EU", "AT": "EU", "UA": "EU", "RO": "EU", "GR": "EU", "CZ": "EU", "HU": "EU",
    "EE": "EU", "LT": "EU", "LV": "EU", "TR": "MEA", "IL": "MEA", "AE": "MEA", "EG": "MEA",
    "ZA": "MEA", "NG": "MEA", "KE": "MEA", "ZM": "MEA", "LR": "MEA", "GH": "MEA",
    "IN": "SA", "PK": "SA", "BD": "SA", "LK": "SA",
    "SG": "APAC", "JP": "APAC", "CN": "APAC", "PH": "APAC", "ID": "APAC", "VN": "APAC",
    "AU": "APAC", "NZ": "APAC",
}

NICKNAMES = {
    "chris": {"christopher", "christian", "christina", "christine"},
    "jeff": {"jeffrey", "geoffrey", "jeffery"}, "mike": {"michael"}, "matt": {"matthew"},
    "dave": {"david"}, "dan": {"daniel"}, "danny": {"daniel"}, "tom": {"thomas"},
    "nick": {"nicholas", "nicolas"}, "alex": {"alexander", "alexandra", "alexis"},
    "sam": {"samuel", "samantha"}, "ben": {"benjamin"}, "joe": {"joseph"}, "bob": {"robert"},
    "rob": {"robert", "robin"}, "bill": {"william"}, "will": {"william"}, "jim": {"james"},
    "jon": {"jonathan", "john"}, "tony": {"anthony"}, "andy": {"andrew"}, "drew": {"andrew"},
    "steve": {"steven", "stephen"}, "kate": {"katherine", "kathryn", "catherine"},
    "liz": {"elizabeth"}, "beth": {"elizabeth"}, "zach": {"zachary", "zachariah"},
    "zack": {"zachary"}, "greg": {"gregory"}, "pete": {"peter"}, "ed": {"edward"},
    "ted": {"edward", "theodore"}, "rick": {"richard"}, "rich": {"richard"},
    "dick": {"richard"}, "jen": {"jennifer"}, "jenny": {"jennifer"}, "abby": {"abigail"},
    "manny": {"manuel"}, "raj": {"rajesh", "rajendra"}, "kev": {"kevin"},
}

TITLE_WORDS = {
    "founder", "co-founder", "cofounder", "ceo", "cto", "cfo", "coo", "cmo", "cro", "cpo",
    "head", "director", "vp", "vice", "president", "lead", "leader", "engineer", "developer",
    "manager", "consultant", "partner", "owner", "principal", "senior", "sr", "staff",
    "architect", "analyst", "designer", "mod", "moderator", "advisor", "investor", "officer",
    "specialist", "strategist", "marketer", "recruiter", "scientist", "researcher",
    "executive", "chief", "coach", "author", "writer", "editor", "evangelist", "intern",
    "associate", "representative", "rep", "sdr", "bdr", "ae", "revops", "operations",
    "product", "community", "content", "growth", "sales", "marketing", "security",
    "relations", "business", "managing", "board", "member", "chair", "chairman",
}
SENIORITY = [
    ("founder", 5), ("co-founder", 5), ("cofounder", 5), ("owner", 5), ("ceo", 5),
    ("chief", 5), ("president", 5), ("cto", 5), ("cfo", 5), ("coo", 5), ("cmo", 5), ("cro", 5),
    ("partner", 4), ("vp", 4), ("vice president", 4), ("head", 4), ("director", 4),
    ("principal", 3), ("lead", 3), ("manager", 3), ("managing", 3), ("senior", 2), ("sr", 2),
    ("staff", 2), ("consultant", 2), ("engineer", 1), ("developer", 1), ("analyst", 1),
    ("associate", 1), ("intern", 0),
]

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "at", "in", "on", "for", "to", "with", "by", "from",
    "is", "my", "me", "i", "we", "our", "your", "you", "it", "this", "that", "as", "be", "are",
    "looking", "much", "more", "lover", "occasional", "x", "etc", "https", "http", "www", "com",
}

COMPANY_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|plc|pvt|private|labs?|hq|group|"
    r"technologies|technology|tech|ai|io|app|software|solutions|the)\b\.?", re.I)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str | None) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def tokens(s: str | None) -> list[str]:
    return [t for t in norm(s).split() if t not in STOPWORDS and len(t) > 1]


def norm_company(s: str | None) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)                  # "Inventive AI (YC S23)" -> "inventive ai"
    s = re.sub(r"\.(com|ai|io|co|uk|life|net|org|app|dev|tech|so|xyz)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = COMPANY_SUFFIXES.sub(" ", s)
    return " ".join(s.split())


def registrable_domain(host: str) -> str:
    host = host.lower().strip().strip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def country_from_text(text: str | None) -> str | None:
    """Best-effort country ISO2 from a free-text location."""
    if not text:
        return None
    t = " " + norm(text) + " "
    for name, iso in sorted(COUNTRY_NAMES.items(), key=lambda kv: -len(kv[0])):
        if len(name) <= 3:
            # short codes only as standalone uppercase tokens in the raw text
            if re.search(rf"(?<![A-Za-z]){re.escape(name.upper())}(?![A-Za-z])", text):
                return iso
            continue
        if f" {name} " in t:
            return iso
    for st in US_STATE_NAMES:
        if f" {st} " in t:
            return "US"
    m = re.search(r",\s*([A-Z]{2})\b", text)
    if m and m.group(1).lower() in US_STATES:
        return "US"
    return None


def tz_prior(tz: str | None) -> tuple[str | None, str | None, bool]:
    """timezone -> (country, region, is_weak)."""
    if not tz:
        return None, None, True
    if tz in WEAK_TZ:
        return None, None, True
    if tz in US_TZ:
        return "US", "NA", False
    c = TZ_COUNTRY.get(tz)
    if c:
        return c, COUNTRY_REGION.get(c), False
    head = tz.split("/")[0]
    region = {"America": "NA", "Europe": "EU", "Asia": None, "Africa": "MEA",
              "Australia": "APAC", "Pacific": "APAC"}.get(head)
    return None, region, True


def seniority(title: str | None) -> int | None:
    t = " " + norm(title).replace("co founder", "cofounder") + " "
    best = None
    for word, lvl in SENIORITY:
        if f" {norm(word).replace('co founder', 'cofounder')} " in t:
            best = lvl if best is None else max(best, lvl)
    return best


def parse_size(s: str | None) -> tuple[int, int] | None:
    """'11–50 Employees' -> (11, 50); '10,001+ employees' -> (10001, 10**7)."""
    if not s:
        return None
    s = s.replace(",", "").replace("–", "-").replace("—", "-")
    m = re.search(r"(\d+)\s*-\s*(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+)\s*\+", s)
    if m:
        return int(m.group(1)), 10 ** 7
    return None
