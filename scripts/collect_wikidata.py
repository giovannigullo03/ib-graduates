"""Collect Balseiro-linked people from Wikidata.

Three relations tie a person to the institute (Q3151718):
  P69  educated at   -> alumnus (what we mainly want)
  P108 employer      -> often an alumnus now on staff
  P1416 affiliation  -> looser

Each relation is queried separately (one small query each is far faster and
more reliable on the public endpoint than one big UNION). Results are merged;
`is_alumnus` records whether P69 applied.

Output: data/raw/wikidata_alumni.json
"""
from __future__ import annotations

import json

from common import RAW, WIKIDATA_QID, cached_get

RELATIONS = [("P69", "educated_at"), ("P108", "employer"), ("P1416", "affiliation")]


def _query(pid: str) -> str:
    return f"""
SELECT ?person ?personLabel ?personDescription ?article
       ?dob ?dod ?image ?orcid ?scholar
       (GROUP_CONCAT(DISTINCT ?occLabel; separator=" | ") AS ?occs)
       (GROUP_CONCAT(DISTINCT ?fieldLabel; separator=" | ") AS ?fields)
       (GROUP_CONCAT(DISTINCT ?degLabel; separator=" | ") AS ?degrees)
       (GROUP_CONCAT(DISTINCT ?emp; separator=" | ") AS ?employers)
WHERE {{
  ?person wdt:P31 wd:Q5 ; wdt:{pid} wd:{WIKIDATA_QID} .
  OPTIONAL {{ ?person wdt:P569 ?dob }}
  OPTIONAL {{ ?person wdt:P570 ?dod }}
  OPTIONAL {{ ?person wdt:P18 ?image }}
  OPTIONAL {{ ?person wdt:P496 ?orcid }}
  OPTIONAL {{ ?person wdt:P1960 ?scholar }}
  OPTIONAL {{ ?person wdt:P512 ?deg . ?deg rdfs:label ?degLabel . FILTER(LANG(?degLabel)="en") }}
  OPTIONAL {{ ?person wdt:P106 ?occ . ?occ rdfs:label ?occLabel . FILTER(LANG(?occLabel)="en") }}
  OPTIONAL {{ ?person wdt:P101 ?field . ?field rdfs:label ?fieldLabel . FILTER(LANG(?fieldLabel)="en") }}
  OPTIONAL {{
    ?person wdt:P108 ?employerItem .
    ?employerItem rdfs:label ?employerItemLabel . FILTER(LANG(?employerItemLabel)="en")
    OPTIONAL {{ ?employerItem wdt:P625 ?employerCoord }}
    OPTIONAL {{ ?employerItem wdt:P17 ?ec . ?ec rdfs:label ?ecl . FILTER(LANG(?ecl)="en") }}
    BIND(CONCAT(?employerItemLabel, "@@", COALESCE(STR(?employerCoord), ""), "@@",
               COALESCE(?ecl, "")) AS ?emp)
  }}
  OPTIONAL {{ ?article schema:about ?person ; schema:isPartOf <https://en.wikipedia.org/> }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,es" }}
}}
GROUP BY ?person ?personLabel ?personDescription ?article ?dob ?dod ?image ?orcid ?scholar
"""


def _year(iso):
    if not iso:
        return None
    try:
        return int(iso[:4]) if iso[0] != "-" else -int(iso[1:5])
    except ValueError:
        return None


def _parse_point(text):
    try:
        lon, lat = text.strip()[6:-1].split()
        return float(lat), float(lon)
    except Exception:
        return None


def _run(pid: str):
    data = cached_get(
        "https://query.wikidata.org/sparql",
        params={"query": _query(pid), "format": "json"},
        throttle_key="wikidata", min_interval=2.0, ttl_days=14,
        cache_key=f"wd-rel::{pid}",
    )
    return data["results"]["bindings"]


def collect() -> list[dict]:
    people: dict[str, dict] = {}
    for pid, relabel in RELATIONS:
        print(f"Wikidata: querying {pid} ({relabel}) ...")
        try:
            rows = _run(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {pid} failed: {exc}")
            continue
        for r in rows:
            def g(k):
                return r[k]["value"] if k in r else None

            qid = g("person").rsplit("/", 1)[-1]
            rec = people.get(qid)
            if rec is None:
                employers = []
                for chunk in (g("employers") or "").split(" | "):
                    if not chunk.strip():
                        continue
                    label, coord, country = (chunk.split("@@") + ["", ""])[:3]
                    ll = _parse_point(coord) if coord else None
                    employers.append({"name": label.strip(),
                                      "lat": ll[0] if ll else None,
                                      "lon": ll[1] if ll else None,
                                      "country": country.strip() or None})
                rec = people[qid] = {
                    "source": "wikidata", "id": qid,
                    "name": g("personLabel"), "description": g("personDescription"),
                    "relations": [], "is_alumnus": False,
                    "birth_year": _year(g("dob")), "death_year": _year(g("dod")),
                    "image": g("image"),
                    "occupations": [o for o in (g("occs") or "").split(" | ") if o],
                    "fields": [f for f in (g("fields") or "").split(" | ") if f],
                    "degrees": [d for d in (g("degrees") or "").split(" | ") if d],
                    "employers": employers,
                    "orcid": g("orcid"), "scholar_id": g("scholar"),
                    "wikipedia": g("article"), "wikidata_url": g("person"),
                }
            rec["relations"].append(relabel)
            if relabel == "educated_at":
                rec["is_alumnus"] = True

    out = sorted(people.values(), key=lambda p: p["name"] or "")
    (RAW / "wikidata_alumni.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    n_alum = sum(1 for p in out if p["is_alumnus"])
    print(f"Wikidata: {len(out)} people ({n_alum} educated at Balseiro, "
          f"{len(out) - n_alum} staff/affiliate only; "
          f"{sum(1 for p in out if p['employers'])} with an employer).")
    return out


if __name__ == "__main__":
    collect()
