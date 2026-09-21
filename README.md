# Balseiro Alumni Atlas

An interactive map of where **Instituto Balseiro** (Bariloche, Argentina) graduates
live and work — built to help with a job / PhD / master's search: see which
institutions and companies hire Balseiro people, in which countries and fields,
and find names to reach out to.

## How it works

```
scripts/collect_*.py  ─┐
data/manual_alumni.csv ├─►  scripts/build_dataset.py  ─►  site/data/alumni.json  ─►  site/index.html
data/review_candidates.csv ┘        ▲
data/linkedin/*.csv ───────┘   data/blocklist.txt
```

Most alumni are gathered from **public, structured sources**. One source is
different: LinkedIn profile exports, which are produced outside this repo and
dropped into `data/linkedin/` by hand (see *LinkedIn exports* below).

| Source | Contributes | Confidence |
|--------|-------------|------------|
| [Wikidata](https://www.wikidata.org) | `educated at` / `employer` / `affiliation` = Instituto Balseiro; employers (often with coordinates), fields, photos, Wikipedia links | confirmed |
| [ORCID](https://orcid.org) | Anyone with Instituto Balseiro in their **education** history — grad year, degree, current employer + city/country, keywords. Candidate iDs come from ORCID's own search **and** from OpenAlex. | confirmed |
| [OpenAlex](https://openalex.org) | ~2,900 authors who ever published with a Balseiro affiliation. ORCID ones are verified above; high-scoring ORCID-less ones are added directly; the rest go to a review queue. Also adds publication counts, h-index and research concepts to everyone. | inferred (unless verified) |
| [INSPIRE-HEP](https://inspirehep.net) | The particle / string / gravitation / cosmology / astro-particle slice. Full **career history** (every institution with years + coordinates) and the person's **PhD advisor**. Confirmed when their earliest recorded position was at Balseiro. | inferred / confirmed |
| [NASA ADS](https://ui.adsabs.harvard.edu) | Astronomy / astrophysics authors — most-recent-paper affiliation. **Needs a free API token** (see `scripts/collect_ads.py`); silently skipped without one. | inferred |
| Thesis reconciliation (`reconcile_theses.py`) | For thesis-roster people with no location: an ORCID name search (kept only when Balseiro is in the record), then a match of the exact thesis title to its OpenAlex `dissertation` work → that author's current institution. | confirmed |
| Wikipedia | Curated *Alumnado / Profesores del Instituto Balseiro* categories — bios and portraits | confirmed |
| IB thesis repository (RICABIB, harvested via the public [NUCLEA](https://nuclea.cnea.gob.ar) mirror) | Author + year + title of ~1,000 IB theses — the authoritative roster. No employer, so many are confirmed-but-unmapped. | confirmed |
| LinkedIn exports (`data/linkedin/`) | Self-reported current employer + city, the **full position history** with locations and dates, degree and graduation year, skills and languages. Confirmed when the person lists Instituto Balseiro in their own education history. Local only — never committed. | confirmed / enrichment-only |
| `data/manual_alumni.csv` | Anyone **you** add by hand | confirmed |

The site shows **confirmed people by default** (toggle "Include inferred (OpenAlex-only) entries" in the sidebar for the rest).

`build_dataset.py` merges duplicates (by ORCID iD, then by normalised name),
resolves each person's location from Wikidata/OpenAlex coordinates or
[Nominatim](https://nominatim.org) geocoding (cached), classifies research field
and employer sector, tags `confidence`, and writes a single `site/data/alumni.json`.

## Setup

```bash
pip install -r requirements.txt        # just `requests`
python3 scripts/run_all.py             # first run ≈ 15–20 min (ORCID verification + geocoding)
python3 serve.py                       # -> http://localhost:8000  (fetch() needs http, not file://)
```

Re-running is fast because API responses and geocoding live in `data/`
(`data/raw/`, `data/geocode_cache.json`). `--fresh` ignores the caches:

```bash
python3 scripts/run_all.py --fresh
```

## Growing the database

| Want more… | Do this |
|---|---|
| **people, automatically** | Re-run `run_all.py` periodically to pick up newly-published profiles and theses. |
| **inferred people confirmed** | Edit `data/review_candidates.csv` (below). |
| **people you know personally** | Edit `data/manual_alumni.csv` (below) — highest precision, and the only way to *place* a thesis-roster name on the map. |
| **locations for the thesis-roster names** | Drop LinkedIn profile exports into `data/linkedin/` (below). This is what moves people off the "no known location" pile in bulk. |
| **fewer false positives** | Add names to `data/blocklist.txt`. |

The thesis roster is harvested from **NUCLEA** (`nuclea.cnea.gob.ar`, public) by
default; if you're on the CNEA network and want the primary RICABIB source,
`python3 scripts/collect_ricabib.py --source ricabib`.

**OpenAlex now meters its free API at ~$0.10/day (~100 requests).** The main
collector is well under that, but `reconcile_theses.py`'s OpenAlex pass processes
only ~40 thesis authors per run — just re-run `run_all.py` over a few days and it
resumes from the on-disk cache. The ORCID pass has no such limit.

### `data/manual_alumni.csv` — only `name` is required

```csv
name,grad_year,degree_program,field,role,employer,city,country,lat,lon,links,notes
María Pérez,2018,Physics,Quantum optics,PhD student,ETH Zürich,Zürich,Switzerland,,,https://maria.example,met at a conference
```

Leave `lat`/`lon` blank and the builder geocodes `employer, city, country`.
If a name matches someone already pulled from another source, your fields are
merged in rather than duplicated. Then re-run `python3 scripts/run_all.py`.

### `data/linkedin/` — LinkedIn exports

Drop one or more profile-export CSVs in there and run
`python3 scripts/collect_linkedin.py` (or just `run_all.py`). Files are merged
and de-duplicated by LinkedIn member id, keeping the **richest** row per person,
so adding a later batch that overlaps an earlier one is always safe — re-run and
the stubs are upgraded in place. Exports with a different schema (a plain
name/URL target list, for instance) are detected and skipped.

What each profile contributes: current employer, city and country; the full
position history with locations and dates; degree, field and graduation year;
skills and languages.

Two rules keep this honest:

* **Only a Balseiro entry in the person's own *education* history creates a new
  person.** A CAB/CNEA job, a mention in the headline, or a stub row with
  nothing but a name may enrich someone already known from another source, but
  never adds a name on its own — working at the Centro Atómico is not the same
  as having studied at the Instituto.
* **A self-reported current job outranks every automatic source** (but not a
  hand-checked row in `manual_alumni.csv`). An ORCID employment record goes
  stale; an ADS or OpenAlex affiliation is only where someone was when they last
  published. This is how people who moved back to Argentina stop showing up at
  their old foreign institution.

Privacy: the exports carry e-mail addresses, phone numbers and birthdays.
`collect_linkedin.py` drops those columns outright, `data/linkedin/` and
`data/raw/linkedin_profiles.json` are git-ignored, and profile URLs stay out of
the published dataset unless you flip `PUBLISH_LINKEDIN_URLS` in
`build_dataset.py`. Nothing personal beyond employer/city/degree reaches
`site/data/alumni.json`.

Note that career-history stops are geocoded at most 150 new ones per run (they
are only used to draw trajectory lines, so they are not worth a long wait). Just
re-run `build_dataset.py` a few times until it stops saying "budget reached".

### `data/review_candidates.csv` — vetting the inferred entries

Every run regenerates this file with mid-confidence people OpenAlex thinks
studied at Balseiro but that couldn't be auto-verified. For anyone real, set
`keep` to `y` (and fill `city`/`country` or `lat`,`lon` if the listed
institution looks wrong). Your `keep` marks are preserved across future runs.

## The website

- **Map** — clustered markers; blue = currently in Argentina, amber = abroad.
  Click a marker for role, employer, graduation year, field and links
  (Wikipedia / ORCID / Google Scholar / homepage).
- **Filters** — text search; Argentina / Abroad toggle; facets for research field,
  employer type, degree program, graduation decade and country, each with live counts.
- **Insights** — share working abroad, top destination countries, institutions
  employing 2+ alumni (outreach targets), field and employer-type breakdowns,
  graduations by decade. Bars are clickable.
- **List** — sortable table of everyone, including people with no mapped location.

Pure static files (Leaflet + Leaflet.markercluster from CDN, CARTO basemap).
Deploy by copying `site/` to GitHub Pages, Netlify, etc.

## Caveats

Coverage is **partial** and skewed toward people with an academic/research web
presence — early graduates are under-represented, and so were people who went
into private industry until the LinkedIn exports started filling that gap.
Locations are the most recent employer on record and can be stale. Treat it as a
research starting point, not a directory.

The **Leaving and coming back** and **First stop abroad** panels only count
people with a known graduation year, and only the jobs they held *after* it —
otherwise the home country of a foreign student who arrived with a career
already started would be reported as a destination. Both panels say how many
people they are actually based on.

Skills and languages are **self-reported** and unevenly filled in: read them as
"what this person chose to advertise", not as a measured distribution.

## Data & licensing

Wikidata (CC0), ORCID public records (CC0), Wikipedia (CC BY-SA), geocoding ©
OpenStreetMap contributors (ODbL). Respect each source's terms if you
redistribute the compiled dataset.
