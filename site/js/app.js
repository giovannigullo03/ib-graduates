/* Balseiro Alumni Atlas — front-end (vanilla JS + Leaflet) */
'use strict';

const DATA_URL = 'data/alumni.json';
const ORCID_BASE = 'https://orcid.org/';
const SCHOLAR_BASE = 'https://scholar.google.com/citations?user=';

let ALUMNI = [];
let META = {};
let map, cluster;

const LEVEL_SHORT = {
  'Doctorate (PhD)': 'PhD', "Master's": 'MSc',
  'Specialization / diploma': 'Spec.', 'Engineering degree': 'Eng.',
  'Physics degree (Licenciatura)': 'Lic.',
};

const SOURCE_LABELS = {
  wikidata: 'Wikidata', orcid: 'ORCID', openalex: 'OpenAlex (inferred)',
  reviewed: 'OpenAlex (reviewed)', inspire: 'INSPIRE-HEP', ads: 'NASA ADS', lens: 'Patents (Lens)',
  wikipedia: 'Wikipedia', ricabib: 'IB thesis repo', manual: 'Added by hand',
  linkedin: 'LinkedIn profile',
};

const FACETS = [
  { key: 'discipline', label: 'Research field',      values: p => p.discipline ? [p.discipline] : [], open: true },
  { key: 'sector',     label: 'Type of employer',    values: p => p.sector ? [p.sector] : [],         open: true },
  { key: 'country',    label: 'Country (now)',       values: p => p.country ? [p.country] : [],       open: true },
  // anywhere their career took them, not just where they are today
  { key: 'career_country', label: 'Country (ever worked in)', open: false,
    values: p => [...new Set((p.career || []).map(c => c.country).filter(Boolean))] },
  { key: 'levels',     label: 'Degree at Balseiro',  values: p => p.levels || [],                     open: true },
  { key: 'program',    label: 'Degree subject',      values: p => p.program ? [p.program] : [],       open: false },
  // Self-reported skills (LinkedIn). Thousands of distinct values, so only the
  // most common ones are worth a checkbox — the search box covers the rest.
  { key: 'skills',     label: 'Skills',              values: p => p.skills || [], open: false, limit: 40 },
  { key: 'grad_decade',label: 'Graduation decade',   values: p => p.grad_decade ? [p.grad_decade + 's'] : [], open: false },
  { key: 'confidence', label: 'Confidence',          values: p => p.confidence ? [p.confidence] : [], open: false },
  { key: 'sources',    label: 'Data source',         values: p => (p.sources || []).map(s => SOURCE_LABELS[s] || s), open: false },
];

const state = {
  q: '',
  region: 'all',
  mappedOnly: false,
  includeInferred: false,   // confirmed-only by default
  facets: Object.fromEntries(FACETS.map(f => [f.key, new Set()])),
  view: 'map',
  sort: { key: 'name', dir: 1 },
  selected: null,           // the person whose trajectory is drawn, if any
};

/* ------------------------------------------------------------------ */
/* boot                                                                */
/* ------------------------------------------------------------------ */
fetch(DATA_URL)
  .then(r => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  })
  .then(payload => {
    META = payload.meta || {};
    ALUMNI = payload.alumni || [];
  })
  .catch(err => {
    document.getElementById('main').innerHTML =
      `<div style="padding:40px;max-width:640px">
         <h2>Could not load the alumni data</h2>
         <p>Expected <code>${DATA_URL}</code> (served over http, not file://). Run the pipeline first:</p>
         <pre>python3 scripts/run_all.py
python3 serve.py</pre>
         <p style="color:#a33">${esc(String(err))}</p>
       </div>`;
    throw err;
  })
  .then(() => {
    initMap();
    buildHeadline();
    buildAbout();
    buildFacets();
    wireControls();
    refresh();
  })
  .catch(err => console.error('render error:', err));

