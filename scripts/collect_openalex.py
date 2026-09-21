"""Collect Balseiro-linked authors from OpenAlex.

OpenAlex records ~2,900 authors who have *ever* published with an Instituto
Balseiro affiliation. That set is noisy — big collaborations (notably the
Pierre Auger Observatory) attach the affiliation to hundreds of co-authors who
never studied there. So this collector does NOT emit alumni directly. It:

  1. saves every candidate's useful fields (name, ORCID, current institution
     *with coordinates*, affiliation years, concepts, h-index);
  2. writes data/raw/openalex_orcid_ids.json — the ORCID iDs, which
     collect_orcid.py then verifies against each person's education history
     (the trustworthy gate);
  3. scores the ORCID-less candidates so build_dataset.py can build a
     human review queue.

Output: data/raw/openalex_authors.json, data/raw/openalex_orcid_ids.json
"""
from __future__ import annotations

import json

from common import RAW, cached_get, strip_accents, RateLimitExceeded

OA = "https://api.openalex.org"
MAILTO = "fraanco.borgarello@gmail.com"
BALSEIRO_OA_ID = "I2802473995"          # openalex.org/I2802473995  (ror 00sx0cd23)

AUGER_MARKERS = ("cosmic ray", "pierre auger", "ultra-high-energy", "air shower")

# OpenAlex attaches these broad concepts to almost everyone; they are useless
# for telling one person's field from another's, so drop them before we let
# concepts influence the discipline classifier.
GENERIC_CONCEPTS = {
    "physics", "chemistry", "mathematics", "biology", "computer science",
    "engineering", "materials science", "quantum mechanics", "medicine",
    "geology", "philosophy", "psychology", "economics", "political science",
    "art", "history", "geography", "business", "sociology", "mechanics",
    "thermodynamics", "classical mechanics", "nanotechnology", "chemical physics",
    "analytical chemistry", "composite material", "atomic physics",
    "humanities", "observatory", "environmental science", "biochemistry",
}


def _page_authors():
    cursor = "*"
    while cursor:
        data = cached_get(
            f"{OA}/authors",
            params={
                "filter": f"affiliations.institution.id:{BALSEIRO_OA_ID}",
                "select": ("id,display_name,display_name_alternatives,orcid,works_count,"
                           "summary_stats,last_known_institutions,affiliations,x_concepts"),
                "per-page": 200, "cursor": cursor, "mailto": MAILTO,
            },
            throttle_key="openalex", min_interval=0.2, ttl_days=21,
            cache_key=f"oa-authors::{cursor}",
        )
        yield from data.get("results", [])
        cursor = (data.get("meta") or {}).get("next_cursor")


def _score(author, balseiro_years, all_years):
    """Heuristic 0-6: how likely this ORCID-less author is a real alumnus.

    The dominant noise source is the Pierre Auger Observatory collaboration,
    whose affiliation pollution is almost entirely from 2004 onward (first
    data-taking). So a pre-2004 Balseiro year is a strong positive, and a
    post-2004-only association gets heavy scrutiny.
    """
    s = 0.0
    wc = author.get("works_count") or 0
    h = (author.get("summary_stats") or {}).get("h_index") or 0
    concepts = " ".join(strip_accents(c["display_name"]).lower()
                        for c in (author.get("x_concepts") or [])[:10])
    has_auger = any(m in concepts for m in AUGER_MARKERS)
    started_here = bool(balseiro_years and all_years and min(balseiro_years) <= min(all_years) + 1)
    span = len(balseiro_years)

    if balseiro_years and min(balseiro_years) < 2004:
        s += 2.5                                   # predates the Auger era
    if started_here:
        s += 2.0
    if span >= 3:
        s += 1.0
    elif span == 2:
        s += 0.5
    if 1 <= wc <= 90:
        s += 1.0
    if h <= 25:
        s += 0.5

    if has_auger:
        s -= 2.0
        if not started_here or (balseiro_years and min(balseiro_years) >= 2004):
            s -= 2.5                               # post-2004 cosmic-ray co-author
    if span == 1 and (not balseiro_years or balseiro_years[0] >= 2004):
        s -= 1.5                                   # one recent year only
    if not author.get("last_known_institutions"):
        s -= 1.0
    return round(s, 1)


