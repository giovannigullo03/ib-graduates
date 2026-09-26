"""Normalise LinkedIn profile exports into data/raw/linkedin_profiles.json.

Drop any number of CSV exports into `data/linkedin/` and run this. Files are
merged and de-duplicated by LinkedIn member id, keeping the *richest* row for
each person, so a later full export silently upgrades an earlier stub and
re-running after a new batch arrives is always safe.

Unlike the other collectors this one does not hit an API: the exports are
produced outside the pipeline. What this module owns is the messy part —
258 flat columns of `organization_7_location`-style data turned into the same
career/education shape the rest of the pipeline already speaks.

Privacy: the exports carry e-mail addresses, phone numbers and birthdays.
Those columns are dropped here and never reach data/raw/ or the public site;
see PRIVATE_COLUMNS below.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

from common import DATA, RAW, strip_accents, _matches_balseiro_text

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

INPUT_DIR = DATA / "linkedin"
OUT_FILE = RAW / "linkedin_profiles.json"

MAX_POSITIONS = 10   # organization_1 .. organization_10
MAX_EDUCATION = 3    # education_1 .. education_3

# Columns we refuse to carry past this module, whatever the export contains.
PRIVATE_COLUMNS = re.compile(
    r"^(email|phone|third_party_email|birthday|connected_at|.*_email_.*)", re.I)

THIS_YEAR = dt.date.today().year

# --------------------------------------------------------------------------- #
# Place parsing
# --------------------------------------------------------------------------- #
# LinkedIn appends the work modality to a location: "Trencin, Eslovaquia · Presencial"
_MODALITY = re.compile(r"\s*·.*$")
# LinkedIn decorates a city with a metro-area wrapper in either language:
# "Buenos Aires y alrededores", "Amsterdam Area", "Greater Chicago Area",
# "Área metropolitana de Santiago". Left in place these stop the city from
# being recognised, and "Amsterdam Area" geocoded to the Philippines.
_AREA_SUFFIX = re.compile(
    r"\s+(y alrededores|e alrededores|and surrounding area|"
    r"(bay|metropolitan|metro|capital|greater)\s+area|metropolitana|area)\s*$", re.I)
_AREA_PREFIX = re.compile(
    r"^(greater|gran|area metropolitana de|\u00e1rea metropolitana de|"
    r"region(al)? de|regi\u00f3n de)\s+", re.I)
_REMOTE = {"en remoto", "remote", "remoto", "worldwide", "hibrido", "presencial",
           "a distancia", "teletrabajo", "global", "america latina", "latin america",
           "europa", "europe", "head office", "on-site", "hybrid"}

# The exports are localised, so countries arrive in Spanish far more often than
# in English. Everything downstream (and site/data/alumni.json) is English.
COUNTRY_ES_EN = {
    "argentina": ("Argentina", "AR"), "estados unidos": ("United States", "US"),
    "eeuu": ("United States", "US"), "usa": ("United States", "US"),
    "united states": ("United States", "US"), "ee. uu.": ("United States", "US"),
    "espana": ("Spain", "ES"), "spain": ("Spain", "ES"),
    "alemania": ("Germany", "DE"), "germany": ("Germany", "DE"),
    "francia": ("France", "FR"), "france": ("France", "FR"),
    "italia": ("Italy", "IT"), "italy": ("Italy", "IT"),
    "brasil": ("Brazil", "BR"), "brazil": ("Brazil", "BR"),
    "chile": ("Chile", "CL"), "uruguay": ("Uruguay", "UY"),
    "peru": ("Peru", "PE"), "colombia": ("Colombia", "CO"),
    "bolivia": ("Bolivia", "BO"), "paraguay": ("Paraguay", "PY"),
    "ecuador": ("Ecuador", "EC"), "venezuela": ("Venezuela", "VE"),
    "mexico": ("Mexico", "MX"), "cuba": ("Cuba", "CU"),
    "guatemala": ("Guatemala", "GT"), "costa rica": ("Costa Rica", "CR"),
    "canada": ("Canada", "CA"), "reino unido": ("United Kingdom", "GB"),
    "united kingdom": ("United Kingdom", "GB"), "inglaterra": ("United Kingdom", "GB"),
    "escocia": ("United Kingdom", "GB"), "gales": ("United Kingdom", "GB"),
    "irlanda": ("Ireland", "IE"), "ireland": ("Ireland", "IE"),
    "paises bajos": ("Netherlands", "NL"), "holanda": ("Netherlands", "NL"),
    "netherlands": ("Netherlands", "NL"), "belgica": ("Belgium", "BE"),
    "belgium": ("Belgium", "BE"), "suiza": ("Switzerland", "CH"),
    "switzerland": ("Switzerland", "CH"), "austria": ("Austria", "AT"),
    "suecia": ("Sweden", "SE"), "sweden": ("Sweden", "SE"),
    "noruega": ("Norway", "NO"), "dinamarca": ("Denmark", "DK"),
    "finlandia": ("Finland", "FI"), "portugal": ("Portugal", "PT"),
    "polonia": ("Poland", "PL"), "republica checa": ("Czechia", "CZ"),
    "chequia": ("Czechia", "CZ"), "eslovaquia": ("Slovakia", "SK"),
    "hungria": ("Hungary", "HU"), "grecia": ("Greece", "GR"),
    "rusia": ("Russia", "RU"), "israel": ("Israel", "IL"),
    "japon": ("Japan", "JP"), "japan": ("Japan", "JP"),
    "china": ("China", "CN"), "corea del sur": ("South Korea", "KR"),
    "india": ("India", "IN"), "australia": ("Australia", "AU"),
    "nueva zelanda": ("New Zealand", "NZ"), "sudafrica": ("South Africa", "ZA"),
    "south africa": ("South Africa", "ZA"), "singapur": ("Singapore", "SG"),
    "emiratos arabes unidos": ("United Arab Emirates", "AE"),
    "arabia saudita": ("Saudi Arabia", "SA"), "turquia": ("Turkey", "TR"),
    "luxemburgo": ("Luxembourg", "LU"),
}

# A bare Argentine place with no country attached is extremely common in these
# exports ("Bariloche", "CABA", "Neuquén"). Treat those as Argentina.
AR_PLACES = {
    "caba", "capital federal", "ciudad autonoma de buenos aires",
    "ciudad de buenos aires", "buenos aires", "provincia de buenos aires",
    "bariloche", "san carlos de bariloche", "s.c. de bariloche", "sc de bariloche",
    "cordoba", "rosario", "mendoza", "neuquen", "rio negro", "santa fe",
    "la plata", "mar del plata", "tucuman", "salta", "jujuy", "san juan",
    "san luis", "entre rios", "corrientes", "chaco", "formosa", "misiones",
    "catamarca", "la rioja", "santiago del estero", "chubut", "santa cruz",
    "tierra del fuego", "la pampa", "rio cuarto", "rio tercero", "embalse",
    "comodoro rivadavia", "bahia blanca", "san nicolas", "campana", "zarate",
    "ezeiza", "pilar", "tandil", "san rafael", "villa maria", "parana",
}


# Spanish/other exonyms and bare city names that carry their country with them.
# These turn up constantly once "y alrededores" is stripped ("París", "Múnich").
CITY_COUNTRY = {
    "paris": ("Paris", "France", "FR"), "marsella": ("Marseille", "France", "FR"),
    "grenoble": ("Grenoble", "France", "FR"), "toulouse": ("Toulouse", "France", "FR"),
    "lyon": ("Lyon", "France", "FR"), "burdeos": ("Bordeaux", "France", "FR"),
    "estrasburgo": ("Strasbourg", "France", "FR"), "niza": ("Nice", "France", "FR"),
    "munich": ("Munich", "Germany", "DE"), "berlin": ("Berlin", "Germany", "DE"),
    "hamburgo": ("Hamburg", "Germany", "DE"), "colonia": ("Cologne", "Germany", "DE"),
    "fráncfort": ("Frankfurt", "Germany", "DE"), "francfort": ("Frankfurt", "Germany", "DE"),
    "leverkusen": ("Leverkusen", "Germany", "DE"), "karlsruhe": ("Karlsruhe", "Germany", "DE"),
    "stuttgart": ("Stuttgart", "Germany", "DE"), "dresde": ("Dresden", "Germany", "DE"),
    "aquisgran": ("Aachen", "Germany", "DE"), "gotinga": ("Göttingen", "Germany", "DE"),
    "londres": ("London", "United Kingdom", "GB"), "edimburgo": ("Edinburgh", "United Kingdom", "GB"),
    "mancheste": ("Manchester", "United Kingdom", "GB"), "oxford": ("Oxford", "United Kingdom", "GB"),
    "cambridge": ("Cambridge", "United Kingdom", "GB"), "glasgow": ("Glasgow", "United Kingdom", "GB"),
    "roma": ("Rome", "Italy", "IT"), "milan": ("Milan", "Italy", "IT"),
    "turin": ("Turin", "Italy", "IT"), "trieste": ("Trieste", "Italy", "IT"),
    "florencia": ("Florence", "Italy", "IT"), "napoles": ("Naples", "Italy", "IT"),
    "bolonia": ("Bologna", "Italy", "IT"), "padua": ("Padua", "Italy", "IT"),
    "ginebra": ("Geneva", "Switzerland", "CH"), "zurich": ("Zurich", "Switzerland", "CH"),
    "lausana": ("Lausanne", "Switzerland", "CH"), "basilea": ("Basel", "Switzerland", "CH"),
    "berna": ("Bern", "Switzerland", "CH"), "lausanne": ("Lausanne", "Switzerland", "CH"),
    "amsterdam": ("Amsterdam", "Netherlands", "NL"), "la haya": ("The Hague", "Netherlands", "NL"),
    "rotterdam": ("Rotterdam", "Netherlands", "NL"), "utrecht": ("Utrecht", "Netherlands", "NL"),
    "eindhoven": ("Eindhoven", "Netherlands", "NL"), "delft": ("Delft", "Netherlands", "NL"),
    "bruselas": ("Brussels", "Belgium", "BE"), "amberes": ("Antwerp", "Belgium", "BE"),
    "lovaina": ("Leuven", "Belgium", "BE"), "gante": ("Ghent", "Belgium", "BE"),
    "viena": ("Vienna", "Austria", "AT"), "copenhague": ("Copenhagen", "Denmark", "DK"),
    "estocolmo": ("Stockholm", "Sweden", "SE"), "stockholm": ("Stockholm", "Sweden", "SE"),
    "gotemburgo": ("Gothenburg", "Sweden", "SE"), "oslo": ("Oslo", "Norway", "NO"),
    "helsinki": ("Helsinki", "Finland", "FI"), "lisboa": ("Lisbon", "Portugal", "PT"),
    "oporto": ("Porto", "Portugal", "PT"), "varsovia": ("Warsaw", "Poland", "PL"),
    "cracovia": ("Krakow", "Poland", "PL"), "cracow": ("Krakow", "Poland", "PL"),
    "praga": ("Prague", "Czechia", "CZ"), "budapest": ("Budapest", "Hungary", "HU"),
    "liubliana": ("Ljubljana", "Slovenia", "SI"), "ljubljana": ("Ljubljana", "Slovenia", "SI"),
    "atenas": ("Athens", "Greece", "GR"), "moscu": ("Moscow", "Russia", "RU"),
    "madrid": ("Madrid", "Spain", "ES"), "barcelona": ("Barcelona", "Spain", "ES"),
    "sevilla": ("Seville", "Spain", "ES"), "valencia": ("Valencia", "Spain", "ES"),
    "bilbao": ("Bilbao", "Spain", "ES"), "zaragoza": ("Zaragoza", "Spain", "ES"),
    "santiago de compostela": ("Santiago de Compostela", "Spain", "ES"),
    "tokio": ("Tokyo", "Japan", "JP"), "pekin": ("Beijing", "China", "CN"),
    "shanghai": ("Shanghai", "China", "CN"), "seul": ("Seoul", "South Korea", "KR"),
    "singapur": ("Singapore", "Singapore", "SG"), "sidney": ("Sydney", "Australia", "AU"),
    "melbourne": ("Melbourne", "Australia", "AU"), "toronto": ("Toronto", "Canada", "CA"),
    "montreal": ("Montreal", "Canada", "CA"), "vancouver": ("Vancouver", "Canada", "CA"),
    "ottawa": ("Ottawa", "Canada", "CA"), "ciudad de mexico": ("Mexico City", "Mexico", "MX"),
    "san pablo": ("São Paulo", "Brazil", "BR"), "sao paulo": ("São Paulo", "Brazil", "BR"),
    "rio de janeiro": ("Rio de Janeiro", "Brazil", "BR"), "brasilia": ("Brasília", "Brazil", "BR"),
    "campinas": ("Campinas", "Brazil", "BR"), "bogota": ("Bogotá", "Colombia", "CO"),
    "medellin": ("Medellín", "Colombia", "CO"), "lima": ("Lima", "Peru", "PE"),
    "montevideo": ("Montevideo", "Uruguay", "UY"), "la paz": ("La Paz", "Bolivia", "BO"),
    "quito": ("Quito", "Ecuador", "EC"), "caracas": ("Caracas", "Venezuela", "VE"),
    "asuncion": ("Asunción", "Paraguay", "PY"), "san salvador": ("San Salvador", "El Salvador", "SV"),
    "nueva york": ("New York", "United States", "US"), "new york": ("New York", "United States", "US"),
    "san francisco": ("San Francisco", "United States", "US"),
    "los angeles": ("Los Angeles", "United States", "US"), "chicago": ("Chicago", "United States", "US"),
    "boston": ("Boston", "United States", "US"), "houston": ("Houston", "United States", "US"),
    "seattle": ("Seattle", "United States", "US"), "denver": ("Denver", "United States", "US"),
    "austin": ("Austin", "United States", "US"), "gainesville": ("Gainesville", "United States", "US"),
    "filadelfia": ("Philadelphia", "United States", "US"),
    "washington": ("Washington", "United States", "US"),
    "galway": ("Galway", "Ireland", "IE"), "dublin": ("Dublin", "Ireland", "IE"),
}

# A US state or its postal code, with no country attached ("Orlando, Florida").
US_STATES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada","ohio",
    "oklahoma","oregon","pennsylvania","tennessee","texas","utah","vermont",
    "virginia","washington","wisconsin","wyoming","new york","new jersey",
    "new mexico","new hampshire","north carolina","south carolina","north dakota",
    "south dakota","rhode island","west virginia","district of columbia",
}

# The location field sometimes holds an employer, a department or a street
# address instead of a place. Geocoding those produces nonsense, so they are
# reported as "no geography" rather than as a city.
_NOT_A_PLACE = re.compile(
    r"\b(instituto|institute|universidad|university|facultad|faculty|"
    r"departamento|department|division|divisi\u00f3n|laboratorio|laboratory|"
    r"hospital|clinica|cl\u00ednica|escuela|colegio|centro at\u00f3mico|centro atomico|"
    r"central nuclear|comisi\u00f3n nacional|comision nacional|head office|"
    r"grupo de|group|s\.?a\.?s?\b|s\.?r\.?l\.?\b)\b|^av\.|^avda|^calle|^ruta\b|\d{3,}",
    re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s or "").lower()).strip(" .,")


# LinkedIn puts the work arrangement after a "·": "San Francisco, California,
# Estados Unidos · En remoto". Stripping that threw away the one field that
# says the company's address is NOT where the person is.
_REMOTE_WORDS = re.compile(r"\b(en remoto|remoto|remote|a distancia|teletrabajo)\b", re.I)
_HYBRID_WORDS = re.compile(r"\b(h[ií]brido|hybrid)\b", re.I)
_ONSITE_WORDS = re.compile(r"\b(presencial|on[- ]?site)\b", re.I)


def parse_modality(raw: str):
    """'remote' | 'hybrid' | 'onsite' | None — how the job was actually done."""
    s = (raw or "")
    tail = s.split("·", 1)[1] if "·" in s else s
    if _REMOTE_WORDS.search(tail):
        return "remote"
    if _HYBRID_WORDS.search(tail):
        return "hybrid"
    if _ONSITE_WORDS.search(tail):
        return "onsite"
    return None


def clean_place(raw: str) -> str:
    """Strip LinkedIn's modality suffix and its metro-area wrappers."""
    s = _MODALITY.sub("", raw or "").strip()
    for _ in range(3):        # "Greater Stockholm Metropolitan Area"
        before = s
        s = _AREA_PREFIX.sub("", s)
        s = _AREA_SUFFIX.sub("", s).strip(" ,.")
        if s == before:
            break
    return re.sub(r"\s+", " ", s)


