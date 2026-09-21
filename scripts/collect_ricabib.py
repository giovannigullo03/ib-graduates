"""Collect Instituto Balseiro theses from the CNEA institutional repository.

The Balseiro / Centro Atomico Bariloche theses live in RICABIB
(ricabib.cab.cnea.gov.ar) but that host is frequently unresolvable from
outside CNEA's own network. The same collection is mirrored in **NUCLEA**
(https://nuclea.cnea.gob.ar), a DSpace repository that *is* publicly
reachable, so we harvest that by default and fall back to RICABIB.

This is the authoritative *roster* of graduates: author, year and thesis
title for ~1000 theses (physics, nuclear/mechanical engineering, medical
physics, ...). It carries no current-employer data, so a person found only
here shows on the map only once another source supplies a location — but it
confirms names, pins graduation years and feeds the field classifier.

    python3 scripts/collect_ricabib.py           # harvest (NUCLEA)
    python3 scripts/collect_ricabib.py --probe   # test connectivity
    python3 scripts/collect_ricabib.py --source ricabib   # force direct RICABIB

Output: data/raw/ricabib_theses.json
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET

from common import RAW, SESSION

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

SOURCES = {
    # DSpace: the "INSTITUTO BALSEIRO > TESIS" collection
    "nuclea": {
        "oai": "https://nuclea.cnea.gob.ar/oai/request",
        "set": "col_20.500.12553_770",
    },
    # EPrints: harvest everything, filter to theses by dc:type
    "ricabib": {
        "oai": "https://ricabib.cab.cnea.gov.ar/cgi/oai2",
        "set": None,
    },
}

LEVEL_RULES = [
    ("Doctorate (PhD)", r"tesis doctoral|doctorad|\bph\.?\s?d\b|doctor en|tesis de doctorado"),
    ("Master's", r"maestr[ií]a|magister|mag[ií]ster|master|tesis de maestr"),
    ("Specialization / diploma", r"especializaci[oó]n|especialista|carrera de especial"),
    ("Engineering degree", r"proyecto integrador|proyecto y dise|tesis de ingenier|ingenier[ií]a (nuclear|mec[aá]nica)"),
    ("Physics degree (Licenciatura)", r"tesis de licenciatura|licenciatura en|trabajo especial|seminario de licenciatura|tesis de grado"),
]


def _get(url, params, tries=4):
    last = None
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=50)
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError(last)


def _flip_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip().strip(",")
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name


def _year(dates):
    for d in dates:
        m = re.search(r"(18|19|20)\d{2}", d or "")
        if m:
            return int(m.group())
    return None


def _dedupe_subjects(subjects):
    # NUCLEA stores each subject twice (English + Spanish). Keep one of each pair.
    out, seen = [], set()
    for s in subjects:
        k = s.lower().strip()
        if k and k not in seen:
            seen.add(k)
            out.append(s.strip())
    return out[:8]


def probe(source: str) -> bool:
    cfg = SOURCES[source]
    print(f"Probing {cfg['oai']} ...")
    try:
        txt = _get(cfg["oai"], {"verb": "Identify"}, tries=1)
        name = re.search(r"<repositoryName>(.*?)</repositoryName>", txt)
        print("  reachable:", name.group(1) if name else "yes")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  not reachable: {exc}")
        return False


def _harvest(source: str, limit: int | None):
    cfg = SOURCES[source]
    rows, token, page = [], None, 0
    while True:
        page += 1
        params = ({"verb": "ListRecords", "resumptionToken": token} if token else
                  {"verb": "ListRecords", "metadataPrefix": "oai_dc",
                   **({"set": cfg["set"]} if cfg["set"] else {})})
        xml_text = _get(cfg["oai"], params)
        root = ET.fromstring(xml_text)

        err = root.find(".//oai:error", OAI_NS)
        if err is not None:
            if err.get("code") != "noRecordsMatch":
                print(f"  OAI error: {err.get('code')} {err.text}")
            break

        for rec in root.findall(".//oai:record", OAI_NS):
            if rec.find(".//oai:header[@status='deleted']", OAI_NS) is not None:
                continue
            md = rec.find(".//oai_dc:dc", OAI_NS)
            if md is None:
                continue

            def vals(tag):
                return [e.text.strip() for e in md.findall(f"dc:{tag}", OAI_NS)
                        if e.text and e.text.strip()]

            types = " ".join(vals("type")).lower()
            if source == "ricabib" and "tesis" not in types and "thesis" not in types:
                continue

            title = (vals("title") or [None])[0]
            subjects = _dedupe_subjects(vals("subject"))
            blob = " ".join([title or "", " ".join(types), " ".join(vals("description")),
                             " ".join(vals("publisher"))]).lower()
            level = next((lbl for lbl, pat in LEVEL_RULES if re.search(pat, blob)), None)
            ident = next((v for v in vals("identifier") if v.startswith("http")), None)
            year = _year(vals("date"))

            for author in vals("creator"):
                rows.append({
                    "author": _flip_name(author),
                    "year": year,
                    "title": title,
                    "degree_program": level,
                    "subjects": subjects,
                    "url": ident,
                })

        t = root.find(".//oai:resumptionToken", OAI_NS)
        token = t.text if (t is not None and t.text) else None
        print(f"  page {page}: {len(rows)} thesis-author rows")
        if not token or (limit and len(rows) >= limit):
            break
        time.sleep(0.3)
    return rows


def collect(limit: int | None = None, source: str | None = None) -> list[dict]:
    order = [source] if source else ["nuclea", "ricabib"]
    rows = []
    for src in order:
        if not probe(src):
            continue
        try:
            rows = _harvest(src, limit)
            if rows:
                print(f"  harvested from {src}")
                break
        except Exception as exc:  # noqa: BLE001
            print(f"  {src} harvest failed: {exc}")

    if not rows:
        print("RICABIB/NUCLEA: nothing harvested (repository unreachable).")
        return []

    seen, uniq = set(), []
    for r in rows:
        k = (r["author"].lower(), r["year"], (r["title"] or "").lower()[:60])
        if k not in seen:
            seen.add(k)
            uniq.append(r)

    (RAW / "ricabib_theses.json").write_text(
        json.dumps(uniq, indent=1, ensure_ascii=False), encoding="utf-8")
    lv = sum(1 for r in uniq if r["degree_program"])
    print(f"RICABIB/NUCLEA: {len(uniq)} theses, "
          f"{len({r['author'].lower() for r in uniq})} distinct authors "
          f"({lv} with a detected degree level) -> data/raw/ricabib_theses.json")
    return uniq


if __name__ == "__main__":
    args = sys.argv[1:]
    src = args[args.index("--source") + 1] if "--source" in args else None
    lim = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    if "--probe" in args:
        for s in ([src] if src else ["nuclea", "ricabib"]):
            probe(s)
    else:
        collect(lim, src)
