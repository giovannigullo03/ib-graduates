"""Shared helpers: HTTP with on-disk cache, Nominatim geocoding, name utilities."""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
SITE_DATA = ROOT / "site" / "data"
RAW.mkdir(parents=True, exist_ok=True)
SITE_DATA.mkdir(parents=True, exist_ok=True)

USER_AGENT = "BalseiroAlumniMap/1.0 (personal research project; contact fraanco.borgarello@gmail.com)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


class RateLimitExceeded(RuntimeError):
    """A hard quota was hit (e.g. OpenAlex's daily budget). Stop, resume later."""

    def __init__(self, service: str, retry_after: int | None = None):
        self.service = service
        self.retry_after = retry_after
        super().__init__(f"{service} rate limit / quota exceeded"
                         + (f"; retry after ~{retry_after}s" if retry_after else ""))

# Instituto Balseiro identifiers across sources.
WIKIDATA_QID = "Q3151718"
BALSEIRO_NAME_PATTERNS = [
    "instituto balseiro",
    "balseiro institute",
    "institut balseiro",   # fr / common misspelling
    "centro atomico bariloche",
    "bariloche atomic",
    # CNEA only counts when it's the Bariloche site (CNEA also has
    # Constituyentes and Ezeiza, which are not Balseiro):
    ("energia atomica", "bariloche"),
]


def _matches_balseiro_text(text: str) -> bool:
    from unicodedata import normalize, combining
    t = "".join(c for c in normalize("NFKD", text or "") if not combining(c)).lower()
    for p in BALSEIRO_NAME_PATTERNS:
        if isinstance(p, tuple):
            if all(x in t for x in p):
                return True
        elif p in t:
            return True
    return False

_last_call: dict[str, float] = {}


def _throttle(key: str, min_interval: float) -> None:
    now = time.monotonic()
    wait = min_interval - (now - _last_call.get(key, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.monotonic()


def cached_get(url, *, params=None, headers=None, cache_key=None,
               min_interval=1.0, throttle_key="default", ttl_days=30,
               expect="json"):
    """GET with a persistent cache under data/raw/. Returns parsed JSON or text."""
    if cache_key:
        key_src = cache_key
    elif params:
        key_src = url + "?" + json.dumps(params, sort_keys=True)
    else:
        key_src = url
    digest = hashlib.sha1(key_src.encode()).hexdigest()[:16]
    ext = "json" if expect == "json" else "txt"
    path = RAW / f"{throttle_key}_{digest}.{ext}"

    if path.exists() and (time.time() - path.stat().st_mtime) < ttl_days * 86400:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw) if expect == "json" else raw

    _throttle(throttle_key, min_interval)
    hdrs = dict(headers or {})
    if expect == "json":
        hdrs.setdefault("Accept", "application/json")
    for attempt in range(4):
        try:
            resp = SESSION.get(url, params=params, headers=hdrs, timeout=30)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("retry-after", 0) or 0)
                # a long retry-after or a "budget" body = hard quota, not a
                # transient burst -> bail out so the caller can resume later.
                if retry_after > 120 or "budget" in resp.text[:400].lower():
                    raise RateLimitExceeded(throttle_key, retry_after or None)
                time.sleep(min(2 ** attempt * 2, 20))
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt * 2)
    else:  # pragma: no cover
        raise RuntimeError(f"failed: {url}")

    if expect == "json":
        data = resp.json()
        path.write_text(json.dumps(data), encoding="utf-8")
        return data
    path.write_text(resp.text, encoding="utf-8")
    return resp.text


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #
_GEOCODE_CACHE = DATA / "geocode_cache.json"


def _load_geocache() -> dict:
    if _GEOCODE_CACHE.exists():
        return json.loads(_GEOCODE_CACHE.read_text(encoding="utf-8"))
    return {}


def _save_geocache(cache: dict) -> None:
    _GEOCODE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")


def geocode(query: str, *, cache: dict | None = None) -> dict | None:
    """Resolve a place/institution string to {lat, lon, display_name, country}.

    Uses a local cache first, then Nominatim (1 req/s, per their usage policy).
    Returns None when nothing is found; the miss is cached so we do not retry.
    """
    own_cache = cache is None
    cache = cache if cache is not None else _load_geocache()
    q = re.sub(r"\s+", " ", query).strip()
    if not q:
        return None
    if q in cache:
        hit = cache[q]
        if own_cache:
            pass
        return hit or None

    result = None
    try:
        data = cached_get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            throttle_key="nominatim", min_interval=1.1, ttl_days=180,
        )
        if data:
            top = data[0]
            result = {
                "lat": float(top["lat"]),
                "lon": float(top["lon"]),
                "display_name": top.get("display_name"),
                "country": (top.get("address") or {}).get("country"),
                "country_code": ((top.get("address") or {}).get("country_code") or "").upper(),
            }
    except Exception as exc:  # noqa: BLE001 - geocoding is best-effort
        print(f"  ! geocode error for {q!r}: {exc}")
        result = None

    cache[q] = result or {}
    if own_cache:
        _save_geocache(cache)
    return result