/* ------------------------------------------------------------------ */
/* map                                                                 */
/* ------------------------------------------------------------------ */
function initMap() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  map = L.map('map', { worldCopyJump: true, minZoom: 2 }).setView([20, 5], 2);
  // Esri's gray canvas — muted, good for data overlays, and free with no API key.
  const style = dark ? 'World_Dark_Gray_Base' : 'World_Light_Gray_Base';
  const esri = L.tileLayer(
    `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/${style}/MapServer/tile/{z}/{y}/{x}`,
    { attribution: 'Tiles &copy; Esri', maxZoom: 16 }
  );
  esri.on('tileerror', () => {
    if (map.hasLayer(esri)) map.removeLayer(esri);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      { attribution: '&copy; OpenStreetMap contributors', maxZoom: 19 }).addTo(map);
  });
  esri.addTo(map);

  cluster = L.markerClusterGroup({
    maxClusterRadius: 44,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
  });
  map.addLayer(cluster);

  // home base
  L.circleMarker([-41.1335, -71.4281], {
    radius: 7, weight: 2, color: '#a51f1f', fillColor: '#d64545', fillOpacity: 0.9,
  }).addTo(map).bindPopup(
    '<div class="pp"><div class="pp-name">Instituto Balseiro</div>' +
    '<div class="pp-role">San Carlos de Bariloche, Río Negro, Argentina</div>' +
    '<div class="pp-line">Centro Atómico Bariloche — where every person on this map studied.</div></div>'
  );

  const ZoomBtn = L.Control.extend({
    options: { position: 'topleft' },
    onAdd() {
      const b = L.DomUtil.create('button', 'leaflet-bar');
      b.title = 'Zoom to current results';
      b.textContent = '⤢';
      Object.assign(b.style, { width: '34px', height: '34px', fontSize: '16px', background: 'var(--surface)', color: 'var(--text)' });
      L.DomEvent.on(b, 'click', e => { L.DomEvent.stop(e); zoomToResults(); });
      return b;
    },
  });
  map.addControl(new ZoomBtn());
}

const IB_LATLON = [-41.1335, -71.4281];

/* The selected person's trajectory lives in its own layer attached straight to
   the map — never to the cluster group. Marker popups used to carry it, but a
   clustered marker is removed from the map as soon as you zoom out, which
   closed the popup and took the whole trajectory with it exactly when you were
   trying to see all of it. */
let selectionLayer = null;
const TRAJ = '#d64545';

// Career stops with known coordinates, oldest first (IB itself is added as
// the implicit starting point everywhere, so callers never need to include it).
function trajectoryStops(p) {
  return (p.career || [])
    .filter(c => c.lat != null && c.lon != null)
    .slice()
    .sort((a, b) => (a.start || '') > (b.start || '') ? 1 : -1);
}

function stopLabel(c) {
  const yr = c.start ? ` (${c.start}${c.current ? '–now' : c.end ? '–' + c.end : ''})` : '';
  return esc(c.institution) + esc(yr);
}

// Every point the trajectory should pass through: Balseiro, then each dated
// stop with coordinates, then where they are now.
function trajectoryPoints(p) {
  const stops = trajectoryStops(p);
  const pts = [IB_LATLON, ...stops.map(s => [s.lat, s.lon])];
  if (p.lat != null && p.lon != null) {
    const last = pts[pts.length - 1];
    if (last[0] !== p.lat || last[1] !== p.lon) pts.push([p.lat, p.lon]);
  }
  return { stops, pts };
}

function markerFor(p) {
  const abroad = p.country && p.country !== 'Argentina';
  const inferred = p.confidence === 'inferred';
  const selected = state.selected === p;
  const m = L.circleMarker([p.lat, p.lon], {
    radius: selected ? 8 : (inferred ? 5 : 6),
    weight: selected ? 3 : 1.5,
    color: selected ? TRAJ : (abroad ? '#c98432' : '#3f6fae'),
    fillColor: abroad ? '#e0a458' : '#6c9bd1',
    fillOpacity: inferred ? 0.25 : 0.85,
  });
  m.bindTooltip(p.name, { direction: 'top', offset: [0, -4] });
  m.on('click', () => selectPerson(p, { fit: false }));
  return m;
}

/* ------------------------------------------------------------------ */
/* selection — trajectory + detail panel                               */
/* ------------------------------------------------------------------ */
function drawSelection(p) {
  if (selectionLayer) { map.removeLayer(selectionLayer); selectionLayer = null; }
  if (!p) return;
  const { stops, pts } = trajectoryPoints(p);
  const layers = [];

  if (pts.length > 1) {
    // a dark halo under the line keeps it readable over both map themes
    layers.push(L.polyline(pts, { color: '#000', opacity: 0.2, weight: 6, interactive: false }));
    layers.push(L.polyline(pts, {
      color: TRAJ, weight: 2.5, opacity: 0.95, dashArray: '7 5', interactive: false,
    }));
  }

  layers.push(L.circleMarker(IB_LATLON, {
    radius: 6, weight: 2, color: '#a51f1f', fillColor: TRAJ, fillOpacity: 0.95,
  }).bindTooltip('Instituto Balseiro — where the trajectory starts', { direction: 'top' }));

  stops.forEach(s => layers.push(
    L.circleMarker([s.lat, s.lon], {
      radius: 5, weight: 1.5, color: TRAJ, fillColor: '#f2a6a6', fillOpacity: 0.95,
    }).bindTooltip(stopLabel(s), { direction: 'top', sticky: true })
  ));

  if (p.lat != null && p.lon != null) {
    layers.push(L.circleMarker([p.lat, p.lon], {
      radius: 9, weight: 3, color: TRAJ, fillColor: '#fff', fillOpacity: 0.95,
    }).bindTooltip(`${esc(p.name)} — now`, { direction: 'top' }));
  }

  selectionLayer = L.layerGroup(layers).addTo(map);
}