def collect() -> dict:
    authors = []
    n_orcid = 0
    try:
        author_pages = list(_page_authors())
    except RateLimitExceeded as exc:
        if (RAW / "openalex_authors.json").exists():
            print(f"OpenAlex: {exc} -- keeping the previously harvested authors")
            return {"authors": [], "orcid_ids": []}
        raise
    for a in author_pages:
        affils = a.get("affiliations") or []
        by_inst = {af["institution"]["id"]: (af.get("years") or []) for af in affils}
        balseiro_years = sorted(by_inst.get(f"https://openalex.org/{BALSEIRO_OA_ID}", []))
        all_years = sorted(y for ys in by_inst.values() for y in ys)

        lki = (a.get("last_known_institutions") or [{}])[0]
        orcid = (a.get("orcid") or "").rsplit("/", 1)[-1] or None
        if orcid:
            n_orcid += 1

        # keep only specific, confident concepts (drop the broad umbrellas that
        # OpenAlex attaches to everyone)
        concepts = [c["display_name"] for c in (a.get("x_concepts") or [])
                    if (c.get("score") or 0) >= 0.35
                    and c["display_name"].lower() not in GENERIC_CONCEPTS][:5]

        authors.append({
            "openalex_id": a["id"].rsplit("/", 1)[-1],
            "name": a["display_name"],
            "name_alts": a.get("display_name_alternatives") or [],
            "orcid": orcid,
            "works_count": a.get("works_count"),
            "h_index": (a.get("summary_stats") or {}).get("h_index"),
            "concepts": concepts,
            "balseiro_years": balseiro_years,
            "career_start": min(all_years) if all_years else None,
            "current_institution": {
                "name": lki.get("display_name"),
                "country_code": lki.get("country_code"),
                "ror": lki.get("ror"),
                "openalex_id": (lki.get("id") or "").rsplit("/", 1)[-1] or None,
                "type": lki.get("type"),
            } if lki.get("display_name") else None,
            "review_score": _score(a, balseiro_years, all_years),
        })

    orcid_ids = sorted({a["orcid"] for a in authors if a["orcid"]})
    authors.sort(key=lambda a: (-(a["review_score"] or 0), a["name"].lower()))

    (RAW / "openalex_authors.json").write_text(
        json.dumps(authors, indent=1, ensure_ascii=False), encoding="utf-8")
    (RAW / "openalex_orcid_ids.json").write_text(
        json.dumps(orcid_ids, indent=1), encoding="utf-8")

    print(f"OpenAlex: {len(authors)} Balseiro-affiliated authors "
          f"({n_orcid} with ORCID -> handed to the ORCID verifier; "
          f"{sum(1 for a in authors if not a['orcid'] and a['review_score'] >= 3)} "
          f"strong ORCID-less candidates for review)")
    return {"authors": authors, "orcid_ids": orcid_ids}


# ------------------------------------------------------------------ #
# institution coordinates (used by build_dataset for OpenAlex-sourced locations)
# ------------------------------------------------------------------ #
def institution_geo(openalex_id: str) -> dict | None:
    if not openalex_id:
        return None
    try:
        data = cached_get(f"{OA}/institutions/{openalex_id}",
                          params={"select": "display_name,geo,country_code", "mailto": MAILTO},
                          throttle_key="openalex", min_interval=0.2, ttl_days=120,
                          cache_key=f"oa-inst::{openalex_id}")
    except Exception:  # noqa: BLE001
        return None
    geo = data.get("geo") or {}
    if geo.get("latitude") is None:
        return None
    return {
        "lat": geo["latitude"], "lon": geo["longitude"],
        "city": geo.get("city"), "country": geo.get("country"),
        "country_code": (geo.get("country_code") or "").upper(),
    }


if __name__ == "__main__":
    collect()
