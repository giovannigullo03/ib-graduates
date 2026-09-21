"""Collect Balseiro astronomers from the NASA ADS (Astrophysics Data System).

ADS covers astronomy / astrophysics / astro-particle literature. Unlike INSPIRE
it has no author records with career histories — only per-paper affiliations —
so we take each author's most recent paper's affiliation as their current one.

*** Requires a free ADS API token. *** Create an account at
https://ui.adsabs.harvard.edu, then Account -> Settings -> API Token. Provide it
via the environment (preferred, nothing on disk):

    export ADS_TOKEN=xxxxxxxx

or, if you must, a file `data/ads_token.txt` (gitignored -- never commit a token;
this repo is public. If one ever leaks, regenerate it in ADS settings).

Without a token this collector does nothing (the rest of the pipeline is fine).

Output: data/raw/ads_authors.json
"""
from __future__ import annotations

import json
import os
import re

from common import RAW, DATA, cached_get, strip_accents

API = "https://api.adsabs.harvard.edu/v1/search/query"
QUERY = ('aff:("Instituto Balseiro" OR "Centro Atomico Bariloche" OR '
         '"Centro Atómico Bariloche" OR "Bariloche Atomic")')
BALSEIRO_RE = re.compile(r"balseiro|bariloche", re.I)


def _token() -> str | None:
    tok = os.environ.get("ADS_TOKEN", "").strip()
    if tok:
        return tok
    f = DATA / "ads_token.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip() or None
    return None


def _flip(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip().strip(",. ")
    if "," in name:
        fam, giv = name.split(",", 1)
        name = f"{giv.strip()} {fam.strip()}"
    return re.sub(r"\s+", " ", name).strip(",. ")


def _clean_aff(aff: str) -> str | None:
    if not aff or aff.strip() in ("-", "<NA>"):
        return None
    a = re.sub(r"\s*<[^>]*>\s*", " ", aff)
    a = re.split(r";", a)[0]
    # keep just the first institution: cut at " and ", the second comma, or
    # any postal cruft (street numbers, "8400 Bariloche", emails)
    a = re.split(r"\s+and[\s,]|,?\s*(?:Av\.|Avenida|Ruta|\d{3,}|C\.?P\.?|[\w.]+@)", a)[0]
    parts = [p.strip() for p in a.split(",") if p.strip()]
    a = ", ".join(parts[:2])
    a = re.sub(r"\s+", " ", a).strip(" ,.-")
    return a if 3 < len(a) < 80 else None


def collect() -> list[dict]:
    tok = _token()
    if not tok:
        print("ADS: no API token (set ADS_TOKEN or create data/ads_token.txt) — skipping. "
              "See scripts/collect_ads.py header.")
        (RAW / "ads_authors.json").write_text("[]", encoding="utf-8")
        return []

    authors: dict[str, dict] = {}
    start, rows = 0, 200
    while True:
        try:
            data = cached_get(
                API,
                params={"q": QUERY, "fl": "author,aff,orcid_pub,year,bibcode",
                        "rows": rows, "start": start, "sort": "date desc"},
                headers={"Authorization": f"Bearer {tok}"},
                throttle_key="ads", min_interval=1.0, ttl_days=21,
                cache_key=f"ads::{start}",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ADS: query failed ({exc})")
            break
        docs = (data.get("response") or {}).get("docs") or []
        for d in docs:
            names = d.get("author") or []
            affs = d.get("aff") or []
            orcids = d.get("orcid_pub") or []
            year = d.get("year")
            for i, nm in enumerate(names):
                aff = affs[i] if i < len(affs) else ""
                orc = orcids[i] if i < len(orcids) else "-"
                is_balseiro = bool(BALSEIRO_RE.search(aff or ""))
                key = strip_accents(_flip(nm)).lower()
                cur = authors.get(key)
                if cur is None:
                    authors[key] = {
                        "name": _flip(nm),
                        "orcid": orc if re.match(r"\d{4}-", orc or "") else None,
                        "_year": year, "_aff": aff, "_balseiro_seen": is_balseiro,
                    }
                else:
                    if not cur.get("_balseiro_seen") and is_balseiro:
                        cur["_balseiro_seen"] = True
                    if year and (not cur["_year"] or year > cur["_year"]):
                        cur["_year"], cur["_aff"] = year, aff
                    if not cur["orcid"] and re.match(r"\d{4}-", orc or ""):
                        cur["orcid"] = orc
        total = (data.get("response") or {}).get("numFound", 0)
        start += rows
        if start >= total or not docs:
            break

    out = []
    for a in authors.values():
        if not a.get("_balseiro_seen"):
            continue  # only appeared as a non-Balseiro co-author on some paper
        if len(a["name"].split()) < 2:
            continue
        aff = _clean_aff(a["_aff"])
        out.append({
            "name": a["name"],
            "orcid": a["orcid"],
            "last_paper_year": a["_year"],
            "current_institution": {"name": aff, "country": None} if aff else None,
            "keywords": [],
        })

    out.sort(key=lambda a: a["name"].lower())
    (RAW / "ads_authors.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"ADS: {len(out)} authors seen with a Balseiro affiliation "
          f"({sum(1 for a in out if a['orcid'])} with ORCID, "
          f"{sum(1 for a in out if a['current_institution'])} with a recent affiliation)")
    return out


if __name__ == "__main__":
    collect()
