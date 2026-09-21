# Runbook — enrich the "unmatched graduates"

**Goal.** Find a current (or last-known, or *deceased*) location for each IB *grado* graduate
who has **no record** in the atlas. Work is resumable: a ledger tracks who is done.

Run this whenever you have budget. Each run processes one batch, updates the ledger,
appends confirmed finds to `data/found_alumni.csv`, and stops. Re-run to continue.

## Files

| File | Role |
|---|---|
| `data/enrichment/ledger.json` | the work queue + every result. **Local only (gitignored).** |
| `data/found_alumni.csv` | confirmed finds, same schema as `manual_alumni.csv`. The builder merges it. |
| `egresadosGradoIB1958-2014.docx` | original source list (local, gitignored). Not needed — the ledger already has every name. |

Ledger entry shape:
```
{ "docx_name","surname","given","grad_year","program","degree_program",
  "status": "pending|found|probable|not_found|deceased",
  "attempts": int, "last_attempt": "YYYY-MM-DD",
  "result": null | { "employer","city","country","role","evidence_year","links","confidence","notes" } }
```

## One batch

1. Load `data/enrichment/ledger.json`. Take the next **20** people with
   `status == "pending"` (they are pre-sorted newest-cohort-first — keep that order;
   recent grads are far more findable and more map-relevant).
   Also re-try `status == "not_found"` entries whose `last_attempt` is > 12 months ago,
   but only after the pending queue is empty.

2. For each person, search **public sources**, stopping at the first solid hit.
   Their identity anchor is: graduated from **Instituto Balseiro** (Bariloche, Argentina),
   program + year from the ledger. Spanish names: `given` may hold 2 names, `surname`
   may hold 2 surnames — try combinations.

   Tiered search (cheap → thorough):
   a. **ORCID** — `curl -s -H "Accept: application/json" "https://pub.orcid.org/v3.0/search?q=given-names:<first>+AND+family-name:<last>"`.
      For each hit id: `.../v3.0/<id>/employments` and `.../<id>/educations`.
      An education at Balseiro / Instituto Balseiro / Universidad Nacional de Cuyo (Bariloche) /
      Centro Atómico Bariloche / CNEA ⇒ almost certainly them. Take the ongoing employment
      (end-date null); note its year.
   b. **OpenAlex** — `curl -s "https://api.openalex.org/authors?search=<Name>"`.
      Check `last_known_institutions` and recent `affiliations` (they carry years + country).
      Confirm with a work whose abstract/venue ties to Bariloche, or a rare-name exact match.
   c. **Web search** — try, in order:
      `"<First Last>" Instituto Balseiro` · `"<First Last>" Bariloche físico|ingeniero` ·
      `"<First Last>" CNEA|CONICET` · `"<First Last>" <likely field> profesor|research` ·
      for pre-1985 cohorts also `"<First Last>" obituario|obituary|falleció|in memoriam`.
      A **LinkedIn snippet in the search results is OK to read**; do **not** fetch linkedin.com.
   d. **Wikipedia / Wikidata**, **CONICET** (`bicyt.conicet.gov.ar` blocks fetch — use search snippets),
      university faculty pages, `prabook`, `academictree`, AFA (Asociación Física Argentina) memorials.

3. **Disambiguation guards — do NOT guess.**
   - Accept a location only with **either** (i) an explicit IB/Bariloche/CNEA link on the profile,
     **or** (ii) a genuinely rare name + a career consistent with the program and graduation year
     (born ≈ grad_year − 23; career starts after graduation).
   - Common name + no IB signal ⇒ `not_found`.
   - Note the **year** of every piece of evidence.

4. Set the ledger entry:
   - `status = "found"` — IB-linked, dated current/last affiliation. Fill `result`, `confidence:"confirmed"`.
   - `status = "probable"` — strong unique-name + consistent career, no explicit IB link.
     Fill `result`, `confidence:"probable"`. **Do not** write these to the CSV.
   - `status = "deceased"` — with year + source. `result` location = last known post.
   - `status = "not_found"` — nothing solid. `result` stays null.
   - Always bump `attempts` and set `last_attempt` to today (`date +%F`).

5. For every `status == "found"` (and `deceased` with a known last post), **append one row**
   to `data/found_alumni.csv`:
   ```
   name,grad_year,degree_program,field,role,employer,city,country,lat,lon,links,notes
   ```
   - `name`: `"<Given> <Surname>"` in natural order (not "Surname, Given"). Never start with `#`.
   - `grad_year`, `degree_program`: from the ledger. `field`: leave blank unless certain.
   - `employer,city,country,role`: the current (or last-known) ones. Plain ASCII, no accents.
   - `lat,lon`: blank (builder geocodes).
   - `links`: space-separated proof URLs.
   - `notes`: `enrichment: <evidence>, <year>; confidence=<confirmed|deceased>`
   - Quote any field containing a comma.
   - If the name is already a row in `found_alumni.csv` or `manual_alumni.csv`, **edit that row**, don't duplicate.

6. Write `data/enrichment/ledger.json` back (same structure, updated entries).

7. **Stop.** Print a short summary: how many processed this run, the found/probable/not_found/deceased
   split, and the running totals (`grep -c` style from the ledger). Do **not** run the pipeline,
   do **not** git commit.

## Progress check (any time)

```bash
python3 - <<'EOF'
import json,collections
L=json.load(open('data/enrichment/ledger.json'))
c=collections.Counter(p['status'] for p in L['people'])
print(L['meta']['total'],'total :',dict(c))
EOF
```

## When the queue is drained

All entries are `found`/`probable`/`not_found`/`deceased`. Report the final tally and stop
scheduling. A human then reviews `data/found_alumni.csv`, runs `python3 scripts/build_dataset.py`,
and commits.
