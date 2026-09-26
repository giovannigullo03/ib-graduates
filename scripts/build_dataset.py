"""Merge every source into a single site/data/alumni.json for the website.

Steps
-----
1. Load the raw per-source files (Wikidata, ORCID, Wikipedia, OpenAlex, and
   the optional RICABIB thesis harvest) + hand-editable CSVs
   (data/manual_alumni.csv, data/review_candidates.csv) and data/blocklist.txt.
2. Merge records that refer to the same person (by ORCID iD, then by a
   normalised name key).
3. OpenAlex: enrich matched people with publication stats / research concepts,
   add high-confidence ORCID-less authors, and (re)write the review queue.
4. Resolve each person's *current* location (institution -> lat/lon) from
   coordinates already in Wikidata / OpenAlex, else geocoding "<org>, <city>,
   <country>" via Nominatim (cached in data/geocode_cache.json).
5. Fold employer spelling variants onto one canonical name per institution
   (every source names them differently), classify research field + employer
   sector, tag confidence.
6. Write site/data/alumni.json (list + meta) — the only file the site loads.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import json
import re

from common import (DATA, SITE_DATA, ROOT, geocode, geocode_many, name_key, strip_accents,
                    ror_lookup,
                    initial_key, is_initials_form)
import common
import collect_openalex

RAW_FILES = {
    "wikidata": DATA / "raw" / "wikidata_alumni.json",
    "orcid": DATA / "raw" / "orcid_alumni.json",
    "wikipedia": DATA / "raw" / "wikipedia_alumni.json",
    "openalex": DATA / "raw" / "openalex_authors.json",
    "ricabib": DATA / "raw" / "ricabib_theses.json",
    "inspire": DATA / "raw" / "inspire_authors.json",
    "ads": DATA / "raw" / "ads_authors.json",
    "lens": DATA / "raw" / "lens_inventors.json",
    "linkedin": DATA / "raw" / "linkedin_profiles.json",
}
# LinkedIn profile URLs identify a private individual's account. They stay out
# of the published dataset unless you deliberately turn them on.
PUBLISH_LINKEDIN_URLS = False
# Lens applicants specific enough that a new inventor is likely an alumnus:
LENS_DISCOVERY_APPLICANTS = ("INVAP", "Instituto Balseiro", "Centro Atómico Bariloche")
MANUAL_CSV = DATA / "manual_alumni.csv"
FOUND_CSV = DATA / "found_alumni.csv"   # auto-researched locations for previously-unmatched graduates
REVIEW_CSV = DATA / "review_candidates.csv"
BLOCKLIST = DATA / "blocklist.txt"
INSTITUTIONS_CSV = DATA / "institutions.csv"  # hand-placed org -> city/country

# OpenAlex review_score thresholds (see collect_openalex._score):
OA_AUTO_KEEP = 5.5      # >= this: added straight to the map, tagged 'openalex'
OA_REVIEW_MIN = 3.5     # [MIN, AUTO_KEEP): written to review_candidates.csv

COUNTRY_BY_CODE = {
    "AR": "Argentina", "US": "United States", "BR": "Brazil", "DE": "Germany",
    "ES": "Spain", "FR": "France", "GB": "United Kingdom", "UK": "United Kingdom",
    "IT": "Italy", "CH": "Switzerland", "NL": "Netherlands", "CA": "Canada",
    "MX": "Mexico", "CL": "Chile", "SE": "Sweden", "BE": "Belgium", "AT": "Austria",
    "AU": "Australia", "JP": "Japan", "CN": "China", "PT": "Portugal", "DK": "Denmark",
    "NO": "Norway", "FI": "Finland", "IL": "Israel", "IN": "India", "PL": "Poland",
    "CZ": "Czechia", "CO": "Colombia", "UY": "Uruguay", "PE": "Peru", "KR": "South Korea",
    "IE": "Ireland", "NZ": "New Zealand", "SG": "Singapore", "ZA": "South Africa",
}


# Strings people put in an employer field that name no institution at all.
# ROR will happily match "Family Business" to a Glasgow trade body.
NOT_AN_INSTITUTION = {
    "self employed", "selfemployed", "freelance", "freelancer", "autonomo",
    "independiente", "profesional independiente", "family business",
    "emprendimiento propio", "proyecto personal", "particular", "varios",
    "none", "n a", "home", "casa", "remoto", "remote",
}

# Extra ISO codes so a geocoded country can be resolved by code rather than by
# whatever language Nominatim answered in.
COUNTRY_BY_CODE.update({
    "SK": "Slovakia", "SI": "Slovenia", "HU": "Hungary", "HR": "Croatia",
    "GR": "Greece", "TR": "Turkey", "RU": "Russia", "UA": "Ukraine",
    "RO": "Romania", "BG": "Bulgaria", "RS": "Serbia", "EE": "Estonia",
    "LV": "Latvia", "LT": "Lithuania", "IS": "Iceland", "LU": "Luxembourg",
    "BO": "Bolivia", "PY": "Paraguay", "EC": "Ecuador", "VE": "Venezuela",
    "CR": "Costa Rica", "PA": "Panama", "GT": "Guatemala", "CU": "Cuba",
    "SV": "El Salvador", "DO": "Dominican Republic", "HN": "Honduras",
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "EG": "Egypt",
    "MA": "Morocco", "TN": "Tunisia", "NG": "Nigeria", "KE": "Kenya",
    "MY": "Malaysia", "PH": "Philippines", "TH": "Thailand", "ID": "Indonesia",
    "VN": "Vietnam", "TW": "Taiwan", "HK": "Hong Kong", "PK": "Pakistan",
    "BD": "Bangladesh", "IR": "Iran", "QA": "Qatar", "JO": "Jordan",
})

# Nominatim answers in the local language, so the same country arrives as
# "Germany" from one lookup and "Deutschland" from the next — which splits the
# country facet and the flow map in two. Everything is normalised to English:
# by ISO code where we have one, else by this table.
COUNTRY_ALIASES = {
    "deutschland": "Germany", "osterreich": "Austria", "sverige": "Sweden",
    "suomi": "Finland", "suomi / finland": "Finland", "norge": "Norway",
    "danmark": "Denmark", "island": "Iceland", "espana": "Spain",
    "italia": "Italy", "schweiz": "Switzerland", "suisse": "Switzerland",
    "svizzera": "Switzerland", "belgie": "Belgium", "belgique": "Belgium",
    "nederland": "Netherlands", "the netherlands": "Netherlands",
    "letzebuerg": "Luxembourg", "luxemburg": "Luxembourg",
    "slovenija": "Slovenia", "slovensko": "Slovakia", "cesko": "Czechia",
    "polska": "Poland", "magyarorszag": "Hungary", "hrvatska": "Croatia",
    "brasil": "Brazil", "mexico": "Mexico", "panama": "Panama",
    "peru": "Peru", "paraguay / paraguai": "Paraguay",
    "turkiye": "Turkey", "ellada": "Greece",
    # non-Latin scripts cannot be accent-folded, so they are keyed verbatim
    "日本": "Japan", "中国": "China", "대한민국": "South Korea",
    "ישראל": "Israel", "भारत": "India", "россия": "Russia",
    "україна": "Ukraine", "ελλάδα": "Greece",
    "السعودية": "Saudi Arabia", "الإمارات العربية المتحدة": "United Arab Emirates",
    "مصر": "Egypt", "المغرب": "Morocco", "ليبيا": "Libya",
    "հայաստան": "Armenia", "नेपाल": "Nepal", "ไทย": "Thailand",
    "საქართველო": "Georgia", "қазақстан": "Kazakhstan",
}


def _country_en(name, code=None):
    """Canonical English name for a country, from its ISO code where possible."""
    if code:
        hit = COUNTRY_BY_CODE.get(str(code).upper())
        if hit:
            return hit
    if not name:
        return name
    raw = str(name).strip()
    hit = (COUNTRY_ALIASES.get(raw.lower())
           or COUNTRY_ALIASES.get(strip_accents(raw).lower()))
    if hit:
        return hit
    # Nominatim sometimes answers with several scripts at once
    # ("ⵍⵉⴱⵢⴰ ليبيا Libya"); take the Latin part if there is one.
    latin = " ".join(w for w in raw.split() if re.fullmatch(r"[A-Za-z.'-]+", w))
    if latin and latin != raw:
        return COUNTRY_ALIASES.get(latin.lower(), latin)
    return raw


DISCIPLINE_RULES = [
    ("Particle & high-energy physics", r"particle physic|high[- ]energy|quantum field|standard model|collider|hadron|neutrino|lhc\b|atlas experiment"),
    ("String theory & gravitation", r"string theory|superstring|supergravit|gravitation|holograph|ads/cft|black hole|cosmolog|general relativ|quantum gravity"),
    ("Quantum information & computing", r"quantum info|quantum comput|qubit|quantum optic|entanglement|quantum technolog|quantum simulation"),
    ("Condensed matter & materials", r"condensed matter|solid[- ]state|material science|materials science|superconduct|magnetism|magnetic material|\balloy|aleacion|nanostructur|nanoscien|nanotechnolog|nanopart|spintron|semiconductor|thin film|\bcrystal|metallurg|corrosion|graphene|hydrogen storage|shape memory|multiferroic|multilayer|anisotropy|sintering"),
    ("Astrophysics & astronomy", r"astrophys|astronom|cosmic ray|galax|stellar|exoplanet|planetary scien|solar physic"),
    ("Nuclear engineering & energy", r"nuclear|reactor|fission|neutron|monte carlo (method|simulation|code)|radioprotection|radiation protection|fuel (cycle|element)|radioisotop|nucleoelectr|criticality|thermal[- ]hydraulic|hydrogen (storage|embrittlement|absorption)"),
    ("Plasma & fusion physics", r"plasma|tokamak|fusion|magnetohydro"),
    ("Atmospheric, earth & environment", r"atmospher|climate|meteorolog|geophys|environment|oceanograph|hydrolog|glaciolog|earth scien|renewable energ|solar energ|wind energ"),
    ("Biophysics, medical & health physics", r"biophys|medical physic|health physic|radiotherap|dosimetr|biomedic|neuroscien|molecular biolog|medicine|clinical|hospital|health"),
    ("Photonics & optics", r"photonic|optic|laser|plasmonic|spectroscop|holograph"),
    ("Computer science, data & AI", r"machine learning|artificial intelligence|deep learning|neural network|computer scien|data scien|data analy|software|algorithm|comput\w* vision|blockchain|cryptograph|informatic"),
    ("Mechanical & aerospace engineering", r"mechanical eng|aerospace|aeronaut|fluid dynam|fluid mechanic|thermodynam|combustion|turbomachin|structural eng|manufacturing"),
    ("Electronics, control & telecom", r"electronic|telecommunicat|signal processing|\bradar\b|\bsonar\b|antenna|microwave|\bfpga\b|embedded system|control system|\bsensors?\b|instrumentation|\bcircuit"),
    ("Mathematics & statistics", r"mathematic|statistic|probability|topolog|geometry|number theory|differential equation|dynamical system"),
    ("Complex systems & statistical physics", r"complex system|statistical (physic|mechanic)|network scien|econophys|nonlinear dynam|agent[- ]based|sociophys"),
    ("Economics, finance & policy", r"econophysic|quantitative finance|financial market|quantitative analyst|science polic|actuaria"),
]

# arXiv primary-category -> discipline (used for INSPIRE authors).
ARXIV_DISCIPLINE_RULES = [
    ("String theory & gravitation", r"hep-th|gr-qc"),
    ("Particle & high-energy physics", r"hep-ph|hep-ex|hep-lat"),
    ("Astrophysics & astronomy", r"astro-ph"),
    ("Nuclear engineering & energy", r"nucl-th|nucl-ex"),
    ("Condensed matter & materials", r"cond-mat"),
    ("Quantum information & computing", r"quant-ph"),
    ("Plasma & fusion physics", r"plasm-ph"),
    ("Mathematics & statistics", r"math-ph|math\."),
    ("Complex systems & statistical physics", r"nlin|physics\.soc-ph"),
]

# Coarse fallback when nothing specific matches, based on the Balseiro degree.
PROGRAM_TO_DISCIPLINE = {
    "Physics": "Physics (general)",
    "Nuclear engineering": "Nuclear engineering & energy",
    "Mechanical engineering": "Mechanical & aerospace engineering",
    "Telecommunications engineering": "Electronics, control & telecom",
}

SECTOR_RULES = [
    # Checked first: "Profesional independiente" would otherwise fall through to
    # nothing, and it is a meaningful answer to "who employs Balseiro people".
    ("Self-employed / freelance", r"^(profesional independiente|aut[oó]nomo|self[- ]employed|freelance|independiente|por cuenta propia|emprendedor|entrepreneur|consultor[ía]* independiente)\b|\bfreelancer\b"),
    ("Industry / company", r"\b(inc|ltd|llc|gmbh|ltda|corp|co\.|pvt|ab|sl|srl|bv)\b|s\.?a\.?s?\b|technolog(y|ies|ia|ía)|\bsolutions\b|\bsystems\b|\bsoftware\b|semiconductor|consult|satellogic|\binvap\b|\bgoogle\b|microsoft|amazon|\bintel\b|\bibm\b|nvidia|bosch|siemens|\bbank\b|\bcapital\b|quantum computing|quantum tech|recursion|traceable|hutek|startup|accenture|grumman|\bypf\b|\bmerck\b|quandela|qilimanjaro|studsvik|ikerlan|seamplex|innomerics|molecular gate|4feedstock|candu owners|itaipu|nucleoel[eé]ctrica|\btecna\b"
     # Employers the LinkedIn exports surfaced that the patterns above miss —
     # mostly private companies with no legal-form suffix in their name.
     r"|mercado libre|\btenaris\b|\btechint\b|globant|despegar|\brappi\b|\bual[aá]\b"
     r"|\baluar\b|\barcor\b|pan american energy|\bvista energy\b|\bpampa energ"
     r"|\bedenor\b|\bedesur\b|telecom argentina|\bmovistar\b|rather labs|skyloom"
     r"|meitner|superhuman|turtle\.xyz|cuballama|latin securities|apollo generators"
     r"|atkins|\bsnc[- ]lavalin\b|\bwestinghouse\b|\bframatome\b|\borano\b"
     r"|\brolls[- ]royce\b|\bairbus\b|\bboeing\b|\bthales\b|\basml\b|\btsmc\b"
     r"|applied materials|qualcomm|\bmicron\b|mckinsey|deloitte|\bpwc\b|\bkpmg\b"
     r"|\bminera\b|\bpetrolera\b|\bpetroleum\b|oil\s?&?\s?gas|\bseguros\b"
     r"|\binsurance\b|fintech|\btrading\b|\bsecurities\b|\bventures\b"),
    ("Government & national lab", r"national laborator|nacional de energ|atomic energy|\bcnea\b|comisi[oó]n nacional|nuclear regulatory|\bcern\b|\bnasa\b|\bconae\b|ministr|ministerio|\bagency\b|agencia (nuclear|espacial|nacional)|oak ridge|los alamos|brookhaven|fermilab|fermi national|argonne|\bslac\b|jefferson lab|\bnist\b|\binta\b|servicio geol|physikalisch-technische bundesanstalt|estaci[oó]n experimental"),
    ("Research institute / council", r"institut|instituto|istituto|centro at[oó]mico|atomic cent(re|er)|centro cient|centro de investigaci|laboratoir|\blaboratory\b|\bcnrs\b|\bcsic\b|\bconicet\b|\bifiba\b|\binfina\b|consejo (nacional|superior) de investigaci|national (council for scientific|scientific and technical)|research council|research (centre|center)|synchrotron|supercomputing (cent(er|re)|center)|physics cent(er|re)|max planck|helmholtz|leibniz|fraunhofer|forschungszentrum|\briken\b|weizmann|perimeter institute|kavli|flatiron|\bictp\b|\bgssi\b|\bicrea\b|\bcinvestav\b|cea saclay|\bcea\b|\bimdea\b|academ(y|ia) of scien|consiglio nazionale|foundation|fundaci[oó]n|\binst\.(\s|,|$)"),
    ("University / academia", r"universi|universidad|universit[aeà]|\buniv\b|college|coll[eè]ge|\bescuela\b|\bfacultad\b|\bfiuba\b|faculty of|school of|polytechnic|polit[eé]cnic|\becole\b|eth z[uü]rich|\bepfl\b|\bmit\b|caltech|leuven|\bunam\b|\buba\b|\bunc\b|\bunlp\b|\buns\b|\butn\b|unizar|\bupm\b|\bkit\b|\bu\.(\s|,|$)"),
    ("Hospital / clinic", r"klinikum|hospital|\bclinic\b|cl[ií]nica"),
    ("School / secondary education", r"\bcolegio|colegios|\be\.?e\.?t\.?\b|escuela t[eé]cnica|escuela de ense|secondary school|high school|\bliceo\b"),
]

DEGREE_PROGRAM_RULES = [
    ("Physics", r"physics|f[ií]sic|licenciatura en f"),
    ("Nuclear engineering", r"nuclear"),
    ("Mechanical engineering", r"mechanic|mec[aá]nic"),
    ("Telecommunications engineering", r"telecom"),
]

# Level of study at Balseiro. Checked in order; a person can match several
# (e.g. did the Licenciatura *and* the Doctorado there).
DEGREE_LEVEL_RULES = [
    ("Doctorate (PhD)", r"\bph\.?\s?d|\bdoctor|\bdr\.?\b|doctorad|doctoral"),
    ("Master's", r"\bmaster|mag[ií]ster|maestr[ií]a|\bm\.?\s?sc|\bmsc\b|magister"),
    ("Specialization / diploma", r"especialist|especializaci[oó]n|\bspecialist|diploma de espec|carrera de especial"),
    ("Engineering degree", r"\bengineer\b|ingenier[oí]|engineering degree|proyecto integrador|nuclear engineer|mechanical engineer"),
    ("Physics degree (Licenciatura)", r"licenciad|licenciatura|bachelor|grado en f[ií]s|physics degree|licentiate"),
]


def _classify(text: str, rules, default=None):
    t = strip_accents(text or "").lower()
    for label, pattern in rules:
        if re.search(pattern, t):
            return label
    return default


def _classify_all(text: str, rules) -> list[str]:
    t = strip_accents(text or "").lower()
    return [label for label, pattern in rules if re.search(pattern, t)]


# --------------------------------------------------------------------------- #
def _blank_person():
    return {
        "name": None, "aka": set(), "orcid": None, "scholar_id": None,
        "wikidata_url": None, "wikipedia": None, "image": None,
        "description": None, "biography": None,
        "birth_year": None, "death_year": None,
        "grad_year": None, "degrees": set(), "degree_program": None,
        "keywords": set(), "occupations": set(), "fields": set(),
        "employer_name": None, "employer_city": None, "employer_country": None,
        "employer_country_code": None, "lat": None, "lon": None,
        "role": None, "sources": set(), "urls": set(),
        "works_count": None, "h_index": None, "concepts": set(),
        "thesis_title": None, "thesis_year": None,
        "career": [], "_orcid_career": [], "_linkedin_career": [],
        "skills": [], "languages": set(), "linkedin_url": None,
        "advisors": set(), "arxiv_categories": set(),
        "loc_asof": None,          # year the displayed location is from, or "manual"
        "_hand_location": False,   # a CSV row supplied an explicit location
        "wikidata_alumnus": None,   # None unknown / True P69 / False staff-only
        "ib_trained": False,        # earliest known position/degree was at Balseiro
        "_wd_employers": [], "_orcid_current": None, "_openalex": None,
        "_inspire_inst": None, "_ads_inst": None,
        "_linkedin_current": None,  # self-reported current job (org/city/country/year)
        "_linkedin": False,         # a LinkedIn profile contributed to this record
    }


def _merge_wikidata(idx, by_orcid, by_name):
    for p in json.loads(RAW_FILES["wikidata"].read_text(encoding="utf-8")):
        key = ("orcid", p["orcid"]) if p.get("orcid") else ("name", name_key(p["name"]))
        rec = idx.setdefault(key, _blank_person())
        rec["name"] = rec["name"] or p["name"]
        rec["sources"].add("wikidata")
        rec["orcid"] = rec["orcid"] or p.get("orcid")
        rec["scholar_id"] = rec["scholar_id"] or p.get("scholar_id")
        rec["wikidata_url"] = p.get("wikidata_url")
        rec["wikipedia"] = rec["wikipedia"] or p.get("wikipedia")
        rec["image"] = rec["image"] or p.get("image")
        rec["description"] = rec["description"] or p.get("description")
        rec["birth_year"] = rec["birth_year"] or p.get("birth_year")
        rec["death_year"] = rec["death_year"] or p.get("death_year")
        rec["occupations"].update(p.get("occupations") or [])
        rec["fields"].update(p.get("fields") or [])
        rec["degrees"].update(p.get("degrees") or [])
        rec["_wd_employers"] = p.get("employers") or []
        if p.get("is_alumnus"):
            rec["wikidata_alumnus"] = True
        elif rec["wikidata_alumnus"] is None:
            rec["wikidata_alumnus"] = False
        if p.get("orcid"):
            by_orcid[p["orcid"]] = key
        by_name.setdefault(name_key(p["name"]), key)


def _orcid_career_entry(e):
    """Turn one ORCID employment-affiliation dict into a career-stop entry
    shaped like INSPIRE's (institution/city/country/start/end/current)."""
    cc = (e.get("country_code") or "").upper()
    return {
        "institution": e.get("org"),
        "city": e.get("city"),
        "country": COUNTRY_BY_CODE.get(cc, cc or None),
        "country_code": cc or None,
        "start": str(e["start_year"]) if e.get("start_year") else None,
        "end": None if e.get("ongoing") else (str(e["end_year"]) if e.get("end_year") else None),
        "current": bool(e.get("ongoing")),
    }


