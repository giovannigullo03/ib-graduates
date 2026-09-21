"""Collect notable Balseiro alumni from Wikipedia maintenance categories.

Wikipedia curates "<lang>:Category:Balseiro Institute alumni" style lists.
For each member we pull a one-line description, thumbnail, and the linked
Wikidata QID so the builder can merge these with the richer Wikidata records.

Output: data/raw/wikipedia_alumni.json
"""
from __future__ import annotations

import json
import re

from common import RAW, cached_get

CATEGORIES = [
    ("es", "Alumnado del Instituto Balseiro"),
    ("es", "Profesores del Instituto Balseiro"),
    ("en", "Balseiro Institute alumni"),
    ("en", "Instituto Balseiro alumni"),
    ("en", "Balseiro Institute faculty"),
    ("pt", "Alunos do Instituto Balseiro"),
    ("fr", "Étudiant de l'Institut Balseiro"),
    ("it", "Studenti dell'Istituto Balseiro"),
    ("de", "Absolvent des Instituto Balseiro"),
    ("ca", "Alumnes de l'Institut Balseiro"),
]


def _api(lang, params, key):
    params = {"format": "json", "action": "query", **params}
    return cached_get(f"https://{lang}.wikipedia.org/w/api.php", params=params,
                      throttle_key="wikipedia", min_interval=0.4, ttl_days=14,
                      cache_key=f"wp::{lang}::{key}")


def _category_members(lang, cat):
    data = _api(lang, {"list": "categorymembers", "cmtitle": f"Category:{cat}",
                       "cmlimit": 500, "cmtype": "page"}, f"cat::{cat}")
    return [m["title"] for m in data.get("query", {}).get("categorymembers", [])]


def _describe(lang, titles):
    out = {}
    for i in range(0, len(titles), 40):
        batch = titles[i:i + 40]
        data = _api(lang, {
            "prop": "extracts|pageprops|pageimages", "exintro": 1, "explaintext": 1,
            "exsentences": 2, "ppprop": "wikibase_item", "piprop": "thumbnail",
            "pithumbsize": 400, "titles": "|".join(batch), "redirects": 1,
        }, f"desc::{'|'.join(batch)}")
        for page in data.get("query", {}).get("pages", {}).values():
            if "missing" in page:
                continue
            out[page["title"]] = {
                "source": "wikipedia",
                "name": page["title"],
                "description": re.sub(r"\s+", " ", (page.get("extract") or "")).strip()[:300] or None,
                "qid": (page.get("pageprops") or {}).get("wikibase_item"),
                "image": (page.get("thumbnail") or {}).get("source"),
                "wikipedia": f"https://{lang}.wikipedia.org/wiki/{page['title'].replace(' ', '_')}",
            }
    return out


def collect() -> list[dict]:
    people: dict[str, dict] = {}
    for lang, cat in CATEGORIES:
        try:
            members = _category_members(lang, cat)
        except Exception as exc:  # noqa: BLE001
            print(f"Wikipedia[{lang}:{cat}]: {exc}")
            continue
        if not members:
            continue
        print(f"Wikipedia[{lang}:{cat}]: {len(members)} members")
        for title, rec in _describe(lang, members).items():
            key = rec["qid"] or f"{lang}:{rec['name'].lower()}"
            people.setdefault(key, rec)

    out = sorted(people.values(), key=lambda p: p["name"].lower())
    (RAW / "wikipedia_alumni.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Wikipedia: {len(out)} notable alumni "
          f"({sum(1 for p in out if p['qid'])} with a Wikidata item)")
    return out


if __name__ == "__main__":
    collect()