function fitSelection(p) {
  if (!p) return;
  const { pts } = trajectoryPoints(p);
  // keep the framed trajectory clear of the detail panel on the right
  const panel = document.getElementById('selection');
  const wide = window.innerWidth > 820 && panel && !panel.hidden;
  const opts = {
    maxZoom: 8,
    paddingTopLeft: [30, 30],
    paddingBottomRight: [wide ? panel.offsetWidth + 30 : 30, 30],
  };
  if (pts.length > 1) map.fitBounds(L.latLngBounds(pts), opts);
  else if (pts.length === 1) map.setView(pts[0], 5);
}

function renderSelectionPanel(p) {
  const host = document.getElementById('selection');
  if (!p) { host.hidden = true; host.innerHTML = ''; return; }
  const { pts } = trajectoryPoints(p);
  const traceable = pts.length > 1;
  host.hidden = false;
  host.innerHTML = `
    <div class="sel-head">
      <button class="sel-fit" type="button" ${traceable ? '' : 'disabled'}
        title="${traceable ? 'Zoom the map to the whole trajectory'
                           : 'No mapped trajectory for this person'}">⤢ Fit trajectory</button>
      <button class="sel-close" type="button" title="Clear selection">✕</button>
    </div>
    <div class="sel-body">${popupHtml(p)}</div>
    ${traceable ? '' : `<p class="sel-note">No dated career stops with coordinates —
      only their current location is on the map.</p>`}`;
  host.querySelector('.sel-close').addEventListener('click', clearSelection);
  const fit = host.querySelector('.sel-fit');
  if (traceable) fit.addEventListener('click', () => fitSelection(p));
}

function selectPerson(p, { fit = true } = {}) {
  state.selected = p;
  drawSelection(p);
  renderSelectionPanel(p);
  if (state.view !== 'map') setView('map');
  // redraw markers so the selected one is emphasised
  cluster.clearLayers();
  cluster.addLayers(filtered(null).filter(x => x.located).map(markerFor));
  // setView('map') calls invalidateSize on a timer; fit after it settles
  if (fit) setTimeout(() => fitSelection(p), 90);
  document.querySelectorAll('.people tr.sel').forEach(tr => tr.classList.remove('sel'));
  const row = document.querySelector(`.people tr[data-name="${cssEscape(p.name)}"]`);
  if (row) row.classList.add('sel');
}

function clearSelection() {
  state.selected = null;
  drawSelection(null);
  renderSelectionPanel(null);
  document.querySelectorAll('.people tr.sel').forEach(tr => tr.classList.remove('sel'));
  refresh();
}