def _strip_area(part: str) -> str:
    """Remove metro-area decoration from one comma segment."""
    for _ in range(3):
        before = part
        part = _AREA_PREFIX.sub("", part)
        part = _AREA_SUFFIX.sub("", part).strip(" ,.")
        if part == before:
            break
    return part


def parse_place(raw: str) -> dict:
    """"Trencin, Trencín, Eslovaquia · Presencial" -> city/country/country_code.

    Returns {} when the string carries no geography: a modality ("En remoto"),
    or an employer, department or street address that LinkedIn accepted in the
    location field. Returning a bogus city there is worse than returning
    nothing — "Amsterdam Area" used to geocode to the Philippines.
    """
    s = clean_place(raw)
    if not s or _norm(s) in _REMOTE:
        return {}

    # Balseiro and the Centro Atómico turn up as "locations"; we know where
    # they are, so answer directly instead of letting the geocoder guess.
    if _matches_balseiro_text(s):
        return {"city": "San Carlos de Bariloche", "country": "Argentina",
                "country_code": "AR"}

    parts = [_strip_area(p.strip()) for p in re.split(r"[,;]", s) if p.strip()]
    parts = [p for p in parts if p and _norm(p) not in _REMOTE]
    if not parts:
        return {}

    # 1. an explicit country anywhere in the string. Scanning every segment and
    #    every word beats only looking at the last one: the exports carry
    #    "San Nicolas Argentina" and "Haldensleben Alemania" with no comma.
    for i in range(len(parts) - 1, -1, -1):
        pieces = [p.strip() for p in re.split(r"[-()/.]", parts[i]) if p.strip()]
        for token in pieces + ([parts[i]] if parts[i] not in pieces else []):
            hit = COUNTRY_ES_EN.get(_norm(token))
            if hit:
                city = parts[i - 1] if i > 0 else None
                if not city and len(pieces) > 1:
                    city = pieces[0]
                if _norm(city or "") == _norm(token):
                    city = None
                return {"city": city or None, "country": hit[0], "country_code": hit[1]}
        # a trailing country word glued to the city: "San Nicolas Argentina"
        words = parts[i].split()
        for take in (3, 2, 1):
            if len(words) > take:
                hit = COUNTRY_ES_EN.get(_norm(" ".join(words[-take:])))
                if hit:
                    return {"city": " ".join(words[:-take]).strip(" ,.") or None,
                            "country": hit[0], "country_code": hit[1]}

    # 2. a city we know by name, in Spanish or English
    for part in parts:
        hit = CITY_COUNTRY.get(_norm(part))
        if hit:
            return {"city": hit[0], "country": hit[1], "country_code": hit[2]}

    # 3. a US state with the country left off ("Orlando, Florida")
    for i, part in enumerate(parts):
        if _norm(part) in US_STATES:
            city = parts[i - 1] if i > 0 else None
            return {"city": city, "country": "United States", "country_code": "US"}

    # 4. a bare Argentine place
    for part in parts:
        if _norm(part) in AR_PLACES:
            return {"city": part, "country": "Argentina", "country_code": "AR"}
    for part in parts:                       # "Buenos Aires Florencio Varela"
        words = _norm(part).split()
        for size in (3, 2):
            for j in range(len(words) - size + 1):
                if " ".join(words[j:j + size]) in AR_PLACES:
                    return {"city": part, "country": "Argentina", "country_code": "AR"}

    # 5. not a place at all — an employer, a department, an address
    if _NOT_A_PLACE.search(s):
        return {}

    # 6. unknown but plausible: hand the raw city to the geocoder, invent no country
    return {"city": parts[0], "country": None, "country_code": None}