def geocode_many(queries, progress_every=25):
    """Geocode an iterable of strings, sharing one cache file."""
    cache = _load_geocache()
    out = {}
    todo = list(dict.fromkeys(queries))
    for i, q in enumerate(todo, 1):
        out[q] = geocode(q, cache=cache)
        if i % progress_every == 0:
            print(f"  geocoded {i}/{len(todo)}")
            _save_geocache(cache)
    _save_geocache(cache)
    return out


# --------------------------------------------------------------------------- #
# Name helpers (for dedupe across sources)
# --------------------------------------------------------------------------- #
def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


_MOJIBAKE = {"ı": "i", "İ": "i", "ﬁ": "fi", "ﬂ": "fl"}


def _clean_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    # OpenAlex sometimes stores "Garci ́a" (letter, space, combining accent);
    # pull the mark back onto its letter before we normalise.
    text = re.sub(r"\s+([̀-ͯ])", r"\1", text)
    return unicodedata.normalize("NFKC", text)


_NAME_STOP = {"de", "del", "la", "el", "van", "von", "da", "do", "dos", "der", "den"}


def _name_parts(name: str) -> list[str]:
    n = strip_accents(_clean_unicode(name)).lower()
    n = re.sub(r"[^a-z\s-]", " ", n)
    return [p for p in re.split(r"[\s-]+", n) if p and p not in _NAME_STOP]


def name_key(name: str) -> str:
    return " ".join(sorted(_name_parts(name)))


def initial_key(name: str) -> str:
    """A looser key that unifies "A. Baruj" / "Alberto G. Baruj" / "Baruj, Alberto".

    = the surname (last token, or the part before a comma) + the sorted initials
    of the given names. Ambiguous by design (Spanish double surnames, common
    surnames) so callers MUST guard against collisions.
    """
    raw = strip_accents(_clean_unicode(name)).lower().strip()
    if "," in raw:
        surname_src, given_src = raw.split(",", 1)
    else:
        toks = [t for t in re.split(r"[^a-z]+", raw) if t and t not in _NAME_STOP]
        if not toks:
            return ""
        surname_src, given_src = toks[-1], " ".join(toks[:-1])
    surname = "".join(re.findall(r"[a-z]+", surname_src))
    initials = sorted(t[0] for t in re.split(r"[^a-z]+", given_src) if t)
    if not surname:
        return ""
    return surname + "|" + "".join(initials)


def is_initials_form(name: str) -> bool:
    parts = _name_parts(name)
    long = [p for p in parts if len(p) > 1]
    return len(parts) >= 2 and len(long) <= 1


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).title() if (name or "").isupper() else re.sub(r"\s+", " ", (name or "").strip())


# --------------------------------------------------------------------------- #
# ROR — Research Organization Registry
# --------------------------------------------------------------------------- #
# Most places this project fails to locate are institutions, not addresses, and
# an address geocoder is the wrong tool for them: it returns a city centroid at
# best and nonsense at worst. ROR is a free, open registry of ~100k research
# organisations with coordinates, so it answers the institution question
# directly and precisely.
#
# The catch is that ROR's search is fuzzy and *always* returns something —
# "Rather Labs" comes back as a laboratory in Bologna. Everything below the
# request is about refusing bad matches.
_ROR_CACHE = DATA / "ror_cache.json"

# Words that say "this is an organisation" rather than *which* organisation.
_ORG_STOP = {
    "de", "del", "la", "el", "los", "las", "y", "of", "the", "for", "and", "in",
    "at", "a", "en", "der", "die", "das", "und", "di", "da", "do", "des",
    "universidad", "university", "universite", "universitat", "universita",
    "universiteit", "universidade", "univ", "instituto", "institute", "institut",
    "instituut", "inst", "centro", "center", "centre", "zentrum", "national",
    "nacional", "nationale", "research", "investigaciones", "laboratory",
    "laboratoire", "laboratorio", "lab", "labs", "sa", "se", "srl", "ltd",
    "inc", "gmbh", "llc", "college", "school", "escuela", "facultad", "faculty",
    "department", "departamento", "dept", "group", "grupo", "foundation",
    "fundacion", "academy", "academia", "society", "council", "consejo",
}