function cssEscape(s) {
  return String(s).replace(/["\\]/g, '\\$&');
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function popupHtml(p) {
  const links = [];
  if (p.wikipedia) links.push(`<a href="${esc(p.wikipedia)}" target="_blank" rel="noopener">Wikipedia</a>`);
  if (p.orcid) links.push(`<a href="${ORCID_BASE}${esc(p.orcid)}" target="_blank" rel="noopener">ORCID</a>`);
  if (p.scholar_id) links.push(`<a href="${SCHOLAR_BASE}${esc(p.scholar_id)}" target="_blank" rel="noopener">Scholar</a>`);
  if (p.urls && p.urls[0]) links.push(`<a href="${esc(p.urls[0])}" target="_blank" rel="noopener">Homepage</a>`);
  if (p.wikidata) links.push(`<a href="${esc(p.wikidata)}" target="_blank" rel="noopener">Wikidata</a>`);

  const chips = [];
  if (p.discipline && p.discipline !== 'Not specified') chips.push(`<span class="chip">${esc(p.discipline)}</span>`);
  (p.levels || []).forEach(l => chips.push(`<span class="chip lvl">${esc(l)}${p.grad_year ? ` ’${String(p.grad_year).slice(2)}` : ''}</span>`));
  if (!p.levels && p.grad_year) chips.push(`<span class="chip muted">IB ${esc(p.grad_year)}</span>`);
  if (p.sector) chips.push(`<span class="chip muted">${esc(p.sector)}</span>`);
  if (p.concepts) p.concepts.slice(0, 3).forEach(c => chips.push(`<span class="chip muted">${esc(c)}</span>`));

  const metrics = [];
  if (p.works_count) metrics.push(`${p.works_count} papers`);
  if (p.h_index) metrics.push(`h-index ${p.h_index}`);

  const thesisLine = p.thesis
    ? `<div class="pp-line pp-thesis">IB thesis${p.thesis.year ? ` (${esc(p.thesis.year)})` : ''}: “${esc(p.thesis.title)}”</div>`
    : '';
  const advisorLine = p.advisors
    ? `<div class="pp-line pp-thesis">PhD advisor: ${esc(p.advisors.join(', '))}</div>`
    : '';

  let careerLine = '';
  if (p.career && p.career.length > 1) {
    const stops = p.career.slice().sort((a, b) => (a.start || '') > (b.start || '') ? 1 : -1)
      .map(c => {
        const yr = c.start ? c.start + (c.current ? '–now' : c.end ? '–' + c.end : '') : '';
        return `${esc(c.institution)}${yr ? ` <span class="yr">${esc(yr)}</span>` : ''}`;
      });
    careerLine = `<div class="pp-line pp-career">${stops.join(' → ')}</div>`;
  }

  const skillLine = (p.skills && p.skills.length)
    ? `<div class="pp-line pp-skills">${p.skills.slice(0, 8)
        .map(s => `<span class="chip skill">${esc(s)}</span>`).join('')}</div>`
    : '';
  const langLine = (p.languages && p.languages.length)
    ? `<div class="pp-line pp-lang">Languages: ${esc(p.languages.join(', '))}</div>`
    : '';

  const inferredNote = p.confidence === 'inferred'
    ? `<div class="pp-line pp-inferred">Inferred from affiliation data (${esc((p.sources || []).join(', '))}) — not independently confirmed.</div>`
    : '';

  const photo = p.image
    ? `<img class="pp-photo" src="${esc(p.image)}" alt="" loading="lazy"
         onerror="this.style.display='none'">`
    : `<div class="pp-photo"></div>`;

  return `<div class="pp">
    <div class="pp-head">${photo}
      <div>
        <div class="pp-name">${esc(p.name)}${p.deceased ? ' †' : ''}</div>
        <div class="pp-role">${esc(p.role || p.description || '')}</div>
      </div>
    </div>
    <div class="pp-line"><b>${esc(p.employer || 'Unknown employer')}</b><br>
      ${esc([p.city, p.country].filter(Boolean).join(', '))}${
        typeof p.loc_asof === 'number' ? ` <span class="asof">· as of ${p.loc_asof}</span>` : ''}</div>
    ${metrics.length ? `<div class="pp-line pp-metrics">${esc(metrics.join(' · '))}</div>` : ''}
    ${careerLine}
    ${thesisLine}
    ${advisorLine}
    ${chips.length ? `<div class="pp-chips">${chips.join('')}</div>` : ''}
    ${skillLine}
    ${langLine}
    ${links.length ? `<div class="pp-links">${links.join('')}</div>` : ''}
    ${inferredNote}
  </div>`;
}

/* ------------------------------------------------------------------ */
/* filtering                                                           */
/* ------------------------------------------------------------------ */
function matches(p, ignoreKey) {
  if (state.mappedOnly && !p.located) return false;
  if (!state.includeInferred && p.confidence === 'inferred'
      && !state.facets.confidence.has('inferred')) return false;
  if (state.q) {
    const hay = [
      p.name, p.employer, p.role, p.description, p.discipline, p.city, p.country,
      (p.keywords || []).join(' '), (p.fields || []).join(' '),
      (p.concepts || []).join(' '), (p.aka || []).join(' '),
      (p.skills || []).join(' '), (p.languages || []).join(' '),
      (p.career || []).map(c => c.institution).join(' '),
      p.thesis && p.thesis.title,
    ].join(' ').toLowerCase();
    if (!hay.includes(state.q)) return false;
  }
  if (state.region === 'ar' && p.country !== 'Argentina') return false;
  if (state.region === 'abroad' && (!p.country || p.country === 'Argentina')) return false;

  for (const f of FACETS) {
    if (f.key === ignoreKey) continue;
    const sel = state.facets[f.key];
    if (!sel.size) continue;
    const vals = f.values(p).map(String);
    if (!vals.some(v => sel.has(v))) return false;
  }
  return true;
}

function filtered(ignoreKey) {
  return ALUMNI.filter(p => matches(p, ignoreKey));
}

/* ------------------------------------------------------------------ */
/* headline + about                                                    */
/* ------------------------------------------------------------------ */
function buildHeadline() {
  const confirmed = ALUMNI.filter(p => p.confidence === 'confirmed');
  const mapped = confirmed.filter(p => p.located);
  const countries = new Set(mapped.map(p => p.country).filter(Boolean)).size;
  const inferred = ALUMNI.length - confirmed.length;
  document.getElementById('headline-stats').innerHTML = `
    <div class="stat"><div class="num">${confirmed.length}</div><div class="lbl">confirmed alumni</div></div>
    <div class="stat"><div class="num">${countries}</div><div class="lbl">countries</div></div>
    <div class="stat"><div class="num">${mapped.length}</div><div class="lbl">on the map</div></div>
    <div class="stat"><div class="num">+${inferred}</div><div class="lbl">inferred</div></div>`;
}

function buildAbout() {
  const s = META.sources || {};
  const srcLine = Object.entries(SOURCE_LABELS)
    .filter(([k]) => s[k]).map(([k, l]) => `${l} ${s[k]}`).join(' · ');
  document.getElementById('about-panel').innerHTML = `
    <h3>About this atlas</h3>
    <p>${esc(META.disclaimer || '')}</p>
    <ul>
      <li><b>${META.confirmed || 0}</b> of ${META.total} entries are confirmed alumni (Wikidata / ORCID / Wikipedia / IB thesis repository / hand-added); the rest are inferred from OpenAlex affiliation data.</li>
      <li>Only <b>${META.located}</b> have a known current location and appear on the map — the rest are confirmed by their thesis but we don't know where they are now. Tick "Only people placed on the map" to hide them.</li>
      <li>Sources: ${esc(srcLine)}</li>
      <li>Generated ${esc((META.generated || '').replace('T', ' ').replace('+00:00', ' UTC'))}</li>
      <li>Add people in <code>data/manual_alumni.csv</code>, vet inferred ones in <code>data/review_candidates.csv</code>, remove wrong ones in <code>data/blocklist.txt</code>, then re-run <code>python3 scripts/run_all.py</code>.</li>
    </ul>`;
}

/* ------------------------------------------------------------------ */
/* facets                                                              */
/* ------------------------------------------------------------------ */
function buildFacets() {
  const host = document.getElementById('facets');
  host.innerHTML = '';
  for (const f of FACETS) {
    const universe = new Map();
    for (const p of ALUMNI) for (const v of f.values(p)) universe.set(String(v), (universe.get(String(v)) || 0) + 1);
    let sorted = [...universe.entries()].sort((a, b) =>
      f.key === 'grad_decade' ? a[0].localeCompare(b[0]) : b[1] - a[1]);
    if (f.limit) sorted = sorted.slice(0, f.limit);

    const det = document.createElement('details');
    det.className = 'facet';
    det.open = f.open;
    det.innerHTML = `<summary>${f.label}</summary>
      <div class="facet-options">${sorted.map(([v]) => `
        <label class="opt" data-facet="${f.key}" data-value="${esc(v)}">
          <input type="checkbox">
          <span class="opt-label">${esc(v)}</span>
          <span class="count"></span>
        </label>`).join('')}</div>`;
    host.appendChild(det);
  }
  host.addEventListener('change', e => {
    const lab = e.target.closest('.opt');
    if (!lab) return;
    const set = state.facets[lab.dataset.facet];
    e.target.checked ? set.add(lab.dataset.value) : set.delete(lab.dataset.value);
    refresh();
  });
}

function updateFacetCounts() {
  for (const f of FACETS) {
    const subset = filtered(f.key);
    const counts = new Map();
    for (const p of subset) for (const v of f.values(p)) counts.set(String(v), (counts.get(String(v)) || 0) + 1);
    document.querySelectorAll(`.opt[data-facet="${f.key}"]`).forEach(lab => {
      const n = counts.get(lab.dataset.value) || 0;
      lab.querySelector('.count').textContent = n;
      lab.classList.toggle('disabled', n === 0 && !state.facets[f.key].has(lab.dataset.value));
    });
  }
}

/* ------------------------------------------------------------------ */
/* controls                                                            */
/* ------------------------------------------------------------------ */
function wireControls() {
  const search = document.getElementById('search');
  let searchTimer;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = search.value.trim().toLowerCase();
      refresh();
    }, 150);
  });

  document.getElementById('region-toggle').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    state.region = b.dataset.region;
    [...e.currentTarget.children].forEach(c => c.classList.toggle('active', c === b));
    refresh();
  });

  document.querySelector('#mapped-only input').addEventListener('change', e => {
    state.mappedOnly = e.target.checked;
    refresh();
  });

  document.querySelector('#include-inferred input').addEventListener('change', e => {
    state.includeInferred = e.target.checked;
    refresh();
  });

  document.getElementById('view-tabs').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    setView(b.dataset.view);
  });

  document.getElementById('about-toggle').addEventListener('click', () => {
    const p = document.getElementById('about-panel');
    p.hidden = !p.hidden;
  });

  document.getElementById('reset').addEventListener('click', () => {
    state.q = ''; search.value = '';
    state.region = 'all';
    state.selected = null;
    drawSelection(null);
    renderSelectionPanel(null);
    state.mappedOnly = false;
    state.includeInferred = false;
    document.querySelector('#mapped-only input').checked = false;
    document.querySelector('#include-inferred input').checked = false;
    document.querySelectorAll('#region-toggle button').forEach(b => b.classList.toggle('active', b.dataset.region === 'all'));
    for (const k in state.facets) state.facets[k].clear();
    document.querySelectorAll('#facets input[type=checkbox]').forEach(c => (c.checked = false));
    refresh();
  });

  const mfb = document.createElement('button');
  mfb.className = 'mobile-filter-btn';
  mfb.textContent = 'Filters';
  mfb.addEventListener('click', () => document.getElementById('sidebar').classList.toggle('open'));
  document.getElementById('view-map').appendChild(mfb);
}