# --------------------------------------------------------------------------- #
# Field parsing
# --------------------------------------------------------------------------- #
def parse_year(raw: str):
    """"2006.02" / "2006" / "Jun 2006" -> 2006 (None when unusable)."""
    m = re.search(r"(19|20)\d{2}", raw or "")
    if not m:
        return None
    y = int(m.group(0))
    return y if 1940 <= y <= THIS_YEAR + 12 else None


def parse_skills(raw: str) -> list[str]:
    """"Python : null,Rayos X : 3" -> ["Python", "Rayos X"] (endorsements dropped)."""
    out = []
    for chunk in (raw or "").split(","):
        name = chunk.rsplit(" : ", 1)[0].strip() if " : " in chunk else chunk.strip()
        if name and name.lower() != "null":
            out.append(re.sub(r"\s+", " ", name))
    seen, uniq = set(), []
    for s in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)
    return uniq


# LinkedIn's "current company" is free text, and people routinely type their
# *field* there instead of an employer. Those values would land on the map as
# if they were institutions, so they are dropped.
NOT_A_COMPANY = {
    "fisica", "física", "physics", "ingenieria", "ingeniería", "engineering",
    "telecomunicaciones", "telecommunications", "investigacion", "investigación",
    "research", "ciencia", "science", "educacion", "educación", "education",
    "docencia", "estudiante", "student", "desempleado", "unemployed",
    "matematica", "matemática", "mathematics", "quimica", "química", "chemistry",
    "tecnologia", "tecnología", "technology", "energia", "energía", "energy",
    "n/a", "na", "none", "-",
}


