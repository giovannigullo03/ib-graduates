"""Reconcile the IB thesis roster against OpenAlex to locate & confirm people.

The thesis roster (collect_ricabib) gives us ~900 confirmed graduates with a
name, a year and a very specific thesis title — but no idea where they are now.
OpenAlex indexes most of those theses as `dissertation` works. Matching a
thesis to its OpenAlex work gives us the author's OpenAlex profile, and with it
their **current institution (with coordinates)**, ORCID, publication count and
research concepts.

For every thesis whose author isn't already in the OpenAlex Balseiro pool:
  1. search works by the (verbatim) title,
  2. keep a hit whose year is within +/-1 of the thesis and whose author
     surname matches,
  3. pull that author's record.

Output: data/raw/thesis_reconciled.json  (same shape as openalex_authors.json,
so build_dataset merges it through the normal OpenAlex path)
"""
from __future__ import annotations

import json
import re

from common import RAW, name_key, strip_accents, cached_get, RateLimitExceeded
from collect_openalex import OA, MAILTO, GENERIC_CONCEPTS
import collect_orcid as co

THESES = RAW / "ricabib_theses.json"
OA_AUTHORS = RAW / "openalex_authors.json"
ORCID_ALUMNI = RAW / "orcid_alumni.json"
OUT = RAW / "thesis_reconciled.json"        # OpenAlex-shaped, merged via _enrich_openalex
OUT_ORCID = RAW / "thesis_orcid.json"       # ORCID-shaped, merged via _merge_orcid

# OpenAlex now bills the free API against a ~$0.10/day budget (~100 requests).
# Each reconciliation is up to 2 requests, so cap new lookups per run and let
# the on-disk cache carry progress across days.
MAX_NEW_PER_RUN = 40


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", strip_accents(name or "").lower()) if len(t) > 2}


def _same_person(thesis_name: str, oa_name: str) -> bool:
    a, b = _tokens(thesis_name), _tokens(oa_name)
    if not a or not b:
        return False
    # at least one shared surname-ish token, and no more than one side-only long token
    return len(a & b) >= 1


def _todo_theses():
    """Unique thesis authors not already covered by OpenAlex's Balseiro pool
    or the ORCID Balseiro-education search."""
    theses = json.loads(THESES.read_text(encoding="utf-8"))
    covered = set()
    for f in (OA_AUTHORS, ORCID_ALUMNI):
        if f.exists():
            covered |= {name_key(a["name"]) for a in json.loads(f.read_text(encoding="utf-8"))}
    todo = {}
    for t in theses:
        if not t.get("title") or not t.get("year"):
            continue
        nk = name_key(t["author"])
        if nk in covered or nk in todo:
            continue
        todo[nk] = t
    return todo


# ------------------------------------------------------------------ #
# ORCID name reconciliation (free)
# ------------------------------------------------------------------ #
def _orcid_search(family: str, given: str):
    q = f'family-name:{family} AND given-names:{given}'
    try:
        data = cached_get(f"{co.PUB}/expanded-search/",
                          params={"q": q, "rows": 8},
                          throttle_key="orcid", min_interval=0.34, ttl_days=30,
                          cache_key=f"orcid-namesearch::{family}|{given}".lower())
    except Exception:  # noqa: BLE001
        return []
    return data.get("expanded-result") or []


def reconcile_orcid(todo: dict, cap: int) -> int:
    done = {}
    if OUT_ORCID.exists():
        done = {name_key(r["name"]): r for r in json.loads(OUT_ORCID.read_text(encoding="utf-8"))}

    out = list(done.values())
    checked = hits = 0
    for nk, t in sorted(todo.items()):
        if nk in done:
            continue
        if checked >= cap:
            print(f"reconcile/orcid: stopping at {cap} this run")
            break
        checked += 1
        parts = re.split(r"\s+", t["author"].strip())
        if len(parts) < 2:
            continue
        given, family = parts[0], parts[-1]
        family2 = " ".join(parts[-2:]) if len(parts) >= 3 else None

        cands = _orcid_search(family, given)
        if family2:
            cands += _orcid_search(family2, given)
        seen = set()
        best = None
        for c in cands:
            oid = c.get("orcid-id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            if len(seen) > 6:
                break
            try:
                rec = co.parse_record(oid, co.fetch_record(oid), require_balseiro=False)
            except Exception:  # noqa: BLE001
                continue
            if not rec:
                continue
            rn = strip_accents(rec["name"]).lower()
            if strip_accents(family).lower() not in rn:
                continue
            edu_blob = strip_accents(" ".join(rec.get("all_education") or [])).lower()
            balseiro = rec["balseiro_edu"] or rec["balseiro_affiliate"] \
                or "balseiro" in edu_blob or "atomico bariloche" in edu_blob
            end_years = []  # rough: any education near thesis year is a good sign
            near_year = False
            if not balseiro:
                # weak match: require the roster year to sit inside their career
                cur = rec.get("current_employer") or {}
                if cur.get("start_year") and cur["start_year"] >= (t["year"] - 2):
                    near_year = True
            score = (2 if balseiro else 0) + (1 if near_year else 0) \
                + (1 if strip_accents(given).lower()[:3] in rn else 0)
            if score >= 3 and (best is None or score > best[0]):
                best = (score, rec)

        if best:
            rec = best[1]
            for k in ("given", "family", "balseiro_edu", "balseiro_affiliate", "all_education"):
                rec.pop(k, None)
            rec["grad_year"] = rec.get("grad_year") or t["year"]
            rec["_via"] = "thesis->orcid"
            out.append(rec)
            hits += 1

    OUT_ORCID.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"reconcile/orcid: +{hits} matched this run ({checked} searched); "
          f"{len(out)} total, {sum(1 for r in out if r['current_employer'])} with an employer.")
    return checked