function setView(v) {
  state.view = v;
  document.querySelectorAll('#view-tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  document.getElementById('view-map').hidden = v !== 'map';
  document.getElementById('view-insights').hidden = v !== 'insights';
  document.getElementById('view-list').hidden = v !== 'list';
  if (v === 'map') setTimeout(() => map.invalidateSize(), 50);
  if (v === 'insights') renderInsights();
  if (v === 'list') renderList();
}

/* ------------------------------------------------------------------ */
/* refresh + render                                                    */
/* ------------------------------------------------------------------ */
function refresh() {
  const res = filtered(null);
  const located = res.filter(p => p.located);

  // a selection that the current filters exclude would leave an orphan
  // trajectory on the map with nothing to explain it
  if (state.selected && !res.includes(state.selected)) {
    state.selected = null;
    drawSelection(null);
    renderSelectionPanel(null);
  }

  cluster.clearLayers();
  cluster.addLayers(located.map(markerFor));

  const unmapped = res.length - located.length;
  document.getElementById('unmapped-note').textContent =
    `${located.length} of ${res.length} shown on map` + (unmapped ? ` · ${unmapped} without a known location` : '');
  document.getElementById('result-count').textContent =
    `${res.length} ${res.length === 1 ? 'person' : 'people'}`;

  updateFacetCounts();
  if (state.view === 'insights') renderInsights();
  if (state.view === 'list') renderList();
}

function zoomToResults() {
  const pts = filtered(null).filter(p => p.located).map(p => [p.lat, p.lon]);
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 12 });
}