def is_real_company(name: str) -> bool:
    return bool(name) and _norm(name) not in NOT_A_COMPANY


def parse_languages(raw: str) -> list[str]:
    return [l.strip() for l in (raw or "").split(",") if l.strip()]


def parse_positions(row: dict) -> list[dict]:
    """organization_1..10 -> career stops in the pipeline's shape."""
    stops = []
    for i in range(1, MAX_POSITIONS + 1):
        org = (row.get(f"organization_{i}") or "").strip()
        if not org:
            continue
        start = parse_year(row.get(f"organization_start_{i}"))
        end = parse_year(row.get(f"organization_end_{i}"))
        raw_loc = row.get(f"organization_location_{i}") or ""
        place = parse_place(raw_loc)
        modality = parse_modality(raw_loc)
        is_default = (row.get(f"position_is_default_{i}") or "").lower() == "true"
        stops.append({
            "institution": org,
            "title": (row.get(f"organization_title_{i}") or "").strip() or None,
            "city": place.get("city"),
            "country": place.get("country"),
            "country_code": place.get("country_code"),
            "start": str(start) if start else None,
            "end": str(end) if end else None,
            # "current" = still running (no end date), which is also how the
            # ORCID/INSPIRE career entries are tagged.
            "current": bool(not end and start),
            "modality": modality,
            "is_default": is_default,
            "url": (row.get(f"organization_url_{i}") or "").strip() or None,
        })
    stops.sort(key=lambda s: s["start"] or "0")
    return stops