# Words that are distinctive enough to survive _ORG_STOP but still say nothing
# about *which* organisation: an overlap made only of these is not evidence.
# "Family Business" matched a Glasgow trade body, and "CANDOIT Engineering &
# Technology" matched a Peruvian university, on exactly this.
_ORG_GENERIC = {
    "business", "technology", "technologies", "tecnologia", "tecnologica",
    "engineering", "ingenieria", "science", "sciences", "ciencias", "cientificas",
    "energy", "energia", "nuclear", "systems", "sistemas", "solutions",
    "services", "servicios", "industrial", "industria", "industries",
    "automation", "global", "international", "internacional", "superior",
    "tecnico", "tecnica", "technical", "applied", "aplicada", "aplicadas",
    "advanced", "studies", "estudios", "development", "desarrollo", "innovation",
    "innovacion", "management", "consulting", "consultora", "digital", "data",
    "software", "computing", "medicine", "medicina", "health", "salud",
    "physics", "fisica", "chemistry", "quimica", "materials", "materiales",
    "mathematics", "matematica", "biology", "biologia", "regional", "general",
}


def _org_tokens(name: str, distinctive: bool = True) -> frozenset:
    s = strip_accents(_clean_unicode(name or "")).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    toks = [t for t in s.split() if len(t) > 1]
    if distinctive:
        toks = [t for t in toks if t not in _ORG_STOP]
    return frozenset(toks)


def _load_ror_cache() -> dict:
    if _ROR_CACHE.exists():
        return json.loads(_ROR_CACHE.read_text(encoding="utf-8"))
    return {}


def _save_ror_cache(cache: dict) -> None:
    _ROR_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")


def _ror_match(query: str, names: list[str]) -> str | None:
    """How well an ROR record's names match the string we asked about.

    "exact"  — the full word sets agree; the strongest evidence there is.
    "partial"— the distinctive words of one contain those of the other, so
               "Facultad de Ingeniería Química Universidad Nacional del
               Litoral" still reaches its parent university.
    None     — no relation worth acting on.
    """
    q_dist, q_full = _org_tokens(query), _org_tokens(query, distinctive=False)
    if not q_dist:
        return None
    best = None
    for n in names:
        c_dist, c_full = _org_tokens(n), _org_tokens(n, distinctive=False)
        if not c_dist:
            continue
        if q_full == c_full:
            return "exact"
        # a containment relation alone is not enough: what the two names share
        # has to include something that actually names an organisation
        if (q_dist <= c_dist or c_dist <= q_dist) and (q_dist & c_dist) - _ORG_GENERIC:
            best = "partial"
    return best


def ror_lookup(name: str, country: str | None = None, *, cache: dict | None = None):
    """Resolve an institution name to {lat, lon, city, country, ror_id}.

    Returns None when nothing matches confidently; the miss is cached so the
    same name is not asked about twice.
    """
    own = cache is None
    cache = cache if cache is not None else _load_ror_cache()
    q = re.sub(r"\s+", " ", (name or "")).strip()
    if not q:
        return None
    key = f"{q}||{country or ''}"
    if key in cache:
        return cache[key] or None

    result = None
    try:
        data = cached_get("https://api.ror.org/v2/organizations",
                          params={"query": q}, throttle_key="ror",
                          min_interval=0.25, ttl_days=365)
        exact, partial, exact_ids = None, [], set()
        for item in (data.get("items") or [])[:5]:
            names = [n.get("value") for n in item.get("names", []) if n.get("value")]
            loc = ((item.get("locations") or [{}])[0].get("geonames_details") or {})
            lat, lng = loc.get("lat"), loc.get("lng")
            if lat is None or lng is None:
                continue
            cand_country = loc.get("country_name")
            # a country we already know is the cheapest way to throw out the
            # wrong Córdoba, the wrong Cambridge, the wrong Santiago
            if country and cand_country and country != cand_country:
                continue
            hit = {
                "lat": float(lat), "lon": float(lng),
                "city": loc.get("name"), "country": cand_country,
                "ror_id": item.get("id"), "name": names[0] if names else None,
            }
            kind = _ror_match(q, names)
            hit["match"] = kind
            if kind == "exact":
                exact_ids.add(item.get("id"))
                if exact is None:
                    exact = hit
            elif kind == "partial":
                partial.append(hit)
        # An exact name match wins. Failing that a partial one is only safe
        # when it is the *only* candidate that fits: two organisations sharing
        # the distinctive word ("Córdoba") means we cannot tell them apart, and
        # guessing would put someone on the wrong continent.
        if exact is not None:
            # An exact name match is not the same as a unique one: several
            # countries have a "Universidad Tecnológica Nacional" and an
            # "École normale supérieure". Callers that intend to overrule
            # existing data check this before trusting the answer.
            exact["unique"] = len(exact_ids) == 1
            result = exact
        elif len({h["ror_id"] for h in partial}) == 1:
            result = partial[0]
    except Exception as exc:  # noqa: BLE001 - best effort, like geocoding
        print(f"  ! ROR error for {q!r}: {exc}")
        result = None

    cache[key] = result or {}
    if own:
        _save_ror_cache(cache)
    return result