# --------------------------------------------------------------------------- #
# INSPIRE writes institutions in a clipped house style — "Stanford U., Phys.
# Dept.", "Prague, Inst. Phys.", "Cambridge U., DAMTP". Nothing downstream can
# do anything with those: OpenStreetMap has never heard of them and ROR cannot
# match them either, so places as famous as Harvard and the Weizmann Institute
# were sitting unplaced. Expanding them is what makes the rest of the pipeline
# able to answer.
_INSPIRE_ABBR = [(re.compile(r"\b" + a + r"\."), b) for a, b in [
    ("Univ", "University"), ("U", "University"), ("Inst", "Institute"),
    ("Observ", "Observatory"), ("Coll", "College"), ("Natl", "National"),
    ("Lab", "Laboratory"), ("Tech", "Technology"), ("Polytech", "Polytechnic"),
    ("Phys", "Physics"), ("Astron", "Astronomy"), ("Astrophys", "Astrophysics"),
    ("Dept", "Department"), ("Ctr", "Center"), ("Sci", "Science"),
    ("Acad", "Academy"), ("Nucl", "Nuclear"), ("Res", "Research"),
    ("Math", "Mathematics"), ("Chem", "Chemistry"), ("Eng", "Engineering"),
]]
# Argentine company and school names use dots too ("DABIAN S.A.", "Escuela ...
# Ing. Arboit"). Expanding those would mangle them for no gain, so they opt out.
_NOT_INSPIRE = re.compile(
    r"\b(S\.?A\.?|S\.?R\.?L\.?|Ltda|Inc|Corp|L\.P\.|Ing\.|Dr\.|Hno\.|Nro\.|I\.P\.E\.T)\b", re.I)