def parse_education(row: dict) -> list[dict]:
    out = []
    for i in range(1, MAX_EDUCATION + 1):
        school = (row.get(f"education_{i}") or "").strip()
        if not school:
            continue
        degree = (row.get(f"education_degree_{i}") or "").strip() or None
        desc = (row.get(f"education_description_{i}") or "").strip() or None
        out.append({
            "school": school,
            "degree": degree,
            "field": (row.get(f"education_fos_{i}") or "").strip() or None,
            "description": desc,
            "start": parse_year(row.get(f"education_start_{i}")),
            "end": parse_year(row.get(f"education_end_{i}")),
            "balseiro": _matches_balseiro_text(
                " ".join(x for x in (school, degree, desc) if x)),
        })
    return out


# --------------------------------------------------------------------------- #
# Row -> profile
# --------------------------------------------------------------------------- #
def _row_richness(row: dict) -> int:
    """How much real content a row carries — used to pick a winner when the
    same person shows up in several exports (a stub in the 'pending' list and
    a full profile in a later download)."""
    return sum(1 for k, v in row.items()
               if k and v and str(v).strip() and not PRIVATE_COLUMNS.match(k))


def normalise(row: dict) -> dict | None:
    name = (row.get("full_name") or row.get("original_full_name") or "").strip()
    if not name:
        return None

    education = parse_education(row)
    career = parse_positions(row)
    headline = (row.get("headline") or "").strip()
    summary = (row.get("summary") or "").strip()

    ib_edu = [e for e in education if e["balseiro"]]
    ib_work = [c for c in career if _matches_balseiro_text(c["institution"] or "")]
    if ib_edu:
        evidence = "education"
    elif ib_work:
        evidence = "employment"
    elif _matches_balseiro_text(f"{headline} {summary}"):
        evidence = "text"
    else:
        evidence = None

    # Graduation year = when the Balseiro degree ended. An end date in the
    # future means they are still studying there — no grad year yet.
    grad_year, student, program, degrees = None, False, None, []
    for e in ib_edu:
        if e["degree"]:
            degrees.append(e["degree"])
        if e["end"] and e["end"] > THIS_YEAR:
            student = True
        elif e["end"] and (grad_year is None or e["end"] > grad_year):
            grad_year = e["end"]
    if ib_edu:
        program = ib_edu[0]["degree"] or ib_edu[0]["field"]

    # Where someone studied before Balseiro is the true start of their
    # trajectory, and it is the one leg the academic sources never record.
    # Emit it in the same shape as a job so it geocodes and draws like one.
    edu_stops = []
    for e in education:
        if e["balseiro"] or not e["school"]:
            continue
        edu_stops.append({
            "institution": e["school"],
            "title": e["degree"] or e["field"],
            "city": None, "country": None, "country_code": None,
            "start": str(e["start"]) if e["start"] else None,
            "end": str(e["end"]) if e["end"] else None,
            "current": False,
            "kind": "education",
        })

    place = parse_place(row.get("location_name") or "")

    # A remote job's address is the company's, not the person's. Someone in
    # Bariloche working remotely for a company in San Francisco was being drawn
    # in California. Where LinkedIn says the work was remote, the employer's
    # city is discarded and replaced by where the person actually is: their own
    # profile location first, and failing that the nearest job they did say was
    # on-site. Positions that say nothing are left alone — most people never
    # fill the field in, and assuming remote would be far more wrong than
    # assuming the default.
    # People split one stint at an employer across several rows (a promotion,
    # a title change) and annotate only one of them. Agustín Bernardo has two
    # Superhuman rows: one says "En remoto", the other just "San Francisco".
    # Merging them later mixed a San Francisco city with an Argentine country.
    # If any row at an employer says remote, the whole stint there was.
    remote_orgs = {_norm(c["institution"]) for c in career
                   if c.get("modality") == "remote" and c.get("institution")}
    for c in career:
        if c.get("institution") and _norm(c["institution"]) in remote_orgs:
            c["modality"] = "remote"

    onsite = next((c for c in reversed(career)
                   if c.get("modality") in ("onsite", "hybrid") and c.get("country")), None)
    home = place if place.get("country") or place.get("city") else None
    fallback = home or ({"city": onsite["city"], "country": onsite["country"],
                         "country_code": onsite["country_code"]} if onsite else None)
    n_remote = 0
    for c in career:
        if c.get("modality") != "remote":
            continue
        n_remote += 1
        if fallback:
            c["city"] = fallback.get("city")
            c["country"] = fallback.get("country")
            c["country_code"] = fallback.get("country_code")
        else:
            # nothing to put in its place: better unknown than wrong
            c["city"] = c["country"] = c["country_code"] = None
    # The profile's own location field is a home town; the current job's
    # location is the better "where do they work" signal when both exist.
    default_job = next((c for c in career if c["is_default"]), None)
    current = default_job or next((c for c in reversed(career) if c["current"]), None)

    # The position list holds the real organisation name ("Nucleoeléctrica
    # Argentina S.A."); `current_company` is a free-text field that may be
    # shorter, or not a company at all. Prefer the position, fall back to the
    # free text only when it looks like an actual employer.
    cc = (row.get("current_company") or "").strip()
    current_employer = (current or {}).get("institution") or (cc if is_real_company(cc) else None)

    asof = parse_year(row.get("result_created_at") or row.get("mini_profile_actual_at")
                      or row.get("current_company_actual_at") or "")

    return {
        "name": name,
        "public_id": (row.get("public_id") or "").strip() or None,
        "member_id": (row.get("member_id") or "").strip() or None,
        "profile_url": (row.get("profile_url") or "").strip() or None,
        "headline": headline or None,
        "summary": summary or None,
        "location_name": clean_place(row.get("location_name") or "") or None,
        "city": place.get("city"),
        "country": place.get("country"),
        "country_code": place.get("country_code"),
        "current_employer": current_employer,
        "current_role": (row.get("current_company_position") or "").strip()
                        or (current or {}).get("title"),
        "current_city": (current or {}).get("city") or place.get("city"),
        "current_country": (current or {}).get("country") or place.get("country"),
        "current_country_code": (current or {}).get("country_code") or place.get("country_code"),
        "career": career,
        "education_stops": edu_stops,
        "education": education,
        "degrees": degrees,
        "program": program,
        "grad_year": grad_year,
        "student": student,
        "skills": parse_skills(row.get("skills") or "")[:25],
        "languages": parse_languages(row.get("languages") or ""),
        "balseiro_evidence": evidence,
        "remote_positions": n_remote,
        "asof": asof,
    }


