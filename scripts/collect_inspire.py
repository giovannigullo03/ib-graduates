"""Collect Balseiro physicists from INSPIRE-HEP.

INSPIRE (inspirehep.net) is the curated literature + author database for
high-energy physics, run by CERN with a public API meant for reuse. It covers
the particle / string / gravitation / cosmology / astro-particle slice of
Balseiro alumni -- a minority of graduates, but disproportionately the ones who
went abroad, and it has data the other sources don't:

  * a full career history -- every institution with start/end years and a
    "current" flag (and INSPIRE institution records carry coordinates);
  * the person's PhD advisor.

Authors are found two ways: a direct search on `positions.institution`, and by
walking the authors of every paper with a Balseiro affiliation.

Output: data/raw/inspire_authors.json
"""
from __future__ import annotations

import json

from common import RAW, cached_get

API = "https://inspirehep.net/api"
# ICNs (INSPIRE Classification Names) that mean Instituto Balseiro / CAB:
BALSEIRO_ICNS = [
    "Balseiro Inst., San Carlos de Bariloche",
    "Bariloche, CAB",
    "Bariloche, Inst. Balseiro",
]
ARXIV_TO_FIELD = {
    "hep-th": "string theory quantum gravity",
    "hep-ph": "particle physics phenomenology",
    "hep-ex": "particle physics experiment",
    "hep-lat": "lattice field theory",
    "gr-qc": "general relativity gravitation",
    "astro-ph": "astrophysics cosmology",
    "nucl-th": "nuclear physics theory",
    "nucl-ex": "nuclear physics experiment",
    "cond-mat": "condensed matter physics",
    "quant-ph": "quantum information",
    "math-ph": "mathematical physics",
    "physics.plasm-ph": "plasma physics",
    "physics.acc-ph": "accelerator physics",
}


def _get(path, params, key, ttl=21):
    return cached_get(f"{API}/{path}", params=params, throttle_key="inspire",
                      min_interval=0.4, ttl_days=ttl, cache_key=key)


def _author_recids() -> set[str]:
    recids: set[str] = set()

    # 1. direct author search
    for icn in BALSEIRO_ICNS:
        try:
            data = _get("authors", {"q": f'positions.institution:"{icn}"',
                                    "fields": "control_number", "size": 250},
                        key=f"insp-auth-search::{icn}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! author search {icn!r}: {exc}")
            continue
        for h in data.get("hits", {}).get("hits", []):
            recids.add(str(h["metadata"]["control_number"]))

    # 2. authors of Balseiro-affiliated papers
    for icn in BALSEIRO_ICNS[:1]:
        page = 1
        while True:
            try:
                data = _get("literature",
                            {"q": f'aff "{icn}"',
                             "fields": "authors.record,authors.affiliations.value",
                             "size": 100, "page": page},
                            key=f"insp-lit::{icn}::{page}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! literature page {page}: {exc}")
                break
            hits = data.get("hits", {}).get("hits", [])
            for h in hits:
                for a in h["metadata"].get("authors", []):
                    affs = " ".join(x.get("value", "") for x in a.get("affiliations", []))
                    if "Balseiro" in affs or "Bariloche" in affs:
                        ref = (a.get("record") or {}).get("$ref", "")
                        if ref:
                            recids.add(ref.rsplit("/", 1)[-1])
            if len(hits) < 100 or page >= 10:
                break
            page += 1

    print(f"INSPIRE: {len(recids)} candidate author records")
    return recids


_INST_CACHE: dict[str, dict] = {}


def _institution(ref_or_id) -> dict | None:
    if not ref_or_id:
        return None
    iid = str(ref_or_id).rsplit("/", 1)[-1]
    if iid in _INST_CACHE:
        return _INST_CACHE[iid]
    try:
        data = _get(f"institutions/{iid}",
                    {"fields": "legacy_ICN,addresses"}, key=f"insp-inst::{iid}", ttl=120)
    except Exception:  # noqa: BLE001
        return None
    m = data.get("metadata", {})
    addr = (m.get("addresses") or [{}])[0]
    out = {
        "name": m.get("legacy_ICN"),
        "city": (addr.get("cities") or [None])[0],
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").upper() or None,
        "lat": addr.get("latitude"),
        "lon": addr.get("longitude"),
    }
    _INST_CACHE[iid] = out
    return out


def _current_position(positions):
    dated = [p for p in positions if p.get("institution")]
    cur = [p for p in dated if p.get("current")]
    if cur:
        return max(cur, key=lambda p: p.get("start_date") or "")
    if dated:
        return max(dated, key=lambda p: p.get("start_date") or "")
    return None


def collect() -> list[dict]:
    recids = _author_recids()
    out = []
    for i, rid in enumerate(sorted(recids), 1):
        if i % 50 == 0:
            print(f"  INSPIRE authors {i}/{len(recids)}")
        try:
            data = _get(f"authors/{rid}",
                        {"fields": "name,positions,advisors,arxiv_categories,ids"},
                        key=f"insp-author::{rid}", ttl=45)
        except Exception:  # noqa: BLE001
            continue
        m = data.get("metadata", {})
        name = (m.get("name") or {}).get("value")
        if not name:
            continue
        # "Family, Given" -> "Given Family"
        if "," in name:
            fam, giv = name.split(",", 1)
            name = f"{giv.strip()} {fam.strip()}"

        orcid = next((x["value"] for x in m.get("ids", []) if x.get("schema") == "ORCID"), None)
        positions = m.get("positions", [])
        career = [{
            "institution": p.get("institution"),
            "start": (p.get("start_date") or "")[:4] or None,
            "end": (p.get("end_date") or "")[:4] or None,
            "current": bool(p.get("current")),
        } for p in positions if p.get("institution")]

        cur = _current_position(positions)
        cur_inst = _institution((cur or {}).get("record", {}).get("$ref")
                                or (cur or {}).get("record")) if cur else None
        if cur and not cur_inst:
            cur_inst = {"name": cur.get("institution"), "city": None, "country": None,
                        "country_code": None, "lat": None, "lon": None}

        advisors = [a.get("name") for a in m.get("advisors", [])
                    if a.get("name") and a.get("name") != "Unknown"]
        cats = m.get("arxiv_categories") or []
        field_blob = " ".join(ARXIV_TO_FIELD.get(c, c) for c in cats)

        out.append({
            "name": name,
            "orcid": orcid,
            "inspire_bai": next((x["value"] for x in m.get("ids", [])
                                 if x.get("schema") == "INSPIRE BAI"), None),
            "current_institution": cur_inst,
            "career": career,
            "advisors": advisors[:2],
            "arxiv_categories": cats,
            "field_blob": field_blob,
        })

    out.sort(key=lambda a: a["name"].lower())
    (RAW / "inspire_authors.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"INSPIRE: {len(out)} authors "
          f"({sum(1 for a in out if a['current_institution'] and a['current_institution']['lat'])} "
          f"with located current institution, "
          f"{sum(1 for a in out if a['orcid'])} with ORCID, "
          f"{sum(1 for a in out if a['advisors'])} with a known advisor)")
    return out


if __name__ == "__main__":
    collect()