/* ------------------------------------------------------------------ */
/* insights                                                            */
/* ------------------------------------------------------------------ */
function tally(list, keyFn) {
  const m = new Map();
  for (const p of list) {
    for (const k of [].concat(keyFn(p))) {
      if (k == null || k === '') continue;
      m.set(k, (m.get(k) || 0) + 1);
    }
  }
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

function barChart(rows, { max, cls, onClick, facet }) {
  const top = max ? rows.slice(0, max) : rows;
  const peak = top.length ? top[0][1] : 1;
  return top.map(([label, n]) => `
    <div class="bar-row">
      <span class="bl" ${onClick ? `data-filter="${esc(label)}"` : ''}${
        facet ? ` data-facet-key="${esc(facet)}"` : ''}>${esc(label)}</span>
      <span class="bar-track"><span class="bar-fill ${cls || ''}" style="width:${(n / peak * 100).toFixed(1)}%"></span></span>
      <span class="bv">${n}</span>
    </div>`).join('');
}

/* Who left Argentina, who came back — read off the career histories.

   Only *post-graduation* stops count. Many profiles list jobs held before
   Balseiro (foreign students arrive with a career already started), and
   counting those would report their home country as a "destination". That
   means a known graduation year is required, so the card states its own
   denominator rather than implying it covers everyone. */
function migrationStats(list) {
  const out = { tracked: 0, stayed: 0, abroad: 0, returned: 0, firstAbroad: new Map() };
  for (const p of list) {
    if (!p.grad_year) continue;
    const stops = (p.career || [])
      .filter(c => c.country && (!c.start || +c.start >= p.grad_year))
      .sort((a, b) => (a.start || '') > (b.start || '') ? 1 : -1);
    if (!stops.length) continue;
    out.tracked++;
    const firstAbroad = stops.find(c => c.country !== 'Argentina');
    // where they are now: the person-level country wins (it is the resolved
    // current employer), else the last stop on record
    const now = p.country || stops[stops.length - 1].country;
    if (!firstAbroad) out.stayed++;
    else if (now === 'Argentina') out.returned++;
    else out.abroad++;
    if (firstAbroad) {
      out.firstAbroad.set(firstAbroad.country,
        (out.firstAbroad.get(firstAbroad.country) || 0) + 1);
    }
  }
  return out;
}

function renderInsights() {
  const list = filtered(null);
  const host = document.getElementById('view-insights');

  const withCountry = list.filter(p => p.country);
  const inAR = withCountry.filter(p => p.country === 'Argentina').length;
  const abroad = withCountry.length - inAR;
  const pctAbroad = withCountry.length ? Math.round(abroad / withCountry.length * 100) : 0;

  const countries = tally(list, p => p.country);
  const employers = tally(list, p => p.employer);
  const disciplines = tally(list, p => p.discipline);
  const sectors = tally(list, p => p.sector);
  const decades = tally(list, p => p.grad_decade ? p.grad_decade + 's' : null)
    .sort((a, b) => a[0].localeCompare(b[0]));
  const multiEmployers = employers.filter(([, n]) => n >= 2);
  const skills = tally(list, p => p.skills || []);
  const mig = migrationStats(list);
  // share of the people who actually went abroad that are back in Argentina
  const migLeft = mig.returned + mig.abroad;
  const migPct = migLeft ? Math.round(mig.returned / migLeft * 100) : 0;

  const splitBar = (parts) => {
    const total = parts.reduce((s, [, n]) => s + n, 0) || 1;
    return `<div class="split">${parts.map(([lbl, n, color]) =>
      `<span style="flex:${n};background:${color}" title="${esc(lbl)}: ${n}">${n / total > 0.08 ? n : ''}</span>`).join('')}</div>`;
  };

  host.innerHTML = `
   <div class="insights-grid">
    <div class="card">
      <h3>Argentina vs. abroad</h3>
      <p class="sub">of ${withCountry.length} people with a known current country</p>
      <div class="big-figure">${pctAbroad}%</div>
      <p class="callout">work outside Argentina${state.q || anyFacet() ? ' (within the current filter)' : ''}.</p>
      ${splitBar([['In Argentina', inAR, 'var(--argentina)'], ['Abroad', abroad, 'var(--abroad)']])}
    </div>

    <div class="card">
      <h3>Top destination countries</h3>
      <p class="sub">click a bar to filter the map</p>
      ${barChart(countries, { max: 12, onClick: true })}
    </div>

    <div class="card">
      <h3>Where they work — employers</h3>
      <p class="sub">${multiEmployers.length} institutions employ 2+ alumni · click to filter</p>
      ${barChart(employers, { max: 14, onClick: true })}
    </div>

    <div class="card">
      <h3>Research fields</h3>
      ${barChart(disciplines, { max: 12 })}
    </div>

    <div class="card">
      <h3>Type of employer</h3>
      ${barChart(sectors, { max: 8 })}
    </div>

    <div class="card">
      <h3>When they graduated</h3>
      <p class="sub">${list.filter(p => p.grad_year).length} with a known graduation year</p>
      ${barChart(decades, {})}
    </div>

    <div class="card">
      <h3>Leaving and coming back</h3>
      <p class="sub">${mig.tracked} people with a graduation year and a traceable
        career after it${migLeft ? ` · ${migLeft} of them worked abroad` : ''}</p>
      ${mig.tracked ? `
        <div class="big-figure">${migPct}%</div>
        <p class="callout">of the ${migLeft} who went abroad are back in Argentina.</p>
        ${splitBar([
          ['Never left Argentina', mig.stayed, 'var(--argentina)'],
          ['Went abroad, came back', mig.returned, 'var(--returned, #d99a2b)'],
          ['Abroad now', mig.abroad, 'var(--abroad)'],
        ])}
        <p class="sub legend-line">
          <span class="key" style="background:var(--argentina)"></span> never left
          <span class="key" style="background:var(--returned, #d99a2b)"></span> returned
          <span class="key" style="background:var(--abroad)"></span> abroad now
        </p>`
      : `<p class="callout">No traceable career histories in this selection.</p>`}
    </div>

    <div class="card">
      <h3>First stop abroad</h3>
      <p class="sub">first country worked in after graduating · click to filter</p>
      ${mig.firstAbroad.size
        ? barChart([...mig.firstAbroad.entries()].sort((a, b) => b[1] - a[1]),
                   { max: 10, onClick: true, facet: 'career_country' })
        : `<p class="callout">Nobody in this selection has a recorded stop abroad.</p>`}
    </div>

    <div class="card">
      <h3>Most common skills</h3>
      <p class="sub">self-reported · ${list.filter(p => p.skills).length} people list any · click to filter</p>
      ${skills.length
        ? barChart(skills, { max: 14, onClick: true, facet: 'skills' })
        : `<p class="callout">No skills recorded in this selection.</p>`}
    </div>
   </div>`;

  host.querySelectorAll('[data-filter]').forEach(el => {
    el.addEventListener('click', () => {
      const val = el.dataset.filter;
      // a bar can name its own facet ("first stop abroad" -> country even when
      // nobody currently works there); otherwise fall back to detection
      const key = el.dataset.facetKey
        || (countries.some(([c]) => c === val) ? 'country' : null);
      if (key && state.facets[key]) {
        state.facets[key].clear();
        state.facets[key].add(val);
        syncCheckboxes();
      } else {
        document.getElementById('search').value = val;
        state.q = val.toLowerCase();
      }
      setView('map');
      refresh();
      setTimeout(zoomToResults, 150);
    });
  });
}

function anyFacet() { return Object.values(state.facets).some(s => s.size); }

function syncCheckboxes() {
  document.querySelectorAll('#facets .opt').forEach(lab => {
    lab.querySelector('input').checked = state.facets[lab.dataset.facet].has(lab.dataset.value);
  });
}

/* ------------------------------------------------------------------ */
/* list                                                                */
/* ------------------------------------------------------------------ */
const COLS = [
  { key: 'name', label: 'Name' },
  { key: 'role', label: 'Role' },
  { key: 'employer', label: 'Employer' },
  { key: 'country', label: 'Location' },
  { key: 'discipline', label: 'Field' },
  { key: 'levels', label: 'Degree' },
  { key: 'grad_year', label: 'IB' },
];

function renderList() {
  const rows = filtered(null).slice().sort((a, b) => {
    const { key, dir } = state.sort;
    let x = a[key], y = b[key];
    if (Array.isArray(x)) x = x.join(', ');
    if (Array.isArray(y)) y = y.join(', ');
    if (x == null) return 1;
    if (y == null) return -1;
    if (typeof x === 'string') return x.localeCompare(y) * dir;
    return (x - y) * dir;
  });

  const body = rows.map(p => {
    const loc = [p.city, p.country].filter(Boolean).join(', ') || '—';
    const tag = p.country === 'Argentina' ? 'tag-ar' : (p.country ? 'tag-abroad' : '');
    const link = p.wikipedia || (p.orcid ? ORCID_BASE + p.orcid : (p.urls && p.urls[0])) || p.wikidata;
    const stops = trajectoryPoints(p).pts.length;
    const trail = stops > 1 ? `<span class="row-trail" title="${stops} mapped stops">↗</span>` : '';
    return `<tr data-name="${esc(p.name)}" class="${state.selected === p ? 'sel' : ''}"
        title="Show on the map">
      <td class="nm">${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(p.name)}</a>` : esc(p.name)}${trail}</td>
      <td class="subtle">${esc(p.role || p.description || '—')}</td>
      <td>${esc(p.employer || '—')}</td>
      <td class="${tag}">${esc(loc)}</td>
      <td class="subtle">${esc(p.discipline === 'Not specified' ? '—' : p.discipline || '—')}</td>
      <td class="subtle">${p.levels ? esc(p.levels.map(l => LEVEL_SHORT[l] || l).join(', ')) : '—'}</td>
      <td class="subtle">${p.grad_year || '—'}</td>
    </tr>`;
  }).join('');

  document.getElementById('list-container').innerHTML = `
    <table class="people">
      <thead><tr>${COLS.map(c =>
        `<th data-key="${c.key}">${c.label}${state.sort.key === c.key ? (state.sort.dir > 0 ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>
      <tbody>${body}</tbody>
    </table>`;

  document.querySelectorAll('.people th').forEach(th => th.addEventListener('click', () => {
    const k = th.dataset.key;
    state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : 1 };
    renderList();
  }));

  // Clicking a row jumps to the map with that person's trajectory drawn and
  // framed. The name is still a plain external link, so let it through.
  const byName = new Map(rows.map(p => [p.name, p]));
  document.querySelector('.people tbody').addEventListener('click', e => {
    if (e.target.closest('a')) return;
    const tr = e.target.closest('tr');
    if (!tr) return;
    const p = byName.get(tr.dataset.name);
    if (p) selectPerson(p, { fit: true });
  });
}