# --------------------------------------------------------------------------- #
def _read_csv(path: Path) -> list[dict]:
    """Read one export. LinkedIn tools emit ';'-quoted CSV; sniff anyway."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
        except csv.Error:
            delim = ";"
        return list(csv.DictReader(fh, delimiter=delim))


def collect() -> list[dict]:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in INPUT_DIR.glob("*.csv") if p.is_file())
    if not files:
        print(f"no CSV exports in data/linkedin/ — skipping")
        return []

    best: dict[str, dict] = {}
    skipped_schema = 0
    for path in files:
        rows = _read_csv(path)
        usable = [r for r in rows if r.get("full_name") and r.get("profile_url")]
        if not usable:
            # e.g. links_perfiles.csv (Nombre;Titular;LinkedIn) — a target list,
            # not a profile export. Nothing to normalise.
            skipped_schema += 1
            print(f"  {path.name}: no profile rows (wrong schema?) — skipped")
            continue
        for row in usable:
            ident = (row.get("member_id") or row.get("public_id")
                     or row.get("profile_url") or "").strip()
            if not ident:
                continue
            if ident not in best or _row_richness(row) > _row_richness(best[ident]):
                best[ident] = row
        print(f"  {path.name}: {len(usable)} profile rows")

    profiles = [p for p in (normalise(r) for r in best.values()) if p]
    profiles.sort(key=lambda p: p["name"].lower())

    OUT_FILE.write_text(json.dumps(profiles, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    n_edu = sum(1 for p in profiles if p["balseiro_evidence"] == "education")
    n_other = sum(1 for p in profiles if p["balseiro_evidence"] in ("employment", "text"))
    print(f"\n{len(files)} file(s), {len(best)} distinct people"
          + (f" ({skipped_schema} skipped)" if skipped_schema else ""))
    print(f"  Balseiro in education : {n_edu}")
    print(f"  Balseiro elsewhere    : {n_other}")
    print(f"  no Balseiro evidence  : {len(profiles) - n_edu - n_other}")
    print(f"  with a usable location: {sum(1 for p in profiles if p['current_country'] or p['current_city'])}")
    print(f"  with career history   : {sum(1 for p in profiles if p['career'])}"
          f"  ({sum(len(p['career']) for p in profiles)} positions total)")
    print(f"  still studying at IB  : {sum(1 for p in profiles if p['student'])}")
    print(f"wrote data/raw/linkedin_profiles.json")
    return profiles


if __name__ == "__main__":
    collect()