_INST_WORD = re.compile(
    r"\b(University|Institute|College|Observatory|Laboratory|Academy|Politec"
    r"|Universidad|Instituto)\b", re.I)
# what INSPIRE appends after the institution: a department, a sub-institute,
# a faculty. None of it helps locate the place.
_SUBUNIT = re.compile(
    r"\b(Department|Dept|Faculty|Facultad|Departamento|Division|Group|Grupo"
    r"|Section|Chair|Campus|Sede|School of)\b", re.I)
_INSPIRE_TIDY = [(re.compile(a), b) for a, b in [
    (r"\bIndian Institute Technology\b", "Indian Institute of Technology"),
    (r"\bInstitute Physics\b", "Institute of Physics"),
    (r"\bInstitute Advanced Study\b", "Institute for Advanced Study"),
    (r"\bUniversity Basque Country\b", "University of the Basque Country"),
]]


def _expand_inspire(name):
    """"Stanford U., Phys. Dept." -> "Stanford University"."""
    if not name or _NOT_INSPIRE.search(name):
        return name
    s = name
    for pat, rep in _INSPIRE_ABBR:
        s = pat.sub(rep, s)
    s = re.sub(r"\s*\((?i:main)\)\s*$", "", s)
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2:
        head, tail = parts[0], parts[-1]
        if _SUBUNIT.search(tail) or re.fullmatch(r"[A-Z]{2,8}|[IVX]+([-/][IVX]+)*", tail):
            # a department, an institute acronym or a campus number: noise. The
            # parent institution is the thing that has coordinates.
            s = head
        elif _INST_WORD.search(head):
            # "Indian Institute of Technology, Madras" — the trailing city is
            # the whole point. Dropping it sent that one to the United States.
            s = f"{head}, {tail}"
        elif _INST_WORD.search(tail):
            # "Prague, Institute of Physics" is written city-first; flip it
            s = f"{tail}, {head}"
    for pat, rep in _INSPIRE_TIDY:
        s = pat.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip(" ,.")


