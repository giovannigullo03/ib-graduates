"""Find Balseiro alumni in the industry diaspora via patents (Lens.org).

Academic sources miss the graduates who went into industry -- but many of them
appear on patents. This collector doesn't try to search "Balseiro alumni"
(patents don't record that); instead it pulls patents filed by organisations
that are overwhelmingly staffed by Balseiro people:

  * INVAP           - the Bariloche high-tech company founded by IB graduates
  * CNEA / CONICET  - Argentina's atomic-energy commission & research council
  * Instituto Balseiro itself

and treats their inventors as likely alumni (confidence: inferred), recording
the assignee as their employer and their residence country as their location.

*** Requires a free Lens.org API token. *** Register for "Lens.org for
Researchers" at https://www.lens.org, then Profile -> Toolkit -> Access Tokens.
Put it in the environment (preferred) or a gitignored file:

    export LENS_TOKEN=xxxxxxxx           # or: data/lens_token.txt

Never commit the token (this repo is public); regenerate it in Lens if it leaks.
Without a token this collector does nothing.

Output: data/raw/lens_inventors.json
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from common import RAW, DATA, SESSION, strip_accents

API = "https://api.lens.org/patent/search"

# Kept deliberately short — the Lens free research tier is request-limited.
# INVAP + Instituto Balseiro + CAB are small and Balseiro-dominated (new
# inventors there are treated as likely alumni); CNEA/CONICET are broad so
# their inventors only enrich people already found elsewhere.
APPLICANTS = [
    "INVAP",
    "Instituto Balseiro",
    "Centro Atomico Bariloche",
    "Comision Nacional de Energia Atomica",
    "Consejo Nacional de Investigaciones Cientificas y Tecnicas",
]

COUNTRY_BY_CODE = {
    "AR": "Argentina", "US": "United States", "BR": "Brazil", "DE": "Germany",
    "ES": "Spain", "FR": "France", "GB": "United Kingdom", "NL": "Netherlands",
    "CH": "Switzerland", "IT": "Italy", "CA": "Canada", "MX": "Mexico",
    "CL": "Chile", "SE": "Sweden", "BE": "Belgium", "AU": "Australia",
}


_ORG_CANON = [
    (r"invap", "INVAP"),
    (r"energ[ií]a at[oó]mica|atomic energy|\bcnea\b", "Comisión Nacional de Energía Atómica (CNEA)"),
    (r"investigaciones cient|\bconicet\b", "CONICET"),
    (r"balseiro", "Instituto Balseiro"),
    (r"centro at[oó]mico bariloche", "Centro Atómico Bariloche"),
    (r"y-?tec", "Y-TEC"),
    (r"satellogic", "Satellogic"),
    (r"\bveng\b", "VENG"),
]


def _canonical_org(name: str) -> str:
    low = strip_accents(name or "").lower()
    for pat, canon in _ORG_CANON:
        if re.search(pat, low):
            return canon
    return re.sub(r"\s+", " ", name or "").strip()


def _matches_org(applicant: dict) -> bool:
    name = strip_accents(applicant.get("extracted_name", {}).get("value", "")).lower()
    return any(re.search(pat, name) for pat, _ in _ORG_CANON)


def _token() -> str | None:
    tok = os.environ.get("LENS_TOKEN", "").strip()
    if tok:
        return tok
    f = DATA / "lens_token.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _flip(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip().strip(",")
    if "," in name:
        fam, giv = name.split(",", 1)
        name = f"{giv.strip()} {fam.strip()}"
    name = re.sub(r"\s+", " ", name).strip()
    # patent inventor names are ALL CAPS -> Title Case
    if name.isupper():
        name = name.title().replace("De ", "de ").replace(" Y ", " y ")
    return name


def _clean_inventor(name: str) -> str:
    # some records append the institution: "AHLERS, MANFRED ... CENTRO ATOMICO BARILOCHE"
    n = re.sub(r"\s+(centro atomico|comision nacional|instituto|invap|conicet|cnea)\b.*$",
               "", name or "", flags=re.I)
    return _flip(n)


def _looks_like_person(name: str) -> bool:
    n = strip_accents(name or "")
    if not n or len(n.split()) < 2:
        return False
    org_words = ("inc", "ltd", "llc", "gmbh", "s.a", "corp", "comision",
                 "university", "universidad", "institut", "consejo", "company",
                 "laborator", "technolog", "invap", "cnea", "conicet", "etablissement",
                 "s.r.l", "sociedad", "establishment")
    return not any(w in n.lower() for w in org_words)


def collect() -> list[dict]:
    tok = _token()
    if not tok:
        print("Lens: no API token (set LENS_TOKEN or create data/lens_token.txt) — "
              "skipping. See scripts/collect_lens.py header.")
        (RAW / "lens_inventors.json").write_text("[]", encoding="utf-8")
        return []

    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    people: dict[str, dict] = {}
    for applicant in APPLICANTS:
        body = {
            "query": {"match_phrase": {"applicant.name": applicant}},
            "size": 100, "scroll": "1m",
            "include": ["lens_id", "biblio.invention_title", "biblio.parties"],
        }
        scroll_id = None
        for pg in range(15):
            payload = {"scroll_id": scroll_id} if scroll_id else body
            cache_f = RAW / ("lens_%s.json" % hashlib.sha1(
                f"{applicant}|{pg}".encode()).hexdigest()[:16])
            if cache_f.exists() and time.time() - cache_f.stat().st_mtime < 21 * 86400:
                data = json.loads(cache_f.read_text(encoding="utf-8"))
            else:
                try:
                    r = SESSION.post(API, headers=headers, json=payload, timeout=45)
                    if r.status_code == 429:
                        print("  Lens rate limit — stopping (cached progress kept)")
                        return _finish(people)
                    r.raise_for_status()
                    data = r.json()
                    cache_f.write_text(json.dumps(data), encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    body_txt = getattr(getattr(exc, "response", None), "text", "")
                    print(f"  Lens query {applicant!r} failed: {exc} {body_txt[:150]}")
                    break
                time.sleep(1.5)
            hits = data.get("data") or []
            for pat in hits:
                parties = ((pat.get("biblio") or {}).get("parties") or {})
                assignees = [a.get("extracted_name", {}).get("value")
                             for a in parties.get("applicants", []) if _matches_org(a)]
                assignee = next((a for a in assignees if a), applicant)
                for inv in parties.get("inventors", []):
                    nm = inv.get("extracted_name", {}).get("value")
                    if not nm or not _looks_like_person(nm):
                        continue
                    cc = (inv.get("residence") or "").upper()[:2]
                    disp = _clean_inventor(nm)
                    key = strip_accents(disp).lower()
                    rec = people.setdefault(key, {
                        "name": disp, "employer": _canonical_org(assignee),
                        "country_code": cc or None, "country": COUNTRY_BY_CODE.get(cc),
                        "via_applicant": applicant, "patent_count": 0,
                    })
                    rec["patent_count"] += 1
                    if cc and not rec["country_code"]:
                        rec["country_code"], rec["country"] = cc, COUNTRY_BY_CODE.get(cc)
            scroll_id = data.get("scroll_id")
            if not scroll_id or len(hits) < 100:
                break
            time.sleep(1)
        print(f"  Lens[{applicant}]: {len(people)} distinct inventors so far")

    return _finish(people)


def _finish(people: dict) -> list[dict]:
    # collapse "Daniel E Balzaretti" / "Daniel Eduardo Balzaretti" -> the fuller one
    merged: dict[tuple, dict] = {}
    for p in people.values():
        toks = strip_accents(p["name"]).lower().split()
        k = (toks[0][:4], toks[-1]) if len(toks) >= 2 else (p["name"].lower(),)
        cur = merged.get(k)
        if cur is None or len(p["name"]) > len(cur["name"]):
            if cur:
                p["patent_count"] += cur["patent_count"]
                p["country_code"] = p["country_code"] or cur["country_code"]
                p["country"] = p["country"] or cur["country"]
            merged[k] = p
        else:
            cur["patent_count"] += p["patent_count"]
            cur["country_code"] = cur["country_code"] or p["country_code"]
            cur["country"] = cur["country"] or p["country"]

    out = sorted(merged.values(), key=lambda p: -p["patent_count"])
    (RAW / "lens_inventors.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Lens: {len(out)} distinct inventors on patents from Balseiro-linked orgs "
          f"({sum(1 for p in out if p['country_code'])} with a residence country)")
    return out


if __name__ == "__main__":
    collect()
