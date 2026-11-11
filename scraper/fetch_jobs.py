"""
Sammelt offene IT-Stellen (Client/Endpoint, M365, Support, Systemtechnik ...)
rund um Baden/Zuerich und schreibt docs/jobs.json fuer die Webseite.

Quellen:
  1. jobs.ch            (oeffentliche Such-API, Kantone ZH + AG)
  2. job-room.ch        (Stellenboerse des Bundes)
  3. Kanton Aargau      (Stellenmarkt, Rubrik Informatik)
  4. ETH Zuerich        (jobs.ethz.ch, IT-Stellen)

Nur Python-Standardbibliothek, damit es ohne Installation auf GitHub Actions laeuft.
Aufruf:  python scraper/fetch_jobs.py
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "docs" / "jobs.json"
PLZ_OUT_FILE = ROOT / "docs" / "plz.json"
CACHE_FILE = ROOT / "scraper" / "cache" / "details.json"
PLZ_FILE = ROOT / "scraper" / "plz_ch.tsv"

UA = "Mozilla/5.0 (compatible; it-stellen-privat/1.0; persoenliche Stellensuche)"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date().isoformat()

# Suchbegriffe (werden bei jobs.ch und job-room.ch einzeln abgefragt)
KEYWORDS = [
    "Jamf", "macOS", "Apple Administrator", "Intune", "Autopilot", "Endpoint Management",
    "Client Engineer", "Workplace Engineer", "Modern Workplace", "Microsoft 365", "M365",
    "Entra", "Exchange Online", "PowerShell", "IT-Supporter", "ICT-Supporter", "IT Support",
    "Service Desk", "Helpdesk", "2nd Level Support", "IT-Allrounder", "ICT Allrounder",
    "Systemtechniker", "System Engineer", "Systemadministrator", "ICT-Systemspezialist",
    "Informatiker Systemtechnik", "IT-Techniker", "ICT-Techniker", "Client Management",
    "IT-Verantwortlicher", "Informatik Allrounder",
]
JOBSCH_REGIONS = [7, 12]          # jobs.ch: 7 = Kanton Zuerich, 12 = Kanton Aargau
JOBROOM_CANTONS = ["AG", "ZH"]

# Bekannte Orte, bei denen der erste PLZ-Treffer daneben liegt (z.B. Zuerich -> Flughafen)
PLACE_PLZ = {
    "zürich": "8001", "zurich": "8001", "zuerich": "8001", "baden": "5400", "aarau": "5000",
    "brugg": "5200", "winterthur": "8400", "bern": "3011", "berne": "3011", "basel": "4051",
    "luzern": "6003", "lucerne": "6003", "geneva": "1201", "genève": "1201", "genf": "1201",
    "lausanne": "1003", "zug": "6300", "olten": "4600", "st. gallen": "9000", "dietikon": "8953",
    "schlieren": "8952", "wettingen": "5430", "spreitenbach": "8957", "regensdorf": "8105",
    "kloten": "8302", "opfikon": "8152", "glattbrugg": "8152", "wallisellen": "8304",
    "dübendorf": "8600", "uster": "8610", "horgen": "8810", "thalwil": "8800",
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http(url: str, data: bytes | None = None, headers: dict | None = None,
         tries: int = 3, timeout: int = 40) -> tuple[bytes, dict]:
    h = {"User-Agent": UA, "Accept-Language": "de-CH,de;q=0.9"}
    if headers:
        h.update(headers)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=h,
                                         method="POST" if data is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), dict(r.headers)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def get_text(url: str) -> str:
    body, _ = http(url)
    return body.decode("utf-8", errors="replace")


def get_json(url: str):
    body, _ = http(url, headers={"Accept": "application/json"})
    return json.loads(body)


# --------------------------------------------------------------------------- #
# Text-Helfer
# --------------------------------------------------------------------------- #

def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d|tr|dd|dt)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s)
    return s.strip()


def clean(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"</?em>", "", s)
    s = re.sub(r"\\([&*_#\[\]()-])", r"\1", s)    # Markdown-Escapes von job-room
    s = re.sub(r"\*\*|__", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def snippet(s: str, n: int = 600) -> str:
    s = clean(s)
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + " …"


def parse_date_ch(s: str | None) -> str | None:
    """'30.11.2026' -> '2026-11-30'"""
    if not s:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        d, mth, y = map(int, m.groups())
        try:
            return datetime(y, mth, d).date().isoformat()
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    return None


def parse_pensum(text: str) -> tuple[int | None, int | None]:
    """Sucht '50-80 %', '80 – 100%', '60%' im Text."""
    t = (text or "").replace("–", "-").replace("—", "-")
    m = re.search(r"(\d{2,3})\s*%?\s*(?:-|bis)\s*(\d{2,3})\s*%", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 100:
            return a, b
    m = re.search(r"(\d{2,3})\s*%", t)
    if m:
        a = int(m.group(1))
        if 0 < a <= 100:
            return a, a
    return None, None


def to_int(s) -> int | None:
    try:
        m = re.search(r"\d+", str(s))
        return int(m.group(0)) if m else None
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Klassifizierung: Themen, Rolle, Erfahrung, Match
# --------------------------------------------------------------------------- #

TOPICS: list[tuple[str, str]] = [
    ("Apple / Jamf", r"\bjamf|\bmac ?os\b|\bmacs?\b|\bios\b|ipados|apple"),
    ("Intune / Endpoint", r"intune|autopilot|endpoint|\bMDM\b|\bUEM\b|\bSCCM\b|\bMECM\b|configmgr|client[- ]?(?:management|engineer|admin|spezialist|techni)|workplace|arbeitsplatz"),
    ("M365 / Entra", r"microsoft 365|\bm365\b|office 365|\bo365\b|\bentra\b|azure ad|\baad\b|exchange|sharepoint|modern workplace"),
    ("Support", r"support|service ?desk|help ?desk|\b(?:1st|2nd|first|second)[- ]level|anwenderbetreuung|benutzerbetreuung|user support"),
    ("Systemtechnik", r"systemtechni|system ?engineer|system ?administr|sysadmin|systemspezialist|windows server|\bserver\b|vmware|hyper-v|virtualisier|infrastruktur|infrastructure|active directory"),
    ("Netzwerk / Security", r"netzwerk|network|firewall|security|sicherheit|cisco|fortinet|\bwlan\b|\blan\b"),
    ("Scripting / Automation", r"powershell|scripting|skript|automatisier|automation|power automate|\bpython\b|ansible"),
    ("Cloud / Azure", r"\bazure\b|\baws\b|\bcloud\b"),
    ("Beschaffung / Asset", r"beschaffung|asset[- ]?manag|lifecycle|leasing|procurement|inventar"),
]

# Profil-Begriffe fuer den Match-Score: (Anzeige, Regex, Gewicht)
SKILLS: list[tuple[str, str, int]] = [
    ("Jamf", r"\bjamf", 5),
    ("Intune", r"\bintune", 4),
    ("Autopilot", r"autopilot", 3),
    ("macOS", r"\bmac ?os\b|\bmacs?\b", 3),
    ("iOS", r"\bios\b|ipados|iphone|ipad", 2),
    ("Apple", r"\bapple\b", 2),
    ("Entra ID", r"\bentra\b|azure ad|azure active directory", 3),
    ("Exchange", r"exchange", 2),
    ("M365", r"microsoft 365|\bm365\b|office 365|\bo365\b", 3),
    ("PowerShell", r"powershell", 3),
    ("Automatisierung", r"automatisier|automation|scripting|skript", 2),
    ("MDM", r"\bMDM\b|\bUEM\b|mobile device", 2),
    ("Client/Workplace", r"client|workplace|arbeitsplatz|endpoint", 2),
    ("Allrounder", r"allround|generalist|it-verantwortlich|informatik-verantwortlich|ict-verantwortlich", 3),
    ("Support", r"support|service ?desk|help ?desk", 1),
    ("Active Directory", r"active directory", 1),
    ("Beschaffung/Leasing", r"beschaffung|leasing|asset[- ]?manag|lifecycle", 1),
    ("KMU", r"\bKMU\b", 1),
]

# Eindeutig IT im Titel
IT_RE = re.compile(
    r"informati|\bIT\b|\bICT\b|\bIT-|\bICT-|support|help ?desk|service ?desk|sysadmin|system ?admin|"
    r"systemtechni|system ?engineer|systemspezialist|netzwerk|network|cloud|jamf|intune|microsoft|m365|"
    r"workplace|client|endpoint|devops|cyber|plattform engineer|platform engineer|infrastru|azure|"
    r"apple|mac ?os|windows|linux|server|citrix|vmware|datacenter|rechenzentrum", re.I)
# Nur zusammen mit IT-Kontext (Kategorie oder Text) gueltig
WEAK_IT_RE = re.compile(r"engineer|techniker|technician|security|administrator|system", re.I)
AGENCY_RE = re.compile(
    r"yellowshark|rocken|myitjob|digitalent|adecco|randstad|manpower|hays|michael page|page personnel|"
    r"kelly|careerplus|coople|universal-job|jobup|experis|robert half|gi group|interiman|work selection|"
    r"freestar|evergreen|kennedy|personal|stellenvermittl|recruiting|headhunt|consult(?:ing)? group", re.I)
EXCLUDE_RE = re.compile(
    r"lernende|lehrstelle|\blehre\b|praktik|praktikum|internship|\bintern\b|werkstudent|student|"
    r"doktorand|doctoral|phd|postdoc|zivildienst|schnupper|berufsbildner|lehrperson|dozent|"
    r"elektroinstall|elektriker|automatiker|polymechan|heizung|sanitär|lüftung|haustechni|"
    r"verkauf|aussendienst|sales|account manag|key account|recruit|kundenberat|kundendienst|callcenter|"
    r"medizintechni|photovoltaik|solar|bahnsignal|(?:DE|FR|EN|deutsch|französisch)\s*(?:/|oder|und|,)\s*IT\b", re.I)
DEV_RE = re.compile(
    r"entwickler|developer|software ?engineer|programmier|frontend|backend|full ?stack|"
    r"\bjava\b|\.net\b|\bc#|data scien|data engineer|machine learning|\bml\b|\bsap\b|abap|"
    r"applikationsentwick|applikationsverantw|business analyst|requirements|scrum|product owner|tester|test engineer", re.I)
LEAD_RE = re.compile(r"leiter|leiterin|leitung|head of|teamlead|team lead|teamleit|\bCIO\b|\bCTO\b|bereichsleit|abteilungsleit", re.I)
SUPPORT_RE = re.compile(r"support|help ?desk|service ?desk|supporter|anwenderbetreu|benutzerbetreu|\b(?:1st|2nd|first|second)[- ]level", re.I)
ALLROUND_RE = re.compile(r"allround|generalist|it-verantwortlich|ict-verantwortlich|informatik-verantwortlich|verantwortliche[rn]? (?:it|ict|informatik)", re.I)
SENIOR_RE = re.compile(r"\bsenior\b|\bsr\.?\b|\blead\b|principal|\bexpert\b|architekt|architect", re.I)
JUNIOR_RE = re.compile(r"\bjunior\b|\bjr\.?\b|einsteiger|berufseinst", re.I)
TEMP_RE = re.compile(r"befristet|temporär|temporaer|\btemp\b|projekteinsatz|fixed-term|mutterschaftsvertretung|stellvertretung", re.I)


def classify(table: list[tuple[str, str]], primary: str, fallback: str = "") -> list[str]:
    found = [name for name, rx in table if re.search(rx, primary or "", re.I)]
    if fallback:
        found += [name for name, rx in table if name not in found and re.search(rx, fallback, re.I)]
    return found


def role_of(title: str) -> str:
    if DEV_RE.search(title):
        return "Entwicklung"
    if LEAD_RE.search(title):
        return "Führung / Projektleitung"
    if ALLROUND_RE.search(title):
        return "Allrounder"
    if SUPPORT_RE.search(title):
        return "Support"
    return "Technik / Engineering"


def match_of(title: str, text: str) -> tuple[int, list[str]]:
    score, hits = 0, []
    for name, rx, w in SKILLS:
        in_title = re.search(rx, title or "", re.I)
        in_text = re.search(rx, text or "", re.I)
        if in_title or in_text:
            hits.append(name)
            score += w * (2 if in_title else 1)
    return score, hits


def level_of(title: str) -> str | None:
    if JUNIOR_RE.search(title):
        return "Junior"
    if SENIOR_RE.search(title):
        return "Senior"
    return None


def is_it_job(title: str, it_context: bool = False, text: str = "") -> bool:
    if EXCLUDE_RE.search(title):
        return False
    if IT_RE.search(title):
        return True
    if WEAK_IT_RE.search(title) and not re.search(r"servicetechni|elektr|mechani|maschin|fahrzeug|truck|anlagen|hlk|lift|aufzug|fahrtrepp|kälte|baumasch", title, re.I):
        return it_context or bool(re.search(r"informatik|\bIT\b|\bICT\b|microsoft|windows|netzwerk|server", text or ""))
    return False


# --------------------------------------------------------------------------- #
# Orte / Koordinaten
# --------------------------------------------------------------------------- #

class Geo:
    def __init__(self, path: Path):
        self.by_plz: dict[str, tuple[float, float, str]] = {}
        self.by_name: dict[str, list[tuple[float, float, str]]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            plz, place, canton, lat, lon, commune = parts
            rec = (float(lat), float(lon), canton)
            self.by_plz.setdefault(plz, rec)
            for n in {self.norm(place), self.norm(commune)}:
                if n:
                    self.by_name.setdefault(n, []).append(rec)

    @staticmethod
    def norm(s: str) -> str:
        s = (s or "").lower()
        s = re.sub(r"\ba\.\s*a\.?", "am albis", s)
        s = re.sub(r"\ba\.\s*s\.?", "am see", s)
        s = re.sub(r"\(.*?\)", "", s)
        s = re.sub(r"\b(ag|zh|lu|zg|so|bl|bs|sz|be|sg|tg)\b", "", s)
        s = re.sub(r"\s+\d+$", "", s)
        s = re.sub(r"[^a-zäöüéèàç. -]+", " ", s)
        return s.strip()

    def lookup(self, plz: str | None = None, name: str | None = None,
               canton: str | None = None) -> tuple[float, float, str] | None:
        if plz and plz in self.by_plz:
            return self.by_plz[plz]
        if name:
            # "Zürich, Baden oder Remote" -> erster Ort
            first_place = re.split(r",|/| oder | und | or |;|\|", name.strip())[0].strip()
            for cand in (first_place, name):
                n = self.norm(cand)
                if n in PLACE_PLZ:
                    return self.by_plz.get(PLACE_PLZ[n])
                p = re.search(r"\b([1-9]\d{3})\b", cand)
                if p and p.group(1) in self.by_plz:
                    return self.by_plz[p.group(1)]
                cands = self.by_name.get(n)
                if not cands:
                    word = re.split(r"[-\s]", cand.strip())[0]
                    cands = self.by_name.get(self.norm(word))
                    if self.norm(word) in PLACE_PLZ:
                        return self.by_plz.get(PLACE_PLZ[self.norm(word)])
                if cands:
                    if canton:
                        for c in cands:
                            if c[2] == canton:
                                return c
                    return cands[0]
        return None


# --------------------------------------------------------------------------- #
# Quelle 1: jobs.ch
# --------------------------------------------------------------------------- #

JOBSCH = "https://www.jobs.ch/api/v1/public/search"


def jobsch_search(keyword: str) -> list[dict]:
    out = []
    regions = "&".join(f"region-ids%5B%5D={r}" for r in JOBSCH_REGIONS)
    for page in range(1, 11):
        url = f"{JOBSCH}?query={urllib.parse.quote(keyword)}&rows=20&page={page}&{regions}"
        d = get_json(url)
        docs = d.get("documents") or []
        out.extend(docs)
        if not docs or page >= int(d.get("num_pages") or 1):
            break
        time.sleep(0.3)
    return out


def jobsch_detail(job_id: str) -> dict:
    d = get_json(f"{JOBSCH}/job/{job_id}")
    return {
        "text": strip_tags(d.get("template_text") or "")[:6000],
        "zip": d.get("zipcode") or "",
        "apply": d.get("application_url") or d.get("external_url") or None,
        "end": d.get("publication_end_date"),
    }


def source_jobsch(cache: dict, geo: Geo) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in KEYWORDS:
        for x in jobsch_search(kw):
            seen.setdefault(x["job_id"], x)
        time.sleep(0.3)
    picked = [x for x in seen.values()
              if is_it_job(x.get("title", ""), any(c.get("lvl_0") == 106 for c in x.get("categories") or []))]
    todo = [x for x in picked if f"jc-{x['job_id']}" not in cache]
    with ThreadPoolExecutor(4) as ex:
        for x, det in zip(todo, ex.map(lambda j: safe(jobsch_detail, j["job_id"]), todo)):
            if det:
                cache[f"jc-{x['job_id']}"] = det
    jobs = []
    for x in picked:
        jid = x["job_id"]
        d = cache.get(f"jc-{jid}", {})
        title = clean(x.get("title"))
        text = d.get("text") or x.get("preview") or ""
        grades = [g for g in (x.get("employment_grades") or []) if isinstance(g, int) and g > 0]
        pmin, pmax = (min(grades), max(grades)) if grades else parse_pensum(title)
        tmin, tmax = parse_pensum(title)
        if tmax and (not pmax or (pmin == pmax == 100 and tmax != 100)):
            pmin, pmax = tmin, tmax
        place = clean(x.get("place"))
        g = geo.lookup(d.get("zip"), place)
        url = (x.get("_links") or {}).get("detail_de", {}).get("href") or f"https://www.jobs.ch/de/stellenangebote/detail/{jid}/"
        agency = (x.get("company_segmentation") or "") == "pdl" or bool(AGENCY_RE.search(x.get("company_name") or ""))
        jobs.append(make_job(
            id=f"jc-{jid}", source="jobs.ch", url=url, apply=d.get("apply"), title=title,
            employer=x.get("company_name") or "", place=place, plz=d.get("zip") or None, canton="",
            geo=g, pmin=pmin, pmax=pmax, temporary=bool(TEMP_RE.search(title)) or None,
            published=(x.get("initial_publication_date") or x.get("publication_date") or "")[:10] or None,
            text=text, agency=agency,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 2: job-room.ch (Stellenboerse des Bundes / arbeit.swiss)
# --------------------------------------------------------------------------- #

JR = "https://www.job-room.ch/jobadservice/api/jobAdvertisements/_search"


def jobroom_search(keyword: str) -> list[dict]:
    out = []
    page = 0
    while True:
        body = json.dumps({
            "workloadPercentageMin": 0, "workloadPercentageMax": 100, "permanent": None,
            "onlineSince": 60, "displayRestricted": False, "keywords": [keyword],
            "professionCodes": [], "communalCodes": [], "cantonCodes": JOBROOM_CANTONS,
        }).encode()
        raw, headers = http(f"{JR}?page={page}&size=100&sort=date_desc", data=body,
                            headers={"Content-Type": "application/json", "Accept": "application/json"})
        batch = json.loads(raw)
        out.extend(batch)
        total = int({k.lower(): v for k, v in headers.items()}.get("x-total-count", "0") or 0)
        page += 1
        if not batch or page * 100 >= total or page >= 5:
            break
        time.sleep(0.5)
    return out


def source_jobroom(cache: dict, geo: Geo) -> list[dict]:
    seen: dict[str, dict] = {}
    for kw in KEYWORDS:
        for x in jobroom_search(kw):
            ad = x.get("jobAdvertisement", x)
            seen.setdefault(ad["id"], ad)
        time.sleep(0.4)
    jobs = []
    for jid, ad in seen.items():
        c = ad.get("jobContent", {})
        descs = c.get("jobDescriptions") or [{}]
        de = next((d for d in descs if d.get("languageIsoCode") == "de"), descs[0])
        title = clean(de.get("title"))
        desc = clean(de.get("description"))
        if not is_it_job(title, False, desc):
            continue
        comp = c.get("company") or {}
        employer = clean(comp.get("name"))
        loc = c.get("location") or {}
        coords = loc.get("coordinates") or {}
        if coords.get("lat") and coords.get("lon"):
            g = (float(coords["lat"]), float(coords["lon"]), loc.get("cantonCode") or "")
        else:
            g = geo.lookup(loc.get("postalCode"), loc.get("city"), loc.get("cantonCode"))
        emp = c.get("employment") or {}
        pmin, pmax = to_int(emp.get("workloadPercentageMin")), to_int(emp.get("workloadPercentageMax"))
        perm = emp.get("permanent")
        url = c.get("externalUrl") or f"https://www.job-room.ch/job-search/{jid}"
        if url and "utm_" in url:
            url = url.split("?")[0]
        pub = (ad.get("publication") or {}).get("startDate") or ad.get("createdTime") or ""
        agency = bool(AGENCY_RE.search(employer)) or (bool(c.get("displayCompany")) and clean((c.get("displayCompany") or {}).get("name")) != employer)
        jobs.append(make_job(
            id=f"jr-{jid}", source="job-room.ch", url=url, title=title, employer=employer,
            place=loc.get("city") or "", plz=loc.get("postalCode"), canton=loc.get("cantonCode") or "",
            geo=g, pmin=pmin, pmax=pmax,
            temporary=(None if perm is None else not perm) or (True if TEMP_RE.search(title) else None),
            published=pub[:10] or None, text=desc, agency=agency,
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 3: Kanton Aargau Stellenmarkt (Rubrik Informatik)
# --------------------------------------------------------------------------- #

AGK = "https://www.ag.ch/io/jobs-proxy"


def agk_detail(url: str) -> dict:
    text = strip_tags(get_text(url))
    i = text.find("Ihr Arbeitsumfeld")
    return {"text": text[i:i + 4000] if i >= 0 else text[:4000]}


def source_agk(cache: dict, geo: Geo) -> list[dict]:
    data = get_json(f"{AGK}/jobs")
    picked = []
    for x in data.get("jobs", []):
        a = x.get("attributes", {})
        rub = " ".join(a.get("10", []) + a.get("15", []) + a.get("30", []))
        art = " ".join(a.get("50", []))
        title = x.get("title", "")
        if "Lehrstellen" in art or "Praktika" in art or EXCLUDE_RE.search(title):
            continue
        if "Informatik" in rub or is_it_job(title) and re.search(r"informati|\bICT\b|\bIT\b", title, re.I):
            picked.append(x)
    todo = [x for x in picked if f"agk-{x['id']}" not in cache]
    with ThreadPoolExecutor(3) as ex:
        for x, det in zip(todo, ex.map(lambda j: safe(agk_detail, j["links"]["directlink"]), todo)):
            if det:
                cache[f"agk-{x['id']}"] = det
    jobs = []
    for x in picked:
        a = x.get("attributes", {})
        d = cache.get(f"agk-{x['id']}", {})
        title = clean(x.get("title"))
        sz = x.get("szas", {}) or {}
        pmin, pmax = to_int(sz.get("sza_pensum.min")), to_int(sz.get("sza_pensum.max"))
        tmin, tmax = parse_pensum(title)
        if tmax and (not pmax or pmax == 100 and tmax != 100):
            pmin, pmax = tmin, tmax
        place = (a.get("20") or [""])[0]
        befr = " ".join(a.get("70", []))
        jobs.append(make_job(
            id=f"agk-{x['id']}", source="Kanton Aargau", url=x["links"]["directlink"], title=title,
            employer="Kanton Aargau", place=place, plz=None, canton="AG", geo=geo.lookup(None, place, "AG"),
            pmin=pmin, pmax=pmax,
            temporary=("befristet" == befr.strip().lower()) if befr else None,
            published=(x.get("startDate") or "")[:10] or None, text=d.get("text", ""),
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Quelle 4: ETH Zuerich (jobs.ethz.ch)
# --------------------------------------------------------------------------- #

ETH = "https://jobs.ethz.ch"


def source_eth(cache: dict, geo: Geo) -> list[dict]:
    s = get_text(f"{ETH}/")
    jobs = []
    for li in re.findall(r'<li class="job-ad__item__wrapper".*?</li>', s, re.S):
        m = re.search(r'href="(/job/view/([^"]+))"', li)
        t = re.search(r'job-ad__item__title">\s*(.*?)\s*</h3>', li, re.S)
        if not m or not t:
            continue
        title = clean(t.group(1))
        if not is_it_job(title) or DEV_RE.search(title) and not re.search(r"system|support|client|workplace", title, re.I):
            continue
        det = clean(strip_tags((re.search(r'job-ad__item__details">(.*?)</div>', li, re.S) or [None, ""])[1]))
        comp = clean(strip_tags((re.search(r'job-ad__item__company">(.*?)</div>', li, re.S) or [None, ""])[1]))
        date, _, unit = comp.partition("|")
        pmin, pmax = parse_pensum(det)
        bits = [b.strip() for b in det.split(",")]
        place = bits[1] if len(bits) > 1 else "Zürich"
        jobs.append(make_job(
            id=f"eth-{m.group(2)}", source="ETH Zürich", url=ETH + m.group(1), title=title,
            employer=f"ETH Zürich – {unit.strip()}" if unit.strip() else "ETH Zürich",
            place=place, plz=None, canton="ZH", geo=geo.lookup(None, place, "ZH"),
            pmin=pmin, pmax=pmax, temporary=bool(TEMP_RE.search(det)) or None,
            published=parse_date_ch(date), text=f"{det} · {unit.strip()}",
        ))
    return jobs


# --------------------------------------------------------------------------- #
# Zusammenfuehren
# --------------------------------------------------------------------------- #

def make_job(*, id, source, url, title, employer, place, plz, canton, geo, pmin, pmax,
             temporary, published, text, apply=None, agency=False) -> dict:
    if pmin is not None and pmax is not None and pmin > pmax:
        pmin, pmax = pmax, pmin
    if pmax is not None and pmin is None:
        pmin = pmax
    score, hits = match_of(title, text)
    return {
        "id": id, "source": source, "url": url, "apply": apply,
        "title": title, "employer": clean(employer), "place": clean(place), "plz": plz,
        "canton": (geo[2] if geo and geo[2] else canton) or canton,
        "lat": round(geo[0], 4) if geo else None, "lon": round(geo[1], 4) if geo else None,
        "pmin": pmin, "pmax": pmax, "temporary": temporary,
        "role": role_of(title), "level": level_of(title),
        "topics": classify(TOPICS, title),
        "topics_text": [t for t in classify(TOPICS, "", text) if t not in classify(TOPICS, title)],
        "score": score, "skills": hits, "agency": bool(agency),
        "published": published, "text": snippet(text or ""),
    }


def norm_key(j: dict) -> str:
    t = re.sub(r"\(.*?\)|\d+\s*-?\s*\d*\s*%|[^a-zäöü]+", " ", j["title"].lower())
    t = re.sub(r"\b(m|w|d|f|h|in|mwd|wmd|fmd)\b", " ", t)
    return " ".join(t.split()) + "|" + re.sub(r"[^a-z]", "", (j.get("employer") or "").lower())[:10]


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"  ! {fn.__name__}{args[:1]}: {e}", file=sys.stderr)
        return None


def main() -> int:
    geo = Geo(PLZ_FILE)
    previous = {}
    prev_meta = {}
    if OUT_FILE.exists():
        try:
            prev = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            previous = {j["id"]: j for j in prev.get("jobs", [])}
            prev_meta = {s["name"]: s for s in prev.get("sources", [])}
        except Exception:  # noqa: BLE001
            pass
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}

    # Reihenfolge = Prioritaet beim Entfernen von Doppelten (Arbeitgeber-Quellen zuerst)
    sources = [
        ("Kanton Aargau", "agk", source_agk, "https://www.ag.ch/stellen"),
        ("ETH Zürich", "eth", source_eth, "https://jobs.ethz.ch"),
        ("jobs.ch", "jc", source_jobsch, "https://www.jobs.ch"),
        ("job-room.ch", "jr", source_jobroom, "https://www.job-room.ch"),
    ]
    only = set(sys.argv[1:])
    all_jobs: list[dict] = []
    meta = []
    for name, prefix, fn, home in sources:
        t0 = time.time()
        try:
            if only and prefix not in only:
                raise RuntimeError("übersprungen")
            jobs = fn(cache, geo)
            if not jobs and any(k.startswith(prefix + "-") for k in previous):
                raise RuntimeError("Quelle lieferte 0 Stellen (vermutlich Seite geändert)")
            ok, err = True, None
        except Exception as e:  # noqa: BLE001
            if str(e) != "übersprungen":
                traceback.print_exc()
            jobs = [j for k, j in previous.items() if k.startswith(prefix + "-")]
            ok, err = False, str(e)[:300]
        print(f"{name}: {len(jobs)} Stellen ({time.time() - t0:.0f}s){'' if ok else ' FEHLER: ' + err}")
        meta.append({"name": name, "url": home, "count": len(jobs), "ok": ok, "error": err,
                     "last_ok": NOW.isoformat(timespec="minutes") if ok else prev_meta.get(name, {}).get("last_ok")})
        all_jobs.extend(jobs)

    out, keys = [], set()
    for j in all_jobs:
        k = norm_key(j)
        if k in keys:
            continue
        keys.add(k)
        old = previous.get(j["id"])
        if old:
            j["first_seen"] = old.get("first_seen", TODAY)
        elif not previous:
            j["first_seen"] = j.get("published") if j.get("published") and j["published"] <= TODAY else TODAY
        else:
            j["first_seen"] = TODAY
        out.append(j)

    out.sort(key=lambda j: (j.get("first_seen") or "", j.get("published") or ""), reverse=True)

    live = {j["id"] for j in all_jobs}
    cache = {k: v for k, v in cache.items() if k in live}
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps({
        "updated": NOW.isoformat(timespec="minutes"),
        "sources": meta,
        "jobs": out,
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    if not PLZ_OUT_FILE.exists():
        plz = {p: [round(v[0], 3), round(v[1], 3)] for p, v in geo.by_plz.items()}
        PLZ_OUT_FILE.write_text(json.dumps(plz, separators=(",", ":")), encoding="utf-8")

    print(f"Total: {len(out)} Stellen -> {OUT_FILE}")
    return 0 if any(m["ok"] for m in meta) else 1


if __name__ == "__main__":
    sys.exit(main())