def _norm_inst(name):
    """Normalise an institution name for dedupe matching across sources."""
    s = strip_accents((name or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _merge_career(rec):
    """Combine INSPIRE's career history (rec['career']) with ORCID's
    employment history (rec['_orcid_career']) and LinkedIn's positions
    (rec['_linkedin_career']) into one deduped,
    chronologically-sorted list of career stops. Coordinates are filled in
    later by a batched geocode pass; this only merges/sorts/dedupes."""
    stops, seen = [], {}
    for entry in ((rec.get("career") or []) + (rec.get("_orcid_career") or [])
                  + (rec.get("_linkedin_career") or [])):
        inst = _expand_inspire(entry.get("institution"))
        if not inst:
            continue
        entry = dict(entry, institution=inst)
        nk = _norm_inst(inst)
        if nk in seen:
            existing = seen[nk]
            for f in ("city", "country", "country_code", "lat", "lon"):
                if not existing.get(f) and entry.get(f):
                    existing[f] = entry[f]
            existing["current"] = existing.get("current") or entry.get("current")
            if entry.get("start") and (not existing.get("start") or entry["start"] < existing["start"]):
                existing["start"] = entry["start"]
            if entry.get("end") and (not existing.get("end") or entry["end"] > existing["end"]):
                existing["end"] = entry["end"]
            continue
        seen[nk] = dict(entry)
        stops.append(seen[nk])

    # Second pass: INSPIRE and ORCID often name the same job differently
    # ("Witwatersrand U." vs "University of the Witwatersrand"), so a plain
    # name match above misses it. Two stops with the same start+end year are
    # almost certainly the same position -- fold them together, keeping the
    # fuller name and whichever fields either source supplied.
    merged = []
    for entry in stops:
        dup = next((m for m in merged if entry.get("start")
                    and entry["start"] == m.get("start") and entry.get("end") == m.get("end")), None)
        if dup:
            for f in ("city", "country", "country_code", "lat", "lon"):
                if not dup.get(f) and entry.get(f):
                    dup[f] = entry[f]
            if len(entry.get("institution") or "") > len(dup.get("institution") or ""):
                dup["institution"] = entry["institution"]
            dup["current"] = dup.get("current") or entry.get("current")
            continue
        merged.append(entry)
    merged.sort(key=lambda c: c.get("start") or "0")
    return merged


def _merge_orcid(idx, by_orcid, by_name):
    records = json.loads(RAW_FILES["orcid"].read_text(encoding="utf-8"))
    thesis_orcid = DATA / "raw" / "thesis_orcid.json"
    if thesis_orcid.exists():
        records += json.loads(thesis_orcid.read_text(encoding="utf-8"))
    seen_oid = set()
    for p in records:
        if p["orcid"] in seen_oid:
            continue
        seen_oid.add(p["orcid"])
        nk = name_key(p["name"])
        key = by_orcid.get(p["orcid"]) or by_name.get(nk) or ("orcid", p["orcid"])
        rec = idx.setdefault(key, _blank_person())
        rec["name"] = rec["name"] or p["name"]
        if p["name"] and rec["name"] and p["name"] != rec["name"]:
            rec["aka"].add(p["name"])
        rec["sources"].add("orcid")
        rec["orcid"] = rec["orcid"] or p.get("orcid")
        rec["grad_year"] = rec["grad_year"] or p.get("grad_year")
        rec["degrees"].update(p.get("degrees") or [])
        rec["keywords"].update(p.get("keywords") or [])
        rec["biography"] = rec["biography"] or p.get("biography")
        rec["urls"].update(p.get("urls") or [])
        rec["_orcid_current"] = p.get("current_employer")
        if p.get("all_employers") and not rec["_orcid_career"]:
            rec["_orcid_career"] = [_orcid_career_entry(e) for e in p["all_employers"]
                                     if e.get("org")]
        by_orcid.setdefault(p["orcid"], key)
        by_name.setdefault(nk, key)


def _merge_wikipedia(idx, by_orcid, by_name):
    for p in json.loads(RAW_FILES["wikipedia"].read_text(encoding="utf-8")):
        nk = name_key(p["name"])
        key = by_name.get(nk)
        if key is None and p.get("qid"):
            for k, v in idx.items():
                if (v.get("wikidata_url") or "").endswith("/" + p["qid"]):
                    key = k
                    break
        key = key or ("name", nk)
        rec = idx.setdefault(key, _blank_person())
        rec["name"] = rec["name"] or p["name"]
        rec["sources"].add("wikipedia")
        rec["wikipedia"] = rec["wikipedia"] or p.get("wikipedia")
        rec["image"] = rec["image"] or p.get("image")
        rec["description"] = rec["description"] or p.get("description")
        by_name.setdefault(nk, key)


def _merge_csv(idx, by_name, path, source_tag, kept_only=False):
    """Merge a hand-editable CSV of people (manual_alumni.csv or the kept rows
    of review_candidates.csv). Only `name` is required."""
    if not path.exists():
        return 0
    n = 0
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not row.get("name") or row["name"].lstrip().startswith("#"):
                continue
            if kept_only and row.get("keep", "").lower() not in ("y", "yes", "1", "x", "true"):
                continue
            nk = name_key(row["name"])
            key = by_name.get(nk) or ("name", nk)
            rec = idx.setdefault(key, _blank_person())
            rec["name"] = rec["name"] or row["name"]
            rec["sources"].add(source_tag)
            for field in ("grad_year", "role", "degree_program", "description"):
                if row.get(field):
                    rec[field] = row[field]
            if row.get("field"):
                rec["fields"].add(row["field"])
            if row.get("concepts"):
                rec["concepts"].update(c.strip() for c in row["concepts"].split(";") if c.strip())
            for u in re.split(r"[;\s]+", row.get("links", "") + " " + row.get("openalex_url", "")):
                if u:
                    rec["urls"].add(u)
            # a hand-supplied location = the person actually checked it; a bare
            # kept review-queue row (no city/country/coords) is still just the
            # OpenAlex guess in `current_institution` and must NOT count as one.
            hand_loc = bool(row.get("city") or row.get("country")
                            or row.get("employer") or row.get("lat"))
            if row.get("employer") or row.get("current_institution"):
                rec["employer_name"] = row.get("employer") or row.get("current_institution")
            if row.get("city"):
                rec["employer_city"] = row["city"]
            if row.get("country"):
                rec["employer_country"] = row["country"]
            try:
                if row.get("lat") and row.get("lon"):
                    rec["lat"], rec["lon"] = float(row["lat"]), float(row["lon"])
                    hand_loc = True
            except ValueError:
                pass
            if hand_loc:
                rec["_hand_location"] = True
            by_name.setdefault(nk, key)
            n += 1
    return n


def _merge_linkedin(idx, by_name):
    """Merge normalised LinkedIn profiles (data/raw/linkedin_profiles.json).

    Only profiles that name Instituto Balseiro in their *education* history
    create new people — that is the one signal that actually says "graduate".
    Everything weaker (a CAB/CNEA job, a mention in the headline, or a stub row
    from the not-yet-downloaded queue) may only enrich someone we already know
    about from another source, never add a name on its own.
    """
    path = RAW_FILES["linkedin"]
    if not path.exists():
        return 0, 0

    added = enriched = 0
    for p in json.loads(path.read_text(encoding="utf-8")):
        verified = p.get("balseiro_evidence") == "education"
        nk = name_key(p["name"])
        if not nk:
            continue
        key = by_name.get(nk)
        if key is None:
            if not verified:
                continue          # weak evidence + nobody to attach it to
            key = ("name", nk)
        rec = idx.setdefault(key, _blank_person())
        if rec["name"] is None:
            added += 1
        else:
            enriched += 1
        rec["name"] = rec["name"] or p["name"]
        rec["sources"].add("linkedin")
        rec["_linkedin"] = True
        if verified:
            # a self-declared Balseiro degree is as good as the thesis roster
            rec["ib_trained"] = True

        # --- who they are -------------------------------------------------- #
        if p["name"] != rec["name"]:
            rec["aka"].add(p["name"])
        if p.get("headline") and not rec["description"]:
            rec["description"] = p["headline"]
        if p.get("linkedin_url") or p.get("profile_url"):
            rec["linkedin_url"] = p.get("profile_url")
        rec["languages"].update(p.get("languages") or [])
        # skills double as keywords for the site's free-text search
        for s in (p.get("skills") or []):
            if s not in rec["skills"]:
                rec["skills"].append(s)

        # --- degree / graduation ------------------------------------------- #
        for d in (p.get("degrees") or []):
            rec["degrees"].add(d)
        if p.get("program") and not rec["degree_program"]:
            rec["degree_program"] = p["program"]
        if p.get("grad_year") and not rec["grad_year"]:
            rec["grad_year"] = p["grad_year"]

        # --- career + current job ------------------------------------------ #
        rec["_linkedin_career"] = [
            dict({k: v for k, v in c.items() if k not in ("is_default", "url")},
                 kind="job")
            for c in (p.get("career") or [])
        ] + list(p.get("education_stops") or [])
        if p.get("current_employer"):
            rec["_linkedin_current"] = {
                "org": p["current_employer"],
                "city": p.get("current_city"),
                "country": p.get("current_country"),
                "country_code": p.get("current_country_code"),
                "role": p.get("current_role"),
                "year": p.get("asof"),
            }
        if p.get("current_role") and not rec["role"]:
            rec["role"] = p["current_role"]

        by_name.setdefault(nk, key)
    return added, enriched


def _load_openalex():
    out = []
    for f in (RAW_FILES["openalex"], DATA / "raw" / "thesis_reconciled.json"):
        if f.exists():
            out += json.loads(f.read_text(encoding="utf-8"))
    # de-dupe by OpenAlex id, preferring the richer Balseiro-pool record
    by_id = {}
    for a in out:
        by_id.setdefault(a["openalex_id"], a)
    return list(by_id.values())


def _oa_location(inst):
    """(lat, lon, city, country, country_code) for an OpenAlex institution dict."""
    if not inst:
        return None
    geo = collect_openalex.institution_geo(inst.get("openalex_id"))
    if geo:
        return geo
    cc = (inst.get("country_code") or "").upper()
    return {"lat": None, "lon": None, "city": None,
            "country": COUNTRY_BY_CODE.get(cc), "country_code": cc or None}


def _build_ikey_index(idx):
    """initial_key -> idx key, or None where two different people collide."""
    by_ikey = {}
    for key, rec in idx.items():
        ik = initial_key(rec["name"] or "")
        if not ik or ik.endswith("|"):
            continue
        by_ikey[ik] = key if ik not in by_ikey else (
            by_ikey[ik] if by_ikey[ik] == key else None)
    return by_ikey


def _absorb(target, other):
    """Fold `other`'s data into `target` (both are _blank_person dicts)."""
    for k, v in other.items():
        if isinstance(v, set):
            target[k] |= v
        elif isinstance(v, list):
            if not target.get(k):
                target[k] = v
        elif k == "name":
            if v and (not target["name"] or len(v) > len(target["name"])):
                if target["name"]:
                    target["aka"].add(target["name"])
                target["name"] = v
            elif v and v != target["name"]:
                target["aka"].add(v)
        elif v is not None and not target.get(k):
            target[k] = v


_COMMON_GIVEN = {
    "roberto", "juan", "jose", "maria", "ana", "carlos", "luis", "jorge", "daniel",
    "pablo", "diego", "martin", "gabriel", "alejandro", "fernando", "ricardo",
    "eduardo", "francisco", "miguel", "andres", "pedro", "javier", "sergio",
    "gustavo", "mario", "raul", "oscar", "hugo", "hector", "adrian", "ariel",
    "marcelo", "gonzalo", "matias", "nicolas", "santiago", "agustin", "ignacio",
    "laura", "lucia", "sofia", "julia", "paula", "elena", "silvia", "claudia",
    "john", "david", "michael", "paul", "peter", "mark",
}


def _name_tokens(name):
    from common import _name_parts
    return {t for t in _name_parts(name) if len(t) >= 4}


def _dedupe_by_initials(idx):
    """Merge records that are clearly the same person under different name forms
    ("Ana A. Gramajo" / "Ana Alicia Gramajo"): same surname+given-initials key,
    a shared real surname token, ORCIDs that don't conflict, countries
    compatible."""
    groups: dict[str, list] = {}
    for key, rec in idx.items():
        ik = initial_key(rec["name"] or "")
        if ik and not ik.endswith("|"):
            groups.setdefault(ik, []).append(key)

    merged = 0
    for ik, keys in groups.items():
        if len(keys) < 2:
            continue
        pairs = [(k, idx[k]) for k in keys if k in idx]
        pairs.sort(key=lambda kv: (-kv[1]["_hand_location"], -kv[1]["_linkedin"],
                                   -len(kv[1]["sources"]), -bool(kv[1]["orcid"])))
        anchor = pairs[0][1]
        anchor_toks = _name_tokens(anchor["name"])
        for r_key, r in pairs[1:]:
            orc_conflict = anchor["orcid"] and r["orcid"] and anchor["orcid"] != r["orcid"]
            c1, c2 = anchor["employer_country"], r["employer_country"]
            country_conflict = c1 and c2 and c1 != c2
            hand = anchor["_hand_location"] or r["_hand_location"]
            # A country clash normally means "two different people". It does not
            # when one side is LinkedIn: that side is simply more up to date
            # than a years-old paper affiliation, so let the merge through.
            fresh = hand or anchor["_linkedin"] or r["_linkedin"]
            # need a shared surname-like token (not just a shared common first name)
            shared_real = (anchor_toks & _name_tokens(r["name"])) - _COMMON_GIVEN
            if not shared_real or orc_conflict or (country_conflict and not fresh):
                continue
            # a hand-checked location on the loser overrides the anchor's
            if r["_hand_location"] and not anchor["_hand_location"]:
                for f in ("employer_name", "employer_city", "employer_country",
                          "employer_country_code", "lat", "lon", "role"):
                    anchor[f] = r[f] if r[f] is not None else anchor[f]
                anchor["_hand_location"] = True
            # LinkedIn's own payload (_linkedin_current, career, skills) rides
            # along in _absorb below and is picked up by _resolve_location.
            _absorb(anchor, r)
            del idx[r_key]
            merged += 1
    if merged:
        print(f"dedupe: merged {merged} name-variant duplicates")
    return merged


def _enrich_openalex(idx, by_orcid, by_name, blocked):
    """Attach OpenAlex stats to people we already have, and add the
    high-confidence ORCID-less authors straight to the map."""
    authors = _load_openalex()
    by_ikey = _build_ikey_index(idx)
    added = enriched = via_initials = 0
    for a in authors:
        nk = name_key(a["name"])
        if nk in blocked:
            continue
        key = (by_orcid.get(a["orcid"]) if a.get("orcid") else None) or by_name.get(nk)

        # "A. Baruj" (OpenAlex) vs "Alberto Baruj" (thesis roster): match on the
        # looser initials key, but only when it points to exactly one person.
        if key is None and is_initials_form(a["name"]):
            ik_key = by_ikey.get(initial_key(a["name"]))
            if ik_key is not None:
                key = ik_key
                via_initials += 1

        if key is None:
            if (a.get("review_score") or 0) < OA_AUTO_KEEP:
                continue  # handled by the review queue instead
            key = ("oa", a["openalex_id"])
            idx[key] = _blank_person()
            idx[key]["name"] = a["name"]
            idx[key]["sources"].add("openalex")
            by_name.setdefault(nk, key)
            loc = _oa_location(a.get("current_institution"))
            if loc:
                ci = a["current_institution"]
                idx[key]["employer_name"] = ci.get("name")
                idx[key]["lat"], idx[key]["lon"] = loc["lat"], loc["lon"]
                idx[key]["employer_city"] = loc.get("city")
                idx[key]["employer_country"] = loc.get("country")
                idx[key]["employer_country_code"] = loc.get("country_code")
            added += 1

        rec = idx[key]
        rec["_openalex"] = a
        rec["works_count"] = a.get("works_count") or rec["works_count"]
        rec["h_index"] = a.get("h_index") or rec["h_index"]
        rec["concepts"].update(a.get("concepts") or [])
        if a.get("orcid") and not rec["orcid"]:
            rec["orcid"] = a["orcid"]
        if not rec["grad_year"] and a.get("balseiro_years"):
            rec["grad_year"] = min(a["balseiro_years"])
        enriched += 1
    print(f"OpenAlex merge: enriched {enriched} people "
          f"({via_initials} matched to a roster name by initials), "
          f"added {added} new (score >= {OA_AUTO_KEEP})")


def _write_review_queue(idx, by_orcid, by_name, blocked):
    """Regenerate review_candidates.csv for the mid-confidence ORCID-less
    OpenAlex authors, preserving any `keep` marks already set."""
    prior = {}
    if REVIEW_CSV.exists():
        with REVIEW_CSV.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("openalex_id"):
                    prior[row["openalex_id"]] = row.get("keep", "")

    rows = []
    for a in _load_openalex():
        s = a.get("review_score") or 0
        if not (OA_REVIEW_MIN <= s < OA_AUTO_KEEP):
            continue
        nk = name_key(a["name"])
        if nk in blocked:
            continue
        if (a.get("orcid") and a["orcid"] in by_orcid) or nk in by_name:
            continue  # already in the dataset from another source
        ci = a.get("current_institution") or {}
        rows.append({
            "keep": prior.get(a["openalex_id"], ""),
            "name": a["name"],
            "score": s,
            "grad_year": min(a["balseiro_years"]) if a.get("balseiro_years") else "",
            "current_institution": ci.get("name") or "",
            "country": COUNTRY_BY_CODE.get((ci.get("country_code") or "").upper(),
                                           ci.get("country_code") or ""),
            "city": "", "employer": "", "lat": "", "lon": "",
            "degree_program": "", "field": "",
            "concepts": "; ".join(a.get("concepts") or []),
            "works": a.get("works_count") or "",
            "openalex_url": f"https://openalex.org/{a['openalex_id']}",
            "openalex_id": a["openalex_id"],
        })
    rows.sort(key=lambda r: -r["score"])

    cols = ["keep", "name", "score", "grad_year", "current_institution", "country",
            "city", "employer", "lat", "lon", "degree_program", "field",
            "concepts", "works", "openalex_url", "openalex_id"]
    with REVIEW_CSV.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerow({"keep": "# set keep=y to add a row to the map; "
                            "fill city/country or lat,lon if the institution is wrong or missing",
                    "name": "", "score": ""})
        w.writerows(rows)
    kept = sum(1 for r in rows if r["keep"].lower() in ("y", "yes", "1", "x", "true"))
    print(f"review queue: {len(rows)} candidates in {REVIEW_CSV.name} "
          f"({kept} marked keep)")


def _career_earliest_is_ib(career):
    dated = [c for c in career if c.get("start")]
    if not dated:
        return False
    first = min(dated, key=lambda c: c["start"])
    inst = (first.get("institution") or "").lower()
    return "balseiro" in inst or "bariloche" in inst


def _merge_inspire(idx, by_orcid, by_name):
    """INSPIRE-HEP: career history + PhD advisor for the physics diaspora."""
    if not RAW_FILES["inspire"].exists():
        return 0
    n = 0
    for a in json.loads(RAW_FILES["inspire"].read_text(encoding="utf-8")):
        nk = name_key(a["name"])
        key = (by_orcid.get(a["orcid"]) if a.get("orcid") else None) or by_name.get(nk) \
            or ("name", nk)
        rec = idx.setdefault(key, _blank_person())
        rec["name"] = rec["name"] or a["name"]
        rec["sources"].add("inspire")
        rec["orcid"] = rec["orcid"] or a.get("orcid")
        rec["advisors"].update(a.get("advisors") or [])
        rec["arxiv_categories"].update(a.get("arxiv_categories") or [])
        if a.get("field_blob"):
            rec["keywords"].add(a["field_blob"])
        if a.get("career"):
            rec["career"] = a["career"]
        if _career_earliest_is_ib(a.get("career") or []):
            rec["ib_trained"] = True
        ci = a.get("current_institution")
        if ci and ci.get("name"):
            cur_yrs = [int(c["start"]) for c in (a.get("career") or [])
                       if c.get("current") and c.get("start")]
            rec["_inspire_inst"] = dict(ci, year=max(cur_yrs) if cur_yrs else None)
        if a.get("orcid"):
            by_orcid.setdefault(a["orcid"], key)
        by_name.setdefault(nk, key)
        n += 1
    return n


def _merge_ads(idx, by_orcid, by_name):
    """NASA ADS: latest-paper affiliation. *Enrich only* — ADS's Balseiro-aff
    set is ~2,600 people (heavy Auger-collaboration overlap), far too noisy to
    add as new alumni, so we only attach data to people we already have."""
    if not RAW_FILES["ads"].exists():
        return 0
    ik_index = _build_ikey_index(idx)
    n = 0
    for a in json.loads(RAW_FILES["ads"].read_text(encoding="utf-8")):
        nk = name_key(a["name"])
        key = (by_orcid.get(a["orcid"]) if a.get("orcid") else None) or by_name.get(nk) \
            or ik_index.get(initial_key(a["name"]))
        if key is None or key not in idx:
            continue
        rec = idx[key]
        rec["sources"].add("ads")
        rec["orcid"] = rec["orcid"] or a.get("orcid")
        if a.get("current_institution") and not rec["_inspire_inst"] and not rec["_ads_inst"]:
            rec["_ads_inst"] = dict(a["current_institution"],
                                    year=a.get("last_paper_year"))
        n += 1
    return n


def _merge_lens(idx, by_orcid, by_name, blocked):
    """Patent inventors from Balseiro-linked orgs (Lens.org). Enriches matches;
    adds new people only for INVAP / Instituto Balseiro / CAB patents."""
    if not RAW_FILES["lens"].exists():
        return 0
    ik_index = _build_ikey_index(idx)
    added = enriched = 0
    for p in json.loads(RAW_FILES["lens"].read_text(encoding="utf-8")):
        nk = name_key(p["name"])
        if nk in blocked:
            continue
        key = by_name.get(nk) or ik_index.get(initial_key(p["name"]))
        if key is None or key not in idx:
            if p.get("via_applicant") not in LENS_DISCOVERY_APPLICANTS:
                continue
            key = ("lens", nk)
            idx[key] = _blank_person()
            idx[key]["name"] = p["name"]
            by_name.setdefault(nk, key)
            added += 1
        rec = idx[key]
        rec["sources"].add("lens")
        rec["role"] = rec["role"] or "Inventor / engineer"
        if p.get("employer") and not rec["employer_name"]:
            rec["employer_name"] = p["employer"]
            rec["employer_country"] = p.get("country") or rec["employer_country"]
            rec["employer_country_code"] = p.get("country_code") or rec["employer_country_code"]
        enriched += 1
    print(f"Lens merge: enriched {enriched} people, added {added} new")
    return added


def _merge_ricabib(idx, by_name):
    """Optional: IB thesis repository (author + year + title) — authoritative
    alumni names. Run scripts/collect_ricabib.py from Argentina to populate it."""
    if not RAW_FILES["ricabib"].exists():
        return 0
    n = 0
    for t in json.loads(RAW_FILES["ricabib"].read_text(encoding="utf-8")):
        if not t.get("author"):
            continue
        nk = name_key(t["author"])
        key = by_name.get(nk) or ("name", nk)
        rec = idx.setdefault(key, _blank_person())
        rec["name"] = rec["name"] or t["author"]
        rec["sources"].add("ricabib")
        rec["thesis_title"] = rec["thesis_title"] or t.get("title")
        rec["thesis_year"] = rec["thesis_year"] or t.get("year")
        rec["keywords"].update(s for s in (t.get("subjects") or []) if s.isascii())
        if not rec["grad_year"] and t.get("year"):
            rec["grad_year"] = t["year"]
        if t.get("degree_program") and not rec["degree_program"]:
            rec["degree_program"] = t["degree_program"]
        by_name.setdefault(nk, key)
        n += 1
    return n


def _load_blocklist():
    if not BLOCKLIST.exists():
        return set()
    out = set()
    for line in BLOCKLIST.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(name_key(line))
    return out


# --------------------------------------------------------------------------- #
def _chain(*queries):
    """Ordered, de-duplicated list of non-empty geocode queries to try."""
    seen, out = set(), []
    for q in queries:
        q = (q or "").strip(" ,")
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


_THIS_YEAR = dt.date.today().year


def _asof(year):
    """Clamp a location-vintage year to something sensible (no future dates)."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    return y if 1955 <= y <= _THIS_YEAR else None


def _resolve_location(rec):
    """Fill employer_name / city / country, set rec['_geo_chain'], and record
    rec['loc_asof'] (the year the *chosen* location dates from, or 'manual').

    The chain lists progressively looser geocode queries; the first that
    resolves wins (institution -> city -> country).
    """
    oc = rec["_orcid_current"]
    wd = [e for e in rec["_wd_employers"] if e.get("name")]
    wd_with_coord = [e for e in wd if e.get("lat") is not None]
    rec["_geo_chain"] = []
    rec["loc_asof"] = None

    # 1. explicit coordinates already set (from a CSV row)
    if rec["lat"] is not None and rec["lon"] is not None:
        rec["employer_name"] = rec["employer_name"] or (oc or {}).get("org") or (wd[0]["name"] if wd else None)
        if rec["_hand_location"]:
            rec["loc_asof"] = "manual"
        return

    # 1b. a hand-checked CSV location ALWAYS wins over the automatic sources.
    if rec["_hand_location"] and (
            rec["employer_name"] or rec["employer_city"] or rec["employer_country"]):
        org, city, country = rec["employer_name"], rec["employer_city"], rec["employer_country"]
        rec["loc_asof"] = "manual"
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [org, city, country] if b),
            ", ".join(b for b in [org, country] if b),
            ", ".join(b for b in [city, country] if b),
            country,
        )
        return

    # 1b-bis. A LinkedIn current job. This is the person saying where they work
    # *today*, which beats every automatic source below: ORCID employments go
    # stale, and an ADS/OpenAlex affiliation is only where they were when they
    # last published. A hand-checked CSV row (1b) still outranks it.
    lc = rec.get("_linkedin_current")
    if lc and lc.get("org") and (lc.get("city") or lc.get("country")):
        rec["employer_name"] = rec["employer_name"] or lc["org"]
        rec["employer_city"] = rec["employer_city"] or lc.get("city")
        rec["employer_country"] = rec["employer_country"] or lc.get("country")
        rec["employer_country_code"] = rec["employer_country_code"] or lc.get("country_code")
        rec["loc_asof"] = _asof(lc.get("year"))
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [lc["org"], lc.get("city"), lc.get("country")] if b),
            ", ".join(b for b in [lc["org"], lc.get("country")] if b),
            ", ".join(b for b in [lc.get("city"), lc.get("country")] if b),
            lc.get("country"))
        return

    # 1c. INSPIRE current institution (an explicit "current": true career
    # entry) is the freshest "where are they now" signal we have -- it wins
    # even when INSPIRE's own institution record lacks coordinates. Falling
    # through to step 5/6 in that case used to let a stale ORCID/OpenAlex
    # employer win instead (e.g. someone who just moved but hasn't updated
    # their ORCID profile yet) -- geocode by name/city/country instead.
    ii = rec.get("_inspire_inst")
    if ii and ii.get("name") and not (oc and oc.get("ongoing")):
        rec["employer_name"] = rec["employer_name"] or ii.get("name")
        rec["employer_city"] = rec["employer_city"] or ii.get("city")
        rec["employer_country"] = rec["employer_country"] or ii.get("country")
        rec["employer_country_code"] = rec["employer_country_code"] or ii.get("country_code")
        rec["loc_asof"] = _asof(ii.get("year"))
        if ii.get("lat") is not None:
            rec["lat"], rec["lon"] = ii["lat"], ii["lon"]
        else:
            rec["_geo_chain"] = _chain(
                ", ".join(b for b in [ii.get("name"), ii.get("city"), ii.get("country")] if b),
                ", ".join(b for b in [ii.get("name"), ii.get("country")] if b),
                ", ".join(b for b in [ii.get("city"), ii.get("country")] if b),
                ii.get("country"))
        return

    # 2. Wikidata employer that carries coordinates (no date available)
    if wd_with_coord and not (oc and oc.get("ongoing")):
        e = wd_with_coord[0]
        rec.update(employer_name=e["name"], employer_country=e.get("country"),
                   lat=e["lat"], lon=e["lon"])
        rec["role"] = rec["role"] or _nice_role(rec)
        return

    # 3. ORCID current employer
    if oc and oc.get("org"):
        cc = (oc.get("country_code") or "").upper()
        country = COUNTRY_BY_CODE.get(cc, cc or None)
        rec["employer_name"] = oc["org"]
        rec["employer_city"] = oc.get("city")
        rec["employer_country_code"] = cc or None
        rec["employer_country"] = COUNTRY_BY_CODE.get(cc, rec["employer_country"])
        rec["role"] = rec["role"] or oc.get("role")
        rec["loc_asof"] = _asof(oc.get("start_year") if oc.get("ongoing") else oc.get("end_year"))
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [oc["org"], oc.get("city"), country] if b),
            ", ".join(b for b in [oc["org"], country] if b),
            ", ".join(b for b in [oc.get("city"), oc.get("region"), country] if b),
            ", ".join(b for b in [oc.get("city"), country] if b),
            country,
        )
        return

    # 4. Wikidata employer without coordinates
    if wd:
        e = wd[0]
        rec["employer_name"] = e["name"]
        rec["employer_country"] = e.get("country")
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [e["name"], e.get("country")] if b),
            e["name"],
            e.get("country"),
        )
        return

    # 5. OpenAlex current institution — try its own coordinates first, then geocode
    oa = rec.get("_openalex") or {}
    ci = oa.get("current_institution") or {}
    if ci.get("name"):
        cc = (ci.get("country_code") or "").upper()
        country = COUNTRY_BY_CODE.get(cc, cc or None)
        rec["employer_name"] = rec["employer_name"] or ci["name"]
        rec["employer_country"] = rec["employer_country"] or country
        geo = collect_openalex.institution_geo(ci.get("openalex_id"))
        if geo:
            rec["lat"], rec["lon"] = geo["lat"], geo["lon"]
            rec["employer_city"] = rec["employer_city"] or geo.get("city")
            rec["employer_country"] = geo.get("country") or rec["employer_country"]
            rec["employer_country_code"] = geo.get("country_code")
            return
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [ci["name"], country] if b),
            ci["name"], country,
        )
        return

    # 6. last resort — INSPIRE ICN (no coords, and step 1c's ORCID-ongoing
    # exception applied) or a messy ADS affiliation string
    ii = rec.get("_inspire_inst")
    if ii and ii.get("name"):
        rec["employer_name"] = rec["employer_name"] or ii.get("name")
        rec["employer_city"] = rec["employer_city"] or ii.get("city")
        rec["employer_country"] = rec["employer_country"] or ii.get("country")
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [ii.get("name"), ii.get("city"), ii.get("country")] if b),
            ", ".join(b for b in [ii.get("name"), ii.get("country")] if b),
            ", ".join(b for b in [ii.get("city"), ii.get("country")] if b),
            ii.get("country"))
        return
    ai = rec.get("_ads_inst")
    if ai and ai.get("name"):
        rec["employer_name"] = rec["employer_name"] or ai["name"]
        rec["loc_asof"] = _asof(ai.get("year"))
        rec["_geo_chain"] = _chain(ai["name"], ai["name"].split(",")[-1].strip())
        return

    # 7. a bare employer name/country from Lens or a manual/reviewed row
    if rec["employer_name"]:
        rec["_geo_chain"] = _chain(
            ", ".join(b for b in [rec["employer_name"], rec["employer_country"]] if b),
            rec["employer_name"], rec["employer_country"])
        return


# --------------------------------------------------------------------------- #
# Employer name canonicalisation
# --------------------------------------------------------------------------- #
# Every source names the same institution differently: RICABIB is in Spanish,
# INSPIRE abbreviates ("Balseiro Inst."), OpenAlex translates ("Balseiro
# Institute"), LinkedIn carries whatever the person typed. Left alone, the
# employer ranking splits one institution across a dozen rows.
#
# Layer 1: explicit rules, for cases no string comparison can catch — a
# translation, an abbreviation, or a compound affiliation that names a parent
# and a child institution together.
EMPLOYER_ALIASES = [
    # the Balseiro / Centro Atómico Bariloche family: one campus, many namings
    (r"balseiro", "Instituto Balseiro"),
    (r"centro atomico bariloche|bariloche atomic", "Centro Atómico Bariloche"),
    (r"centro atomico constituyentes", "Centro Atómico Constituyentes"),
    (r"centro atomico ezeiza", "Centro Atómico Ezeiza"),
    # the English renderings come from OpenAlex/ADS and are misspelled about as
    # often as not ("Comission", "Commision"), hence the loose spelling
    (r"^cnea\b|comision nacional de energia atomica"
     r"|national atomic energy com|national comm?is?s?ion of atomic energy",
     "Comisión Nacional de Energía Atómica (CNEA)"),
    # faculty/department fragments of universities already on the map
    (r"^universidad de buenos aires\b|^facultad de ciencias exactas y naturales, uba$"
     r"|^.*\buba\b.*facultad|^fcen[ -]uba$",
     "Universidad de Buenos Aires"),
    (r"^universidad nacional de cordoba\b|famaf", "Universidad Nacional de Córdoba"),
    (r"^universidad nacional de la pampa\b|unlpam", "Universidad Nacional de La Pampa"),
    # "Buenos Aires, CONICET" is a place-prefixed form of the same employer.
    # The prefix is deliberately restricted to a short plain-words city name:
    # a longer or punctuated one means a named sub-institute ("Instituto de
    # Física del Sur (IFISUR), ..., CONICET"), which must stay on its own.
    (r"^conicet$|^[a-z ]{1,20}, conicet$"
     r"|consejo nacional de investigaciones cientificas"
     r"|scientific and technical research council",
     "CONICET"),
    (r"conicet.*patagonia norte|patagonia norte.*conicet", "CONICET Patagonia Norte"),
    (r"nanociencia y nanotecnologia|instituto de nanociencia",
     "Instituto de Nanociencia y Nanotecnología (CNEA–CONICET)"),
    (r"^invap\b", "INVAP"),
    (r"^ypf\b", "YPF"),
    (r"nucleoelectrica argentina", "Nucleoeléctrica Argentina S.A."),
    (r"^profesional independiente$|^autonomo$|^self employed$|^freelance$",
     "Self-employed"),
]
EMPLOYER_ALIASES = [(re.compile(p), c) for p, c in EMPLOYER_ALIASES]

# Words that carry no distinguishing information, so "Universidad Nacional de
# Cuyo" and "National University of Cuyo" reduce to the same key.
_INST_STOP = {
    "de", "del", "la", "el", "los", "las", "y", "of", "the", "for", "and", "in",
    "at", "a", "en", "nacional", "national", "universidad", "university", "univ",
    "instituto", "institute", "inst", "centro", "center", "centre", "research",
    "laboratory", "lab", "labs", "sa", "se", "srl", "ltd", "inc", "gmbh",
}


def _inst_key(name):
    """Order-independent fingerprint of an institution name."""
    s = strip_accents((name or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return frozenset(t for t in s.split() if t not in _INST_STOP and len(t) > 2)


def _hand_key(name):
    """Like _inst_key, but keeps two-letter words.

    The hand table matches on a subset of the stop's words, so a key of a
    single common word is dangerous: "Scale AI" reduced to {scale} under
    _inst_key and happily matched "Center for Atomic-scale Materials Physics".
    Keeping "ai" makes the row demand both words.
    """
    s = strip_accents((name or "").lower())
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return frozenset(t for t in s.split() if t not in _INST_STOP and len(t) > 1)


def _alias(name):
    if not name:
        return name
    name = _expand_inspire(name)
    flat = re.sub(r"\s+", " ", strip_accents(name.lower())).strip()
    for pattern, canonical in EMPLOYER_ALIASES:
        if pattern.search(flat):
            return canonical
    return name


# INSPIRE writes institutions in a clipped house style ("Madrid, Autonoma U.",
# "Balseiro Inst."). Those spellings can easily be the most numerous, but they
# make a poor label, so well-formed names win regardless of frequency.
_CLIPPED = re.compile(r"\b(U|Inst|Ctr|Lab|Natl|Univ|Tech|Dept)\.", re.I)


def _spelling_rank(name, count):
    presentable = not _CLIPPED.search(name) and name != name.lower()
    return (presentable, count, len(name), name)


def _canonicalise_employers(idx):
    """Fold spelling variants of one institution onto a single name.

    Layer 1 applies the explicit rules above. Layer 2 then groups whatever is
    left by `_inst_key` and elects the most common spelling as canonical, which
    absorbs accent, case, punctuation and word-order variants — including ones
    nobody has seen yet, so a new batch of profiles does not reintroduce the
    problem. Grouping needs an *identical* token set, so distinct institutions
    that merely share a word ("Max Planck Institute for Physics" vs "... for
    Chemistry") are never merged; the failure mode is leaving two names apart,
    not collapsing two real institutions into one.
    """
    for rec in idx.values():
        rec["employer_name"] = _alias(rec["employer_name"])
        for stop in rec["career"]:
            stop["institution"] = _alias(stop.get("institution"))

    counts: dict = {}
    for rec in idx.values():
        names = [rec["employer_name"]] + [s.get("institution") for s in rec["career"]]
        for name in names:
            if not name:
                continue
            key = _inst_key(name)
            if key:
                counts.setdefault(key, collections.Counter())[name] += 1

    canonical = {
        key: max(spellings.items(), key=lambda kv: _spelling_rank(*kv))[0]
        for key, spellings in counts.items()
    }

    folded = 0
    for rec in idx.values():
        name = rec["employer_name"]
        if name:
            best = canonical.get(_inst_key(name), name)
            if best != name:
                folded += 1
            rec["employer_name"] = best
        for stop in rec["career"]:
            inst = stop.get("institution")
            if inst:
                stop["institution"] = canonical.get(_inst_key(inst), inst)

    distinct = len({rec["employer_name"] for rec in idx.values() if rec["employer_name"]})
    print(f"employers: {distinct} distinct names after folding "
          f"{folded} people onto a canonical spelling")


def _is_yes(v):
    return str(v or "").strip().lower() in ("y", "yes", "1", "true", "x", "si", "sí")


def _hand_placed_institutions(idx):
    """Place career stops listed in data/institutions.csv.

    Nominatim cannot resolve a bare organisation name that is not itself a
    mapped place, and mis-resolves INSPIRE's "City, Acronym" style. The CSV
    supplies the city, so the builder can ask a question OSM can answer —
    the coordinates are still geocoded, not typed in. Explicit lat/lon in the
    CSV wins over geocoding when given.
    """
    if not INSTITUTIONS_CSV.exists():
        return
    entries = []
    with INSTITUTIONS_CSV.open(encoding="utf-8") as fh:
        for row in csv.DictReader(l for l in fh if not l.lstrip().startswith("#")):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not row.get("institution"):
                continue
            entries.append(row)
    if not entries:
        return

    placed: dict = {}
    queries = {}
    for row in entries:
        try:
            if row.get("lat") and row.get("lon"):
                placed[_hand_key(row["institution"])] = (
                    float(row["lat"]), float(row["lon"]),
                    row.get("city") or None, row.get("country") or None,
                    _is_yes(row.get("approx")))
                continue
        except ValueError:
            pass
        # Try the organisation itself first; most companies are not mapped
        # features, so fall back to the city they sit in. City precision is
        # honest — it is where the person actually worked — and it is what the
        # main location pass already does for everyone else.
        chain = _chain(
            ", ".join(b for b in (row["institution"], row.get("city"),
                                  row.get("country")) if b),
            ", ".join(b for b in (row.get("city"), row.get("country")) if b),
            row.get("country"),
        )
        if chain:
            queries[tuple(chain)] = row

    if queries:
        resolved = geocode_many([q for chain in queries for q in chain])
        for chain, row in queries.items():
            for q in chain:
                hit = resolved.get(q)
                if hit and hit.get("lat") is not None:
                    placed[_hand_key(row["institution"])] = (
                        hit["lat"], hit["lon"],
                        row.get("city") or None,
                        row.get("country") or _country_en(
                            hit.get("country"), hit.get("country_code")),
                        _is_yes(row.get("approx")))
                    break
            else:
                print(f"  ! could not place {row['institution']!r} — no query resolved")

    # Unlike the automatic fold, a hand-written row may name the institution
    # more briefly than the data does ("FUESMEN" for "FUESMEN - Fundación
    # Escuela de Medicina Nuclear"), so a row matches when its words are a
    # subset of the stop's. The most specific matching row wins, so a short
    # acronym row never shadows a fuller one.
    by_size = sorted(placed.items(), key=lambda kv: -len(kv[0]))

    def _lookup(name, stop_country):
        key = _hand_key(name)
        if not key:
            return None
        for cand, val in by_size:
            if not cand <= key:
                continue
            # Never relocate a stop to a country the data already contradicts:
            # acronyms are reused across the world ("IFT" is a physics
            # institute in both Madrid and São Paulo).
            if stop_country and val[3] and stop_country != val[3]:
                continue
            return val
        return None

    # A hand-written row outranks the geocoder, so it *overrides* rather than
    # merely filling a gap. The bare "Universidad Tecnológica Nacional" rows
    # already had coordinates — wrong ones, pointing at whichever of the thirty
    # regional faculties the geocoder preferred — so a gap-filling pass never
    # reached them.
    filled = 0
    for rec in idx.values():
        for s in rec["career"]:
            if not s.get("institution"):
                continue
            hit = _lookup(s["institution"], s.get("country"))
            if not hit:
                continue
            s["lat"], s["lon"] = hit[0], hit[1]
            s["city"] = hit[2] or s.get("city")
            s["country"] = hit[3] or s.get("country")
            if len(hit) > 4 and hit[4]:
                # the seat, not the campus — say so rather than imply precision
                s["approx"] = True
            filled += 1
    print(f"  placed {filled} career stops from data/institutions.csv "
          f"({len(placed)}/{len(entries)} rows usable)")


def _ror_stop_coords(idx):
    """Last resort for career stops: ask ROR, the research-organisation registry.

    This runs *after* every other method on purpose. ROR matches on names, and
    a name can collide across the world — asking it about "Comisión Nacional de
    Energía Atómica (CNEA)" returns a Chinese nuclear body, which an earlier
    version of this cheerfully believed and moved 19,000 km. Running last means
    ROR only ever fills a genuine gap and can never overwrite a placement some
    better-evidenced method already made.

    The second guard is a country hint. A stop usually knows its own country;
    when it does not, the person's employer and their other stops are strong
    evidence about which "CNEA" is meant. Handing that to ROR filters the
    candidates before any of them can win.
    """
    cache = common._load_ror_cache()
    targets = [(rec, s) for rec in idx.values() for s in rec["career"]
               if s.get("lat") is None and s.get("institution")
               and _norm_inst(s["institution"]) not in NOT_AN_INSTITUTION]
    if not targets:
        return
    print(f"asking ROR about {len({s['institution'] for _, s in targets})} "
          f"institutions nothing else could place ...")

    def _hint(rec, stop):
        if stop.get("country"):
            return stop["country"]
        if rec.get("employer_country"):
            return rec["employer_country"]
        seen = collections.Counter(c["country"] for c in rec["career"]
                                   if c.get("country") and c.get("lat") is not None)
        return seen.most_common(1)[0][0] if seen else None

    found = 0
    for i, (rec, stop) in enumerate(targets, 1):
        hit = ror_lookup(stop["institution"], _hint(rec, stop), cache=cache)
        if hit:
            stop["lat"], stop["lon"] = round(hit["lat"], 5), round(hit["lon"], 5)
            stop["city"] = stop.get("city") or hit.get("city")
            stop["country"] = stop.get("country") or _country_en(hit.get("country"))
            found += 1
        if i % 100 == 0:
            common._save_ror_cache(cache)

    # Correcting what other sources got wrong. INSPIRE records the country of
    # an affiliation, and it is sometimes simply wrong — "Universidad Industrial
    # de Santander, Mexico", "Universidad Nacional de Cuyo, Philippines". The
    # geocoder then faithfully places the university in the wrong country. A
    # registry entry whose *full name* matches is better evidence than a
    # scraped country field, so it is allowed to overrule one. Only exact
    # name matches qualify; a partial one is never enough to move a pin that
    # already exists.
    fixed = 0
    placed_stops = [(rec, s) for rec in idx.values() for s in rec["career"]
                    if s.get("lat") is not None and s.get("institution")
                    and _norm_inst(s["institution"]) not in NOT_AN_INSTITUTION]
    for i, (rec, stop) in enumerate(placed_stops, 1):
        hit = ror_lookup(stop["institution"], None, cache=cache)
        # ...and only when that name belongs to exactly one organisation
        # worldwide. Otherwise the "correction" is a coin flip: it moved 42
        # Universidad Tecnológica Nacional stops from Argentina to the
        # Dominican Republic before this check existed.
        if not hit or hit.get("match") != "exact" or not hit.get("unique"):
            continue
        ror_country = _country_en(hit.get("country"))
        if ror_country and stop.get("country") and ror_country != stop["country"]:
            stop["lat"], stop["lon"] = round(hit["lat"], 5), round(hit["lon"], 5)
            stop["city"], stop["country"] = hit.get("city"), ror_country
            fixed += 1
        if i % 100 == 0:
            common._save_ror_cache(cache)
    common._save_ror_cache(cache)
    print(f"  located {found} more career stops via ROR"
          + (f", corrected {fixed} placed in the wrong country" if fixed else ""))


def _backfill_stop_coords(idx):
    """Give un-geocoded career stops the coordinates of the same institution
    resolved elsewhere in the dataset.

    Nominatim often fails on a bare institution name with no city attached
    ("CONICET", "Instituto Balseiro"), yet those very places are already
    resolved as somebody's current employer. Rather than spend more lookups on
    names we have already placed, copy across by canonical name. Only
    institution-precision locations are reused: a city- or country-level
    fallback carries deliberate jitter and would put the stop in the wrong spot.
    """
    known: dict = {}
    for rec in idx.values():
        if (rec["employer_name"] and rec["lat"] is not None
                and rec.get("loc_precision") == "institution"
                and _norm_inst(rec["employer_name"]) not in NOT_AN_INSTITUTION):
            known.setdefault(_inst_key(rec["employer_name"]),
                             (rec["lat"], rec["lon"],
                              rec["employer_city"], rec["employer_country"]))
    for rec in idx.values():
        for s in rec["career"]:
            if (s.get("lat") is not None and s.get("institution")
                    and _norm_inst(s["institution"]) not in NOT_AN_INSTITUTION):
                known.setdefault(_inst_key(s["institution"]),
                                 (s["lat"], s["lon"], s.get("city"), s.get("country")))

    filled = 0
    for rec in idx.values():
        for s in rec["career"]:
            if s.get("lat") is not None or not s.get("institution"):
                continue
            hit = known.get(_inst_key(s["institution"]))
            if not hit:
                continue
            # Same guard as the hand table: a stop whose own country disagrees
            # with where the institution sits is usually someone working
            # remotely for a foreign employer. Their dot belongs where they
            # are, not at a head office they may never have visited — and
            # since we have no city for them, the honest answer is to leave
            # the stop unplaced rather than move them abroad.
            if s.get("country") and hit[3] and s["country"] != hit[3]:
                continue
            s["lat"], s["lon"] = hit[0], hit[1]
            s["city"] = s.get("city") or hit[2]
            s["country"] = s.get("country") or hit[3]
            filled += 1
    print(f"  backfilled {filled} career stops from institutions already located")


def _nice_role(rec):
    occ = sorted(rec["occupations"])  # sorted -> deterministic across builds
    for pref in ("professor", "physicist", "researcher", "university teacher", "engineer"):
        for o in occ:
            if pref in o.lower():
                return o.capitalize()
    return occ[0] if occ else None


# --------------------------------------------------------------------------- #
def build():
    idx: dict = {}
    by_orcid: dict = {}
    by_name: dict = {}

    blocked = _load_blocklist()

    _merge_wikidata(idx, by_orcid, by_name)
    _merge_orcid(idx, by_orcid, by_name)
    _merge_wikipedia(idx, by_orcid, by_name)
    n_ricabib = _merge_ricabib(idx, by_name)
    n_inspire = _merge_inspire(idx, by_orcid, by_name)
    n_ads = _merge_ads(idx, by_orcid, by_name)
    n_lens = _merge_lens(idx, by_orcid, by_name, blocked)
    n_manual = _merge_csv(idx, by_name, MANUAL_CSV, "manual")
    n_manual += _merge_csv(idx, by_name, FOUND_CSV, "manual")
    n_li_new, n_li_enriched = _merge_linkedin(idx, by_name)

    # OpenAlex: enrich existing people + add high-confidence ORCID-less authors,
    # then (re)write the review queue and fold back any rows already marked keep.
    _enrich_openalex(idx, by_orcid, by_name, blocked)
    _write_review_queue(idx, by_orcid, by_name, blocked)
    n_review = _merge_csv(idx, by_name, REVIEW_CSV, "reviewed", kept_only=True)

    _dedupe_by_initials(idx)

    # honour the blocklist (name keys to always drop)
    if blocked:
        before = len(idx)
        for k in [k for k, v in idx.items() if name_key(v["name"] or "") in blocked]:
            del idx[k]
        print(f"blocklist: removed {before - len(idx)}")

    print(f"merged: {len(idx)} distinct people "
          f"(+{n_ricabib} ricabib, +{n_inspire} inspire, +{n_ads} ads, "
          f"+{n_lens} lens, +{n_manual} manual, +{n_review} reviewed, "
          f"+{n_li_new} linkedin new / {n_li_enriched} enriched)")

    # ---- resolve locations (batch-geocode, first hit in each chain wins) -- #
    for rec in idx.values():
        _resolve_location(rec)
        # "Self-employed" is an answer about employment, not about geography.
        # Geocoding it put one person in Guinea-Bissau and, through the
        # backfill that copies located institutions, eight career stops after
        # her. For these rows only the city can say anything.
        if _norm_inst(rec["employer_name"] or "") in NOT_AN_INSTITUTION:
            rec["_geo_chain"] = _chain(
                ", ".join(b for b in [rec["employer_city"], rec["employer_country"]] if b),
                rec["employer_country"] or "")
    all_queries = [q for rec in idx.values() for q in rec.get("_geo_chain", [])]
    print(f"geocoding {len(set(all_queries))} distinct places "
          f"(fallback chains, city-level allowed) ...")
    resolved = geocode_many(all_queries)
    located_by_chain = 0
    for rec in idx.values():
        rec.setdefault("loc_precision", "institution" if rec["lat"] is not None else None)
        if rec["lat"] is not None:
            continue
        for i, q in enumerate(rec.get("_geo_chain", [])):
            hit = resolved.get(q)
            if hit and hit.get("lat") is not None:
                lat, lon = hit["lat"], hit["lon"]
                # first query = the institution itself; later = city/country only,
                # so nudge those apart with a deterministic sub-degree jitter.
                if i > 0:
                    h = int(hashlib.sha1((rec["name"] or q).encode()).hexdigest(), 16)
                    span = 0.06 if i < len(rec["_geo_chain"]) - 1 else 0.9
                    lat += ((h & 0xFFFF) / 0xFFFF - 0.5) * span
                    lon += (((h >> 16) & 0xFFFF) / 0xFFFF - 0.5) * span
                    rec["loc_precision"] = "city" if i < len(rec["_geo_chain"]) - 1 else "country"
                else:
                    rec["loc_precision"] = "institution"
                rec["lat"], rec["lon"] = lat, lon
                rec["employer_country"] = rec["employer_country"] or _country_en(
                    hit.get("country"), hit.get("country_code"))
                rec["employer_country_code"] = rec["employer_country_code"] or hit.get("country_code")
                located_by_chain += 1
                break
    print(f"  located {located_by_chain} more via geocoding")

    # ---- career-history stops: merge INSPIRE + ORCID employment records,
    # dedupe, and geocode any stop still missing coordinates, so the site can
    # draw a person's full trajectory (not just their current location).
    # Resolved sequentially (institution+city+country, else institution+
    # country) with an early exit per stop -- unlike the person-level chain
    # above, a career waypoint that only resolves to a bare country centroid
    # isn't useful on a trajectory line, so we don't fall back that far.
    #
    # There are hundreds of institutions here nobody has ever looked up
    # before, so (like the OpenAlex enrichment pass) this is spread over
    # several pipeline runs instead of done in one long burst against a free
    # shared geocoder: each run only makes CAREER_GEOCODE_BUDGET *new*
    # requests and leaves the rest for next time (already-cached stops are
    # always resolved, budget or not). Re-run build_dataset.py on subsequent
    # days to keep backfilling until "0 new lookups" is printed. --------- #
    CAREER_GEOCODE_BUDGET = 150
    for rec in idx.values():
        rec["career"] = _merge_career(rec)
    geo_cache = common._load_geocache()
    # INSPIRE-only stops carry no city/country (only its *current* institution
    # gets that, resolved separately above) -- geocoding an abbreviated,
    # context-free name like "Cuyo U." on its own is a coin flip (Nominatim
    # matched it to the Cuyo Islands, Philippines, not Univ. Nacional de
    # Cuyo). Require at least a city or country already known (from ORCID's
    # structured address, or a fuller INSPIRE institution record) before
    # attempting a lookup; otherwise leave the stop undotted rather than risk
    # a wrong-country match on the trajectory line.
    # An institution name on its own is worth a lookup: universities are mapped
    # places, and the pre-Balseiro education stops arrive with nothing else.
    all_stops = [(rec, stop) for rec in idx.values() for stop in rec["career"]
                 if stop.get("lat") is None
                 and (stop.get("city") or stop.get("country") or stop.get("institution"))]
    print(f"geocoding up to {len(all_stops)} career-history stops "
          f"(cached lookups are instant; capped at {CAREER_GEOCODE_BUDGET} new ones this run)...")
    n_stop_geo = new_lookups = done = 0
    for rec, stop in all_stops:
        # "Self-employed" is not a place. Geocoding it put eight stops in
        # Guinea-Bissau; for these rows only the city can say anything.
        usable_inst = (stop.get("institution")
                       if _norm_inst(stop.get("institution") or "") not in NOT_AN_INSTITUTION
                       else None)
        city_query = ", ".join(b for b in [stop.get("city"), stop.get("country")] if b)
        for q in _chain(
                ", ".join(b for b in [usable_inst, stop.get("city"), stop.get("country")] if b),
                ", ".join(b for b in [usable_inst, stop.get("country")] if b) if stop.get("country") else "",
                usable_inst or "",
                # The employer may be a company OSM has never heard of, but we
                # often know the city it sat in. Falling back to that places
                # the stop where the person actually was, which is all a
                # trajectory dot needs — and it costs nothing.
                city_query):
            is_new = q not in geo_cache
            if is_new and new_lookups >= CAREER_GEOCODE_BUDGET:
                continue  # leave it for the next run; nothing wasted
            hit = geocode(q, cache=geo_cache)
            new_lookups += is_new
            # A bare institution name matches anything, anywhere: "Vanwa" in
            # Mendoza landed in Vancouver, "Rucon" in Buenos Aires Province in
            # Slovakia. When the stop already states its country, a hit that
            # disagrees is wrong by definition — skip it and try the next,
            # looser query rather than believing it.
            hit_country = _country_en(hit.get("country"), hit.get("country_code")) if hit else None
            if hit and stop.get("country") and hit_country and hit_country != stop["country"]:
                # The city is more specific evidence than the country field,
                # which LinkedIn fills with where the *person* is: a job at
                # Equifax in Atlanta arrives labelled Argentina. When the
                # winning query named the city, believe the city and relabel.
                if stop.get("city") and stop["city"].lower() in q.lower():
                    stop["country"] = hit_country
                else:
                    continue
            if hit and hit.get("lat") is not None:
                stop["lat"], stop["lon"] = round(hit["lat"], 5), round(hit["lon"], 5)
                stop["country"] = stop.get("country") or _country_en(
                    hit.get("country"), hit.get("country_code"))
                n_stop_geo += 1
                break
        done += 1
        if done % 25 == 0:
            common._save_geocache(geo_cache)
    common._save_geocache(geo_cache)
    print(f"  located {n_stop_geo} career-history stops ({new_lookups} new lookups this run"
          + (", budget reached -- rerun to continue)" if new_lookups >= CAREER_GEOCODE_BUDGET else ")"))

    # ---- fold employer spelling variants -----------------------------------#
    # after geocoding: both spellings resolve to the same place anyway, and the
    # cache means nothing is re-fetched
    _canonicalise_employers(idx)
    # both need the canonical names, so they run after the fold; the hand table
    # is authoritative and goes first
    _hand_placed_institutions(idx)
    _backfill_stop_coords(idx)
    _ror_stop_coords(idx)

    # ---- normalise country names -------------------------------------------#
    # last line of defence: a country may also arrive already-named from ORCID,
    # INSPIRE or a hand-edited CSV, so fold every one of them, not just the
    # geocoded ones.
    for rec in idx.values():
        rec["employer_country"] = _country_en(rec["employer_country"],
                                              rec["employer_country_code"])
        for stop in rec["career"]:
            stop["country"] = _country_en(stop.get("country"),
                                          stop.get("country_code"))

    # ---- finalise records --------------------------------------------------#
    out = []
    for rec in idx.values():
        degree_blob = " ".join(rec["degrees"]) + " " + (rec["description"] or "") \
            + " " + (rec["thesis_title"] or "")
        program = rec["degree_program"] or _classify(degree_blob, DEGREE_PROGRAM_RULES)
        levels = _classify_all(degree_blob, DEGREE_LEVEL_RULES)

        # Classify the research field, most trustworthy signal first:
        # human-written text -> degree -> (filtered) OpenAlex concepts ->
        # employer name -> coarse bucket from the Balseiro degree.
        human_blob = " ".join([
            " ".join(rec["fields"]), " ".join(rec["keywords"]),
            " ".join(rec["occupations"]), rec["description"] or "",
            rec["biography"] or "", rec["thesis_title"] or "",
        ])
        discipline = (
            _classify(human_blob, DISCIPLINE_RULES, default=None)
            or _classify(" ".join(rec["degrees"]), DISCIPLINE_RULES, default=None)
            or _classify(" ".join(sorted(rec["concepts"])), DISCIPLINE_RULES, default=None)
            or _classify(" ".join(sorted(rec["arxiv_categories"])), ARXIV_DISCIPLINE_RULES, default=None)
            or _classify(rec["employer_name"] or "", DISCIPLINE_RULES, default=None)
            or PROGRAM_TO_DISCIPLINE.get(program, "Not specified")
        )

        sector = _classify(rec["employer_name"] or "", SECTOR_RULES, default=None)

        country = rec["employer_country"]
        if not country and rec["employer_country_code"]:
            country = COUNTRY_BY_CODE.get(rec["employer_country_code"])

        gy = rec["grad_year"]
        try:
            gy = int(gy) if gy else None
        except (TypeError, ValueError):
            gy = None

        out.append({
            "name": rec["name"],
            "aka": sorted(rec["aka"]) or None,
            "description": rec["description"],
            "role": rec["role"] or _nice_role(rec),
            "employer": rec["employer_name"],
            "city": rec["employer_city"],
            "country": country,
            "lat": round(rec["lat"], 5) if rec["lat"] is not None else None,
            "lon": round(rec["lon"], 5) if rec["lon"] is not None else None,
            "loc_precision": rec.get("loc_precision"),
            "loc_asof": rec["loc_asof"] if rec["loc_asof"] == "manual" else _asof(rec["loc_asof"]),
            "discipline": discipline,
            "sector": sector,
            "program": program,
            "levels": levels or None,
            "grad_year": gy,
            "grad_decade": (gy // 10 * 10) if gy else None,
            "birth_year": rec["birth_year"],
            "deceased": bool(rec["death_year"]),
            "keywords": sorted(rec["keywords"])[:12] or None,
            "skills": rec["skills"][:15] or None,
            "languages": sorted(rec["languages"]) or None,
            "linkedin": rec["linkedin_url"] if PUBLISH_LINKEDIN_URLS else None,
            "fields": sorted(rec["fields"]) or None,
            "concepts": sorted(rec["concepts"])[:6] or None,
            "works_count": rec["works_count"],
            "h_index": rec["h_index"],
            "thesis": ({"title": rec["thesis_title"], "year": rec["thesis_year"]}
                       if rec["thesis_title"] else None),
            "career": [dict(c, kind=c.get("kind") or "job") for c in rec["career"]] or None,
            "advisors": sorted(rec["advisors"]) or None,
            "confidence": ("confirmed" if rec["wikidata_alumnus"] or rec["ib_trained"]
                           or {"orcid", "wikipedia", "manual", "ricabib", "reviewed"} & rec["sources"]
                           else "inferred"),
            "orcid": rec["orcid"],
            "scholar_id": rec["scholar_id"],
            "wikipedia": rec["wikipedia"],
            "wikidata": rec["wikidata_url"],
            "image": rec["image"],
            "urls": sorted(rec["urls"]) or None,
            "sources": sorted(rec["sources"]),
            "located": rec["lat"] is not None,
        })

    out.sort(key=lambda p: (not p["located"], p["name"].lower()))
    located = [p for p in out if p["located"]]
    countries = sorted({p["country"] for p in located if p["country"]})

    payload = {
        "meta": {
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "institution": "Instituto Balseiro (Bariloche, Argentina)",
            "total": len(out),
            "located": len(located),
            "countries": len(countries),
            "confirmed": sum(1 for p in out if p["confidence"] == "confirmed"),
            "sources": {
                s: sum(1 for p in out if s in p["sources"])
                for s in ("wikidata", "orcid", "openalex", "reviewed", "inspire",
                          "ads", "lens", "wikipedia", "ricabib", "manual", "linkedin")
            },
            "disclaimer": (
                "Compiled automatically from public data (Wikidata, ORCID, "
                "OpenAlex, Wikipedia). Coverage is partial and skewed toward "
                "people with an academic/research web presence. 'Inferred' "
                "entries come from OpenAlex affiliation data and are less "
                "certain. Locations are the current or most recent employer on "
                "record and may be out of date."
            ),
        },
        "alumni": out,
    }
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / "alumni.json").write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (SITE_DATA / "alumni.pretty.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote site/data/alumni.json  —  {len(out)} people, "
          f"{len(located)} on the map, {len(countries)} countries")


if __name__ == "__main__":
    build()