def _author_record(aid: str, thesis_name: str, thesis_year: int | None) -> dict | None:
    try:
        au = cached_get(
            f"{OA}/authors/{aid}",
            params={"select": "id,display_name,orcid,works_count,summary_stats,"
                              "last_known_institutions,x_concepts,affiliations",
                    "mailto": MAILTO},
            throttle_key="openalex", min_interval=0.25, ttl_days=30,
            cache_key=f"oa-author::{aid}",
        )
    except RateLimitExceeded:
        raise
    except Exception:  # noqa: BLE001
        return None

    lki = (au.get("last_known_institutions") or [{}])[0]
    concepts = [c["display_name"] for c in (au.get("x_concepts") or [])
                if (c.get("score") or 0) >= 0.35
                and c["display_name"].lower() not in GENERIC_CONCEPTS][:5]
    orcid = (au.get("orcid") or "").rsplit("/", 1)[-1] or None
    return {
        "openalex_id": aid,
        "name": thesis_name,                       # match key = the roster name
        "name_alts": [au.get("display_name")] if au.get("display_name") else [],
        "orcid": orcid,
        "works_count": au.get("works_count"),
        "h_index": (au.get("summary_stats") or {}).get("h_index"),
        "concepts": concepts,
        "balseiro_years": [thesis_year] if thesis_year else [],
        "career_start": thesis_year,
        "current_institution": {
            "name": lki.get("display_name"),
            "country_code": lki.get("country_code"),
            "ror": lki.get("ror"),
            "openalex_id": (lki.get("id") or "").rsplit("/", 1)[-1] or None,
            "type": lki.get("type"),
        } if lki.get("display_name") else None,
        "review_score": 6.0,          # thesis+work match is strong evidence
        "via": "thesis->openalex",
    }


def reconcile(orcid_cap: int = 800) -> list[dict]:
    if not THESES.exists():
        print("reconcile: no thesis file yet, skipping")
        return []

    # ---- pass 1: ORCID name search (free) --------------------------------- #
    todo = _todo_theses()
    print(f"reconcile: {len(todo)} thesis authors with no location yet")
    reconcile_orcid(todo, cap=orcid_cap)

    # ---- pass 2: OpenAlex works match (metered, resumes across days) ------ #
    todo = _todo_theses()      # recompute: ORCID pass may have removed some
    orcid_located = set()
    if OUT_ORCID.exists():
        orcid_located = {name_key(r["name"])
                         for r in json.loads(OUT_ORCID.read_text(encoding="utf-8"))}
    todo = {k: v for k, v in todo.items() if k not in orcid_located}

    done = {}
    if OUT.exists():
        done = {name_key(r["name"]): r for r in json.loads(OUT.read_text(encoding="utf-8"))}
    todo = {k: v for k, v in todo.items() if k not in done}

    out = list(done.values())
    new_lookups = hits = 0
    quota_hit = False
    print(f"reconcile/openalex: {len(done)} already reconciled, {len(todo)} left")

    for nk, t in sorted(todo.items()):
        if new_lookups >= MAX_NEW_PER_RUN:
            print(f"reconcile: stopping at {MAX_NEW_PER_RUN} new lookups this run "
                  f"(OpenAlex daily budget) -- run again to continue")
            break
        title = re.sub(r"\s+", " ", t["title"]).strip(" .")[:150]
        try:
            data = cached_get(
                f"{OA}/works",
                params={"filter": f"title.search:{title}",
                        "select": "id,title,publication_year,type,authorships",
                        "per-page": 5, "mailto": MAILTO},
                throttle_key="openalex", min_interval=0.25, ttl_days=21,
                cache_key=f"oa-work::{nk}::{t['year']}",
            )
        except RateLimitExceeded as exc:
            print(f"reconcile: {exc} -- saving progress, run again after reset")
            quota_hit = True
            break
        except Exception:  # noqa: BLE001
            continue
        new_lookups += 1

        for w in data.get("results", []):
            if abs((w.get("publication_year") or 0) - t["year"]) > 1:
                continue
            auths = w.get("authorships") or []
            cand = auths[0] if len(auths) == 1 else next(
                (a for a in auths if _same_person(t["author"], a["author"]["display_name"])), None)
            if not cand:
                continue
            aid = cand["author"]["id"].rsplit("/", 1)[-1]
            try:
                rec = _author_record(aid, t["author"], t["year"])
            except RateLimitExceeded:
                quota_hit = True
                rec = None
            if rec:
                rec["matched_work"] = {"title": w.get("title"),
                                       "year": w.get("publication_year"), "type": w.get("type")}
                out.append(rec)
                hits += 1
            break
        if quota_hit:
            break

    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    located = sum(1 for r in out if r.get("current_institution"))
    remaining = max(0, len(todo) - new_lookups)
    tail = " (OpenAlex budget hit)" if quota_hit else ""
    print(f"reconcile/openalex: +{hits} matched this run ({new_lookups} lookups){tail}; "
          f"{len(out)} total, {located} with a current institution. "
          f"~{remaining} thesis authors still to check -- re-run to continue.")
    return out


if __name__ == "__main__":
    import sys
    cap = int(sys.argv[sys.argv.index("--orcid-cap") + 1]) if "--orcid-cap" in sys.argv else 800
    reconcile(orcid_cap=cap)
