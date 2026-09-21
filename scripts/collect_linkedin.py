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
# "Buenos Aires y alrededores" / "San Francisco Bay Area"
_AREA_SUFFIX = re.compile(
    r"\s+(y alrededores|and surrounding area|bay area|metropolitan area|"
    r"metropolitana)\s*$", re.I)
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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s or "").lower()).strip(" .,")


def clean_place(raw: str) -> str:
    """Strip LinkedIn's modality suffix and 'y alrededores' noise."""
    s = _MODALITY.sub("", raw or "").strip()
    s = _AREA_SUFFIX.sub("", s).strip(" ,.")
    return re.sub(r"\s+", " ", s)


def parse_place(raw: str) -> dict:
    """"Trencin, Trencín, Eslovaquia · Presencial" -> city/country/country_code.

    Returns {} when the string carries no geography (a modality like "En remoto",
    or an org name LinkedIn stuffed into the location field).
    """
    s = clean_place(raw)
    if not s or _norm(s) in _REMOTE:
        return {}
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return {}

    # Walk from the right: the country is normally the last segment, but rows
    # like "Centurion - South Africa" or "IL (USA)" hide it inside one.
    for i in range(len(parts) - 1, -1, -1):
        pieces = [p for p in re.split(r"[-()/]", parts[i]) if p.strip()]
        for token in pieces:
            hit = COUNTRY_ES_EN.get(_norm(token))
            if not hit:
                continue
            if i > 0:
                city = parts[i - 1]
            elif len(pieces) > 1:
                city = pieces[0].strip()
            else:
                city = None
            return {"city": city or None, "country": hit[0], "country_code": hit[1]}

    if any(_norm(p) in AR_PLACES for p in parts):
        city = next(p for p in parts if _norm(p) in AR_PLACES)
        return {"city": city, "country": "Argentina", "country_code": "AR"}

    # No country recognised: keep the string as a free-text place for the
    # geocoder to try, but do not invent a country.
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
        place = parse_place(row.get(f"organization_location_{i}") or "")
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

    place = parse_place(row.get("location_name") or "")
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
        "education": education,
        "degrees": degrees,
        "program": program,
        "grad_year": grad_year,
        "student": student,
        "skills": parse_skills(row.get("skills") or "")[:25],
        "languages": parse_languages(row.get("languages") or ""),
        "balseiro_evidence": evidence,
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
