// Synthesized Grids — Data Viewer (Step 11 redesign; Redo 9: Leaflet/OSM basemap)
// Plain vanilla JS, no frameworks/build step. Two kinds of data source:
//   - GET /data/...      : timestamp-independent JSON, computed live from grid-bundles/*.p by app.py
//   - GET /api/<id>/...  : live ground truth for one timestamp, served by app.py (Flask) straight
//                          from data/power_flow_results/<id>/{bus,line}.parquet
// Noise (0-5%) is applied HERE, client-side, at render time (see "Noise" section below) — the
// backend always returns clean values.
// PMU measurement table (bus vm_pu + va_degree at the current PMU-penetration level) is served by
// GET /api/<id>/pmu_readings?timestamp=...&penetration=... - parallel to the smart-meter table.
//
// Redo 9: the topology view is now a real Leaflet map with an OpenStreetMap tile basemap
// (previously a bare SVG plot on a blank background, using raw EPSG:2056-bbox-to-pixel math).
// Buses/lines/flow-arrows/PMU-markers are Leaflet layers (circleMarker/polyline/divIcon markers)
// positioned by real lat/lon (WGS84), not hand-computed SVG coordinates. Leaflet handles all
// pan/zoom/projection internally, so the old computeTransform()/toSvg()/setupPanZoom() hand-rolled
// coordinate math is gone entirely.

const state = {
  index: [],
  gridId: null,
  meta: null,
  topology: null,
  pmuPenetration: null,   // this grid's pmu_penetration.json
  noiseConfig: [],        // full noise-model-config.csv records (shared across grids)
  timeRange: null,        // {start, end, step_minutes, count} (shared across grids)
  timeIndex: 0,
  currentTimestampIso: null,
  atData: null,           // {timestamp, bus: {id: vm_pu}, line: {id: loading_percent}}
  layers: {                // independent show/hide map-layer toggles (Redo 4; prosumer added Redo 10)
    voltage: true,
    loading: true,
    flow: true,
    nodetype: true,
    pmu: true,
    prosumer: true,
  },
  voltageRange: { min: 0.95, max: 1.05 }, // adjustable color-scale window (Redo 4), persists across grids/time
  loadingRange: { min: 0, max: 100 },
  sizeScale: 1.0, // user-adjustable marker/line size multiplier (Redo 5), persists across grids/time
  noiseLevel: "0%",
  pmuLevel: "0%",
  busById: null,
  busDistance: null,      // {busId: hop count from ext_grid root, via lines+trafos}
  lineDownstreamBus: null, // {lineId: bus id of the farther-from-root endpoint}
  // Leaflet-specific (Redo 9)
  leafletMap: null,        // the single L.map instance, created once (initMap())
  layerGroups: null,       // {line, bus, flow, pmu} -> L.layerGroup, created once, cleared/rebuilt per grid
  busMarkers: null,        // {busId: L.CircleMarker}, rebuilt per grid / per size-slider change
  lineLayers: null,        // {lineId: L.Polyline}, rebuilt per grid / per size-slider change
  fitBounds: null,         // L.LatLngBounds of the current grid's buses, for the dblclick re-fit
  baseRadius: null,        // current bus circle-marker pixel radius (post sizeScale)
  strokeW: null,           // current line weight / bus-ring-width basis (post sizeScale), pixels
};

const el = (id) => document.getElementById(id);

async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let msg = `${res.status}`;
    try { const body = await res.json(); if (body.error) msg += `: ${body.error}`; } catch (e) {}
    throw new Error(`Failed to fetch ${path}: ${msg}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Deterministic client-side noise (design note, Step 11 redesign point E)
//
// The backend always returns CLEAN ground-truth values. Noise is added here, at render time,
// using a small deterministic PRNG seeded from a string key
//   `${gridId}:${quantity}:${idKind}:${id}:${timestampIso}:${noiseLevel}`
// so the SAME (grid, quantity, bus/line id, timestamp, noise level) combination always produces
// the SAME perturbed value if the user revisits it (slider back-and-forth, overlay toggling,
// etc.) — this is about a stable, reproducible *display*, not bit-parity with any Python
// precomputation (nothing in Python computes noise at this timestamp resolution; the noisy
// values here are computed nowhere else).
//
// PRNG: FNV-1a (32-bit) hashes the key string into an integer seed, mulberry32 turns that seed
// into a stream of uniform [0,1) draws, and Box-Muller turns two uniform draws into one
// standard-normal draw, scaled by the quantity's configured std (data/noise-model-config.csv).
// ---------------------------------------------------------------------------

function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// One standard-normal draw, deterministic given `key`.
function seededNormal(key) {
  const rng = mulberry32(fnv1a(key));
  const u1 = Math.max(rng(), 1e-12); // avoid log(0)
  const u2 = rng();
  return Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
}

function noiseKey(quantity, idKind, id) {
  return `${state.gridId}:${quantity}:${idKind}:${id}:${state.currentTimestampIso}:${state.noiseLevel}`;
}

function noiseCfg(deviceType, quantity, level) {
  return state.noiseConfig.find(
    (r) => r.device_type === deviceType && r.quantity === quantity && r.noise_level === level
  );
}

// Voltage overlay noise: modeled on the smart_meter/vm_pu entry (relative_to_value - fraction of
// |measured value|). This is the closest real sensor analogue for "voltage reading at a bus".
function noisyVoltage(vm, busId) {
  const cfg = noiseCfg("smart_meter", "vm_pu", state.noiseLevel);
  if (!cfg || cfg.std_value <= 0) return vm;
  const sigma = cfg.std_value * Math.abs(vm);
  return vm + seededNormal(noiseKey("vm_pu", "bus", busId)) * sigma;
}

// Line-loading overlay noise: data/noise-model-config.csv has no direct "loading_percent" entry.
// loading_percent is a linear function of current magnitude (loading% = I / I_rated * 100), so a
// *relative* current-measurement error translates directly into the same *relative* error on
// loading_percent. We reuse pmu/branch_i_ka's relative_to_value std as the closest available
// current-magnitude noise spec (documented assumption - see report).
function noisyLoading(pct, lineId) {
  const cfg = noiseCfg("pmu", "branch_i_ka", state.noiseLevel);
  if (!cfg || cfg.std_value <= 0) return pct;
  const sigma = cfg.std_value * Math.abs(pct);
  return pct + seededNormal(noiseKey("loading_percent", "line", lineId)) * sigma;
}

function noisySmartMeterVm(vm, loadId) {
  const cfg = noiseCfg("smart_meter", "vm_pu", state.noiseLevel);
  if (!cfg || cfg.std_value <= 0 || vm == null) return vm;
  const sigma = cfg.std_value * Math.abs(vm);
  return vm + seededNormal(noiseKey("vm_pu", "load", loadId)) * sigma;
}

// PMU voltage-magnitude noise: pmu/vm_pu is relative_to_value (fraction of |measured value|), same
// basis as smart_meter/vm_pu, just tighter (Step 9's IEEE C37.118.1-based PMU noise).
function noisyPmuVm(vm, busId) {
  const cfg = noiseCfg("pmu", "vm_pu", state.noiseLevel);
  if (!cfg || cfg.std_value <= 0 || vm == null) return vm;
  const sigma = cfg.std_value * Math.abs(vm);
  return vm + seededNormal(noiseKey("pmu_vm_pu", "bus", busId)) * sigma;
}

// PMU phase-angle noise: pmu/va_degree is ABSOLUTE (degrees), not relative_to_value - std_value
// is added directly, not scaled by |value| (unlike every other quantity here). Do not treat this
// like the relative quantities above.
function noisyPmuVa(va, busId) {
  const cfg = noiseCfg("pmu", "va_degree", state.noiseLevel);
  if (!cfg || cfg.std_value <= 0 || va == null) return va;
  const sigma = cfg.std_value; // absolute basis: std_value IS the sigma in degrees, no scaling
  return va + seededNormal(noiseKey("pmu_va_degree", "bus", busId)) * sigma;
}

function noisySmartMeterPQ(p, q, loadId) {
  const cfgP = noiseCfg("smart_meter", "p_mw", state.noiseLevel);
  const cfgQ = noiseCfg("smart_meter", "q_mvar", state.noiseLevel);
  const s = Math.sqrt(p * p + q * q);
  let np = p, nq = q;
  if (cfgP && cfgP.std_value > 0) {
    np = p + seededNormal(noiseKey("p_mw", "load", loadId)) * (cfgP.std_value * s);
  }
  if (cfgQ && cfgQ.std_value > 0) {
    nq = q + seededNormal(noiseKey("q_mvar", "load", loadId)) * (cfgQ.std_value * s);
  }
  return [np, nq];
}

// ---------------------------------------------------------------------------
// Diverging 3-stop color scales (Step 11 redesign point D)
// ---------------------------------------------------------------------------

const BLUE = [33, 102, 172];
const GREEN = [40, 180, 80];
const RED = [214, 69, 69];

function lerp(a, b, t) { return a + (b - a) * t; }
function lerpColor(c1, c2, t) {
  return [lerp(c1[0], c2[0], t), lerp(c1[1], c2[1], t), lerp(c1[2], c2[2], t)];
}
function rgbToCss(rgb) {
  return `rgb(${Math.round(rgb[0])},${Math.round(rgb[1])},${Math.round(rgb[2])})`;
}

// value clamped to [vMin, vMax]; vMid maps to green; linearly interpolated either side.
function colorScale3(value, vMin, vMid, vMax) {
  const v = Math.max(vMin, Math.min(vMax, value));
  if (v <= vMid) {
    const t = (v - vMin) / (vMid - vMin);
    return rgbToCss(lerpColor(BLUE, GREEN, t));
  }
  const t = (v - vMid) / (vMax - vMid);
  return rgbToCss(lerpColor(GREEN, RED, t));
}

function voltageColor(vm) {
  const { min, max } = state.voltageRange;
  return colorScale3(vm, min, (min + max) / 2, max);
}
function loadingColor(pct) {
  const { min, max } = state.loadingRange;
  return colorScale3(pct, min, (min + max) / 2, max);
}

// Exposed for a quick sanity check from the browser console / node, if ever needed.
window.__colorScale3 = colorScale3;

// ---------------------------------------------------------------------------
// Sidebar: grid list
// ---------------------------------------------------------------------------

async function loadIndex() {
  state.index = await fetchJSON(`/data/index.json`);
  renderGridList(state.index);
  el("grid-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    const filtered = state.index.filter((g) =>
      g.grid_id.toLowerCase().includes(q) ||
      g.category.toLowerCase().includes(q) ||
      g.voltage_level.toLowerCase().includes(q)
    );
    renderGridList(filtered);
  });
}

function renderGridList(rows) {
  const groups = {};
  for (const g of rows) {
    const key = `${g.voltage_level} — ${g.category}`;
    (groups[key] = groups[key] || []).push(g);
  }
  const container = el("grid-list");
  container.innerHTML = "";
  const sortedKeys = Object.keys(groups).sort();
  for (const key of sortedKeys) {
    const header = document.createElement("div");
    header.className = "group-header";
    header.textContent = key;
    container.appendChild(header);
    for (const g of groups[key]) {
      const item = document.createElement("div");
      item.className = "grid-item" + (g.grid_id === state.gridId ? " active" : "");
      item.innerHTML = `<span class="tag">${g.size_tier}</span>${g.grid_id.split("__").pop()}
        <div style="color:#8a93a0;">${g.n_bus} bus · ${g.n_line} line · ${g.n_load} load</div>`;
      item.addEventListener("click", () => selectGrid(g.grid_id));
      container.appendChild(item);
    }
  }
}

function filteredIndex() {
  const q = el("grid-search").value.trim().toLowerCase();
  if (!q) return state.index;
  return state.index.filter((g) =>
    g.grid_id.toLowerCase().includes(q) || g.category.toLowerCase().includes(q) || g.voltage_level.toLowerCase().includes(q)
  );
}

// ---------------------------------------------------------------------------
// Grid selection
// ---------------------------------------------------------------------------

async function selectGrid(gridId) {
  state.gridId = gridId;
  renderGridList(filteredIndex());

  el("loading-overlay").style.display = "flex";
  const base = `/data/${gridId}`;
  const [meta, topology, pmuPenetration] = await Promise.all([
    fetchJSON(`${base}/meta.json`),
    fetchJSON(`${base}/topology.json`),
    fetchJSON(`${base}/pmu_penetration.json`),
  ]);
  state.meta = meta;
  state.topology = topology;
  state.pmuPenetration = pmuPenetration;

  renderGridSummary();
  computeFlowTopology();
  renderTopologyStructure();
  fitToGrid();
  await refreshAtTimestamp();
  await refreshSmartMeterTable();
  await refreshPmuTable();
  el("loading-overlay").style.display = "none";
}

function renderGridSummary() {
  const m = state.meta;
  el("grid-summary").innerHTML = `
    <b>${m.grid_id.split("__").pop()}</b><br>
    ${m.voltage_level} · ${m.category} · ${m.size_tier}<br>
    n_bus=<b>${m.n_bus}</b>, n_line=<b>${m.n_line}</b>, n_trafo=<b>${m.n_trafo}</b>, n_load=<b>${m.n_load}</b>`;
}

// ---------------------------------------------------------------------------
// Time control (own section, decoupled from Scenario)
// ---------------------------------------------------------------------------

function timestampForIndex(i) {
  const start = new Date(state.timeRange.start);
  return new Date(start.getTime() + i * state.timeRange.step_minutes * 60000);
}

function formatTimestamp(date) {
  // Show the exact UTC instant (this is also exactly what's sent to the backend, avoiding any
  // timezone ambiguity) plus the weekday for a quick human read.
  const iso = date.toISOString().replace(".000Z", "Z");
  const weekday = date.toLocaleDateString("en-GB", { weekday: "short", timeZone: "UTC" });
  return `${iso}  (${weekday}, UTC)`;
}

function updateTimeDisplay() {
  const date = timestampForIndex(state.timeIndex);
  state.currentTimestampIso = date.toISOString();
  el("time-display").textContent = formatTimestamp(date);
}

function setupTimeControl() {
  const slider = el("time-slider");
  slider.max = String(state.timeRange.count - 1);
  slider.value = "0";
  el("time-start-label").textContent = state.timeRange.start;
  el("time-end-label").textContent = state.timeRange.end;

  let debounceHandle = null;
  slider.addEventListener("input", () => {
    state.timeIndex = parseInt(slider.value, 10);
    updateTimeDisplay();
    if (debounceHandle) clearTimeout(debounceHandle);
    debounceHandle = setTimeout(async () => {
      await refreshAtTimestamp();
      await refreshSmartMeterTable();
      await refreshPmuTable();
    }, 120);
  });

  updateTimeDisplay();
}

async function refreshAtTimestamp() {
  if (!state.gridId) return;
  updateTimeDisplay();
  const url = `/api/${state.gridId}/at?timestamp=${encodeURIComponent(state.currentTimestampIso)}`;
  state.atData = await fetchJSON(url);
  // The backend may have snapped to a slightly different (nearest) grid timestamp than what we
  // asked for (only relevant right at load/clamp edges) - use its answer as the noise-key anchor
  // so overlay noise is consistent with what's actually displayed.
  state.currentTimestampIso = state.atData.timestamp;
  renderOverlay();
}

// ---------------------------------------------------------------------------
// Leaflet map setup (Redo 9)
//
// The L.map instance is created exactly ONCE (initMap(), called from main() before any grid is
// loaded), bound to the #svg-wrap div, with an OSM tile layer and an L.svg() vector renderer (so
// bus/line layers render as real SVG <path>/<circle> DOM elements, not Canvas). Four persistent
// L.layerGroup's (line/bus/flow/pmu) are created once here and added to the map once; every grid
// switch or size-slider change just clears + repopulates them (renderTopologyStructure()) rather
// than recreating the map.
// ---------------------------------------------------------------------------

function initMap() {
  const map = L.map("svg-wrap", {
    renderer: L.svg(),
    doubleClickZoom: false, // replaced with a re-fit-to-grid action, see below
    zoomControl: true,
    // Leaflet's animated zoom transition gets interrupted and snaps back to the pre-zoom level
    // in this page's layout (confirmed: an animated setZoom()/the +/- control both silently
    // revert, while a non-animated setZoom({animate:false}) applies immediately and sticks) -
    // disabling the animation is Leaflet's own standard workaround for this class of issue when
    // embedded in a non-trivial CSS layout (grid container, scrollable ancestor panel, etc.).
    zoomAnimation: false,
    fadeAnimation: false,
    markerZoomAnimation: false,
  });
  map.setView([46.8, 8.2], 8); // placeholder view (roughly Switzerland); overwritten by the first fitToGrid()

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  state.layerGroups = {
    line: L.layerGroup().addTo(map),
    bus: L.layerGroup().addTo(map),
    flow: L.layerGroup().addTo(map),
    pmu: L.layerGroup().addTo(map),
    prosumer: L.layerGroup().addTo(map),
  };
  state.busMarkers = {};
  state.lineLayers = {};

  // Old behavior (pre-Leaflet) was "double-click resets to fit view" - closer match to that than
  // Leaflet's own zoom-in-on-dblclick default, and more useful here than repeated zoom-ins given
  // OSM tile detail runs out quickly over these mostly-rural areas anyway.
  map.on("dblclick", () => {
    if (state.fitBounds) map.fitBounds(state.fitBounds, { padding: [24, 24] });
  });

  // Flow-arrow rotation is computed in SCREEN space (map.latLngToContainerPoint), so it must be
  // recomputed whenever the projection changes (zoom or pan), not just when the underlying data
  // changes.
  map.on("zoomend moveend", () => {
    if (state.topology) renderFlowArrows();
  });

  state.leafletMap = map;
}

function fitToGrid() {
  const map = state.leafletMap;
  if (!map || !state.topology) return;
  const latlngs = [];
  for (const b of state.topology.buses) {
    if (b.lat != null && b.lon != null) latlngs.push([b.lat, b.lon]);
  }
  if (!latlngs.length) return;
  const bounds = L.latLngBounds(latlngs);
  state.fitBounds = bounds;
  map.fitBounds(bounds, { padding: [24, 24] });
}

// ---------------------------------------------------------------------------
// Load-flow direction (Redo 4, point 5)
//
// Every grid in this dataset is a pure radial load-only feeder with a single source
// (ext_grid count 1, no sgen/gen anywhere) — so flow direction is fully determined by graph
// topology: always source -> leaves. Multi-source BFS over an UNDIRECTED graph built from BOTH
// lines and trafos (so MV_LV combined grids with a trafo mid-tree still get correct downstream
// distances past the trafo), seeded at the ext_grid bus(es). Pure topology (bus-id graph), no
// coordinates involved - unaffected by the Leaflet rewrite.
// ---------------------------------------------------------------------------

function computeFlowTopology() {
  const adj = new Map(); // busId -> [busId, ...]
  const addEdge = (a, b) => {
    if (!adj.has(a)) adj.set(a, []);
    if (!adj.has(b)) adj.set(b, []);
    adj.get(a).push(b);
    adj.get(b).push(a);
  };
  for (const l of state.topology.lines) addEdge(l.from_bus, l.to_bus);
  for (const t of state.topology.trafos) addEdge(t.hv_bus, t.lv_bus);

  const dist = {};
  const queue = [];
  for (const eg of state.topology.ext_grid) {
    if (dist[eg.bus] === undefined) {
      dist[eg.bus] = 0;
      queue.push(eg.bus);
    }
  }
  let head = 0;
  while (head < queue.length) {
    const u = queue[head++];
    const neighbors = adj.get(u) || [];
    for (const v of neighbors) {
      if (dist[v] === undefined) {
        dist[v] = dist[u] + 1;
        queue.push(v);
      }
    }
  }
  state.busDistance = dist;

  const lineDownstreamBus = {};
  for (const l of state.topology.lines) {
    const df = dist[l.from_bus], dt = dist[l.to_bus];
    if (df == null || dt == null) { lineDownstreamBus[l.line] = l.to_bus; continue; }
    lineDownstreamBus[l.line] = dt >= df ? l.to_bus : l.from_bus;
  }
  state.lineDownstreamBus = lineDownstreamBus;
}

// ---------------------------------------------------------------------------
// Topology rendering (Redo 9: Leaflet layers, real lat/lon, constant on-screen pixel sizes)
//
// busRadius()/lineWeight() are now plain pixel values with NO bounding-box-extent dependency -
// L.circleMarker's radius and L.polyline's weight are always screen pixels in Leaflet (unlike the
// old SVG-viewBox approach, where "1 unit" meant 1 metre of real EPSG:2056 distance and had to be
// scaled by the grid's own geographic extent to stay visible at any zoom level). Bus count is kept
// only as a secondary thinning factor (very large grids get slightly smaller default markers so
// they don't visually merge), matching the intent of the original Redo 1 fix.
// ---------------------------------------------------------------------------

function busRadius(n) {
  if (n > 1500) return 2.5;
  if (n > 400) return 3.2;
  if (n > 100) return 4.0;
  if (n > 30) return 4.8;
  return 5.6;
}

function renderTopologyStructure() {
  const { line: lineGroup, bus: busGroup } = state.layerGroups;
  lineGroup.clearLayers();
  busGroup.clearLayers();

  const busById = {};
  for (const b of state.topology.buses) busById[b.bus] = b;
  state.busById = busById;

  const r = busRadius(state.topology.buses.length) * state.sizeScale;
  const strokeW = Math.max(r * 0.4, 1.2);
  state.baseRadius = r;
  state.strokeW = strokeW;

  const lineLayers = {};
  for (const line of state.topology.lines) {
    const busFrom = busById[line.from_bus], busTo = busById[line.to_bus];
    const latlngs = (line.coords_latlon && line.coords_latlon.length >= 2)
      ? line.coords_latlon
      : [[busFrom?.lat, busFrom?.lon], [busTo?.lat, busTo?.lon]];
    if (latlngs.some(([lat, lon]) => lat == null || lon == null)) continue;

    const poly = L.polyline(latlngs, {
      color: "#93a0b3",
      weight: strokeW,
      lineCap: "round",
      // Synthetic straight-line paths (no real MV cable routing, Step 10) render dashed so they
      // read as "schematic", never confused with real geometry (Redo 4, point 4). Set once here,
      // never touched by updateBusesAndLines() (color is independent of dash pattern).
      dashArray: line.geo_source === "synthetic_straight" ? `${strokeW * 3},${strokeW * 2}` : null,
    });
    poly.on("mouseover", (e) => showLineTooltip(e.originalEvent, line));
    poly.on("mousemove", (e) => moveTooltip(e.originalEvent));
    poly.on("mouseout", hideTooltip);
    lineGroup.addLayer(poly);
    lineLayers[line.line] = poly;
  }
  state.lineLayers = lineLayers;

  const busMarkers = {};
  for (const b of state.topology.buses) {
    if (b.lat == null || b.lon == null) continue;
    const marker = L.circleMarker([b.lat, b.lon], {
      radius: r,
      color: "#fff",
      weight: strokeW * 0.5,
      fillColor: "#e6e9ee",
      fillOpacity: 1,
    });
    marker.on("mouseover", (e) => showBusTooltip(e.originalEvent, b));
    marker.on("mousemove", (e) => moveTooltip(e.originalEvent));
    marker.on("mouseout", hideTooltip);
    busGroup.addLayer(marker);
    busMarkers[b.bus] = marker;
  }
  state.busMarkers = busMarkers;
}

// ---------------------------------------------------------------------------
// Overlay: voltage / line loading + PMU penetration markers
// ---------------------------------------------------------------------------

const ROLE_COLOR = { ext_grid: "var(--ext-grid)", trafo: "var(--trafo)", regular: "var(--regular)" };

// All five layers are independent show/hide toggles that combine on the same Leaflet layers
// (Redo 4, points 1-2):
//   - bus fill        <- voltage layer (else neutral grey)
//   - bus ring+radius <- nodetype layer (role-colored ring + radius bump; else thin white ring,
//                           uniform radius) - this lets voltage fill and role ring coexist.
//   - line stroke color <- loading layer (else neutral grey); dash pattern (real vs.
//                           synthetic-straight) is set once at creation time and never touched here.
//   - flow arrows / PMU markers <- their own independent layer groups, gated on/off wholesale.
function renderOverlay() {
  if (!state.atData) return;
  updateBusesAndLines();
  renderFlowArrows();
  renderPmuMarkers();
  renderProsumerMarkers();
  renderColorbar();
}

// Recolors bus fill / bus role-ring / line stroke only, without touching flow arrows, PMU
// markers, or rebuilding the colorbar widgets - split out so a colorbar-handle drag (which needs
// to recolor live on every pointermove) can call just this fast path and keep the drag's own
// handle DOM elements alive throughout the drag (see buildColorbarWidget() below).
function updateBusesAndLines() {
  if (!state.busMarkers || !state.atData) return;

  for (const busId of Object.keys(state.busMarkers)) {
    const marker = state.busMarkers[busId];
    const b = state.busById[busId];

    let fill = "#e6e9ee";
    if (state.layers.voltage) {
      const raw = state.atData.bus[busId];
      if (raw != null) fill = voltageColor(noisyVoltage(raw, busId));
    }

    const style = { fillColor: fill, fillOpacity: 1 };
    if (state.layers.nodetype) {
      style.color = ROLE_COLOR[b.role] || ROLE_COLOR.regular;
      style.weight = state.strokeW * 2.5;
      style.radius = b.role === "regular" ? state.baseRadius : state.baseRadius * 1.5;
    } else {
      style.color = "#fff";
      style.weight = state.strokeW * 0.5;
      style.radius = state.baseRadius;
    }
    marker.setStyle(style);
  }

  for (const lineId of Object.keys(state.lineLayers)) {
    const poly = state.lineLayers[lineId];
    let stroke = "#93a0b3";
    if (state.layers.loading) {
      const raw = state.atData.line[lineId];
      if (raw != null) stroke = loadingColor(noisyLoading(raw, lineId));
    }
    poly.setStyle({ color: stroke });
  }
}

// ---------------------------------------------------------------------------
// Load-flow arrows (Redo 4, point 5; Redo 9: divIcon markers with screen-space rotation)
//
// Direction from computeFlowTopology()'s BFS, magnitude from the same loading_percent already
// driving the line-loading color layer. Must stay a CONSTANT ON-SCREEN PIXEL size regardless of
// zoom (like circleMarker radius already is) - implemented as L.marker + L.divIcon (fixed
// iconSize in pixels), NOT L.polygon (which is geographically-sized and would shrink/grow with
// zoom - wrong for a fixed-size glyph). Rotation is computed from the SCREEN-SPACE vector between
// two nearby points on the line (map.latLngToContainerPoint), not a geographic bearing formula,
// so the arrow always visually points the right way regardless of projection/zoom - this is why
// renderFlowArrows() is also re-run on the map's zoomend/moveend events (see initMap()).
// ---------------------------------------------------------------------------

const FLOW_ARROW_COLOR = "#2b3542";

// Find the geometric midpoint of a line's [lat,lon] point list and a direction vector for its
// bearing, robust to duplicate/zero-length points near the middle (seen in real Swiss-PDGs
// geometry). Coordinate-system agnostic (works the same for [lat,lon] as it did for [x,y]).
function lineMidpointAndDirection(pts) {
  if (pts.length === 2) {
    const mid = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2];
    return { mid, a: pts[0], b: pts[1] };
  }
  const midIdx = Math.floor((pts.length - 1) / 2);
  const mid = pts[midIdx];
  for (let offset = 1; offset < pts.length; offset++) {
    const a = pts[Math.max(0, midIdx - offset)];
    const b = pts[Math.min(pts.length - 1, midIdx + offset)];
    if (a[0] !== b[0] || a[1] !== b[1]) return { mid, a, b };
  }
  return { mid, a: pts[0], b: pts[pts.length - 1] };
}

function renderFlowArrows() {
  const map = state.leafletMap;
  const flowGroup = state.layerGroups && state.layerGroups.flow;
  if (!flowGroup || !map) return;
  flowGroup.clearLayers();
  if (!state.layers.flow || !state.atData || !state.lineDownstreamBus) return;

  const r = state.baseRadius;
  const minLen = r * 1.5, maxLen = r * 4.5;

  for (const line of state.topology.lines) {
    const busFrom = state.busById[line.from_bus], busTo = state.busById[line.to_bus];
    const pts = (line.coords_latlon && line.coords_latlon.length >= 2)
      ? line.coords_latlon
      : [[busFrom?.lat, busFrom?.lon], [busTo?.lat, busTo?.lon]];
    if (pts.length < 2 || pts.some(([lat, lon]) => lat == null || lon == null)) continue;

    const { mid, a, b } = lineMidpointAndDirection(pts);
    // Rotation angle is computed in SCREEN space (container pixels), not from raw lat/lon deltas -
    // this is what keeps the arrow's visual direction correct under any zoom/projection.
    const aPx = map.latLngToContainerPoint(a);
    const bPx = map.latLngToContainerPoint(b);
    let dx = bPx.x - aPx.x, dy = bPx.y - aPx.y;
    if (dx === 0 && dy === 0) continue; // fully degenerate geometry (or a/b coincide on screen), skip

    // `a`/`b` walk outward from the array's middle in increasing-index order (a earlier, b
    // later), i.e. the same order the coords array runs in (from_bus -> to_bus). Flip the arrow
    // if the downstream (farther-from-root) bus is actually at the from_bus end.
    const downstreamBus = state.lineDownstreamBus[line.line];
    if (downstreamBus !== line.to_bus) { dx = -dx; dy = -dy; }
    const angleDeg = Math.atan2(dy, dx) * 180 / Math.PI;

    const raw = state.atData.line[line.line];
    const pct = raw != null ? Math.max(0, Math.min(100, raw)) : 0;
    const len = minLen + (maxLen - minLen) * (pct / 100);
    const halfW = len * 0.32;
    const box = Math.ceil(len + 4); // square icon bounding box, a little slack around the triangle

    const svgHtml = `<svg width="${box}" height="${box}" viewBox="${-box / 2} ${-box / 2} ${box} ${box}" ` +
      `style="display:block; transform: rotate(${angleDeg}deg);">` +
      `<polygon points="${len / 2},0 ${-len / 2},${halfW} ${-len / 2},${-halfW}" fill="${FLOW_ARROW_COLOR}"></polygon>` +
      `</svg>`;
    const icon = L.divIcon({ html: svgHtml, className: "grid-divicon", iconSize: [box, box], iconAnchor: [box / 2, box / 2] });
    const marker = L.marker(mid, { icon, interactive: true, keyboard: false });
    marker.on("mouseover", (e) => showLineTooltip(e.originalEvent, line));
    marker.on("mousemove", (e) => moveTooltip(e.originalEvent));
    marker.on("mouseout", hideTooltip);
    flowGroup.addLayer(marker);
  }
}

function renderPmuMarkers() {
  const pmuGroup = state.layerGroups && state.layerGroups.pmu;
  if (!pmuGroup) return;
  pmuGroup.clearLayers();
  if (!state.layers.pmu) return;
  const levelInfo = state.pmuPenetration[state.pmuLevel];
  if (!levelInfo || levelInfo.buses.length === 0) return;

  const size = Math.max(state.baseRadius * 2.2, 4);
  for (const busId of levelInfo.buses) {
    const bus = state.busById[busId];
    if (!bus || bus.lat == null) continue;
    const isFeederRoot = busId === levelInfo.feeder_root_bus;

    const svgHtml = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="display:block;">` +
      `<rect x="${size * 0.15}" y="${size * 0.15}" width="${size * 0.7}" height="${size * 0.7}" ` +
      `transform="rotate(45 ${size / 2} ${size / 2})" fill="var(--pmu)" stroke="#fff" ` +
      `stroke-width="${Math.max(size * 0.06, 0.3)}"></rect></svg>`;
    const icon = L.divIcon({ html: svgHtml, className: "grid-divicon", iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    const marker = L.marker([bus.lat, bus.lon], { icon, interactive: true, keyboard: false });
    marker.on("mouseover", (e) => showTooltip(e.originalEvent,
      `PMU-equipped bus ${busId}${isFeederRoot ? " (feeder root)" : ""}\n` +
      `penetration level: ${state.pmuLevel}`));
    marker.on("mousemove", (e) => moveTooltip(e.originalEvent));
    marker.on("mouseout", hideTooltip);
    pmuGroup.addLayer(marker);
  }
}

// ---------------------------------------------------------------------------
// Prosumer / PV markers (Redo 10)
//
// Purely a presentation-layer addition over data that's already there — no new backend
// endpoint, no timestamp dependency (is_prosumer/frac_export/peak_export_mw are whole-4-week
// summaries computed by app.py from the bundle's eth_load_profiles, joined onto each load record
// in topology.json). Modeled directly on renderPmuMarkers() just above: fixed-pixel
// L.divIcon markers (not geographically-sized), gated on state.layers.prosumer, cleared/rebuilt
// on every renderOverlay() call. NOT wired to noise or the time slider — it should look
// identical regardless of which timestamp/noise level is selected.
// ---------------------------------------------------------------------------

// 5-point star polygon, centered at (12,12) in a 24x24 viewBox — visually distinct from both the
// PMU diamond (rotated square) and plain bus circles.
const STAR_POINTS = "12,2 14.7,9.2 22.4,9.5 16.3,14.3 18.5,21.6 12,17.3 5.5,21.6 7.7,14.3 1.6,9.5 9.3,9.2";

function renderProsumerMarkers() {
  const group = state.layerGroups && state.layerGroups.prosumer;
  if (!group) return;
  group.clearLayers();
  if (!state.layers.prosumer || !state.topology) return;

  const minSize = Math.max(state.baseRadius * 1.8, 5);
  const maxSize = Math.max(state.baseRadius * 3.4, 9);
  // frac_export ranges ~0-0.65 across the dataset's real prosumer loads (checked directly) —
  // 0.5 as the interpolation ceiling gives good visual spread without over-compressing the
  // (much more common) low end.
  const FRAC_CAP = 0.5;

  for (const load of state.topology.loads) {
    if (!load.is_prosumer) continue;
    const bus = state.busById && state.busById[load.bus];
    if (!bus || bus.lat == null || bus.lon == null) continue;

    const t = Math.max(0, Math.min(1, (load.frac_export || 0) / FRAC_CAP));
    const size = minSize + (maxSize - minSize) * t;

    const svgHtml = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" style="display:block;">` +
      `<polygon points="${STAR_POINTS}" fill="var(--pv)" stroke="#fff" stroke-width="1"></polygon></svg>`;
    const icon = L.divIcon({ html: svgHtml, className: "grid-divicon", iconSize: [size, size], iconAnchor: [size / 2, size / 2] });
    const marker = L.marker([bus.lat, bus.lon], { icon, interactive: true, keyboard: false });
    const pctTime = (load.frac_export * 100).toFixed(1);
    const peakKw = (load.peak_export_mw * 1000).toFixed(1);
    marker.on("mouseover", (e) => showTooltip(e.originalEvent,
      `PV / prosumer load ${load.load} (bus ${load.bus})\n` +
      `exports ${pctTime}% of the time\n` +
      `peak export: ${peakKw} kW`));
    marker.on("mousemove", (e) => moveTooltip(e.originalEvent));
    marker.on("mouseout", hideTooltip);
    group.addLayer(marker);
  }
}

function renderColorbar() {
  const box = el("colorbar");
  box.innerHTML = "";
  if (state.layers.voltage) {
    box.appendChild(buildColorbarWidget({
      title: "Voltage (vm_pu)",
      domain: VOLTAGE_DOMAIN,
      range: state.voltageRange,
      defaultRange: { min: 0.95, max: 1.05 },
      minGap: MIN_RANGE_GAP.voltage,
      formatHandle: (v) => v.toFixed(3),
      formatDomain: (v) => v.toFixed(2),
      onRangeChange: (r) => { state.voltageRange = r; updateBusesAndLines(); },
    }));
  }
  if (state.layers.loading) {
    box.appendChild(buildColorbarWidget({
      title: "Line loading (%)",
      domain: LOADING_DOMAIN,
      range: state.loadingRange,
      defaultRange: { min: 0, max: 100 },
      minGap: MIN_RANGE_GAP.loading,
      formatHandle: (v) => `${Math.round(v)}%`,
      formatDomain: (v) => `${Math.round(v)}%`,
      onRangeChange: (r) => { state.loadingRange = r; updateBusesAndLines(); },
    }));
  }
}

// ---------------------------------------------------------------------------
// Draggable colorbar widget (Redo 4, point 3)
//
// One widget per currently-on color layer (voltage and/or loading). Each has a fixed drag
// DOMAIN (wider than the default highlighted window), a 3-stop blue/green/red gradient drawn
// only between the current min/max handles (flat grey outside them), and two draggable handles
// with live numeric labels. `opts.range` is mutated in place (same object as state.*Range) so the
// widget and colorScale3() always agree on the current window without an extra sync step.
// Entirely independent of the map's coordinate system - unchanged by the Leaflet rewrite.
// ---------------------------------------------------------------------------

// Fixed drag domains for the colorbar handles (Redo 4) — wider than the default window so the
// user can both widen and narrow the highlighted range. Redo 6: capped at exactly the default
// window (narrowing only).
const VOLTAGE_DOMAIN = { min: 0.95, max: 1.05 };
const LOADING_DOMAIN = { min: 0, max: 100 };
const MIN_RANGE_GAP = { voltage: 0.005, loading: 1 }; // minimum min<max separation while dragging

function buildColorbarWidget(opts) {
  const { domain, range, minGap, formatHandle, formatDomain, onRangeChange } = opts;

  const widget = document.createElement("div");
  widget.className = "colorbar-widget";

  const header = document.createElement("div");
  header.className = "cb-header";
  const title = document.createElement("span");
  title.className = "cb-title";
  title.textContent = opts.title;
  const reset = document.createElement("button");
  reset.type = "button";
  reset.className = "cb-reset";
  reset.textContent = "reset";
  reset.addEventListener("click", () => {
    range.min = opts.defaultRange.min;
    range.max = opts.defaultRange.max;
    onRangeChange(range);
    renderColorbar(); // full rebuild is fine here - not mid-drag
  });
  header.appendChild(title);
  header.appendChild(reset);

  const track = document.createElement("div");
  track.className = "cb-track";

  const handleMin = document.createElement("div");
  handleMin.className = "cb-handle cb-handle-min";
  const labelMin = document.createElement("span");
  labelMin.className = "cb-handle-label";
  handleMin.appendChild(labelMin);

  const handleMax = document.createElement("div");
  handleMax.className = "cb-handle cb-handle-max";
  const labelMax = document.createElement("span");
  labelMax.className = "cb-handle-label";
  handleMax.appendChild(labelMax);

  track.appendChild(handleMin);
  track.appendChild(handleMax);

  const domainLabels = document.createElement("div");
  domainLabels.className = "cb-domain-labels";
  const dMin = document.createElement("span");
  dMin.textContent = formatDomain(domain.min);
  const dMax = document.createElement("span");
  dMax.textContent = formatDomain(domain.max);
  domainLabels.appendChild(dMin);
  domainLabels.appendChild(dMax);

  widget.appendChild(header);
  widget.appendChild(track);
  widget.appendChild(domainLabels);

  function frac(v) {
    return (v - domain.min) / (domain.max - domain.min);
  }

  function updatePositions() {
    const fMin = Math.max(0, Math.min(1, frac(range.min)));
    const fMax = Math.max(0, Math.min(1, frac(range.max)));
    const fMid = (fMin + fMax) / 2;
    track.style.background =
      `linear-gradient(to right,` +
      ` #c7ccd4 0%, #c7ccd4 ${fMin * 100}%,` +
      ` ${rgbToCss(BLUE)} ${fMin * 100}%, ${rgbToCss(GREEN)} ${fMid * 100}%, ${rgbToCss(RED)} ${fMax * 100}%,` +
      ` #c7ccd4 ${fMax * 100}%, #c7ccd4 100%)`;
    handleMin.style.left = `${fMin * 100}%`;
    handleMax.style.left = `${fMax * 100}%`;
    labelMin.textContent = formatHandle(range.min);
    labelMax.textContent = formatHandle(range.max);
  }

  function dragHandle(handleEl, which) {
    handleEl.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      handleEl.setPointerCapture(e.pointerId);
      widget.classList.add("dragging");

      const onMove = (ev) => {
        const rect = track.getBoundingClientRect();
        let f = rect.width > 0 ? (ev.clientX - rect.left) / rect.width : 0;
        f = Math.max(0, Math.min(1, f));
        let v = domain.min + f * (domain.max - domain.min);
        if (which === "min") {
          v = Math.max(domain.min, Math.min(v, range.max - minGap));
          range.min = v;
        } else {
          v = Math.min(domain.max, Math.max(v, range.min + minGap));
          range.max = v;
        }
        updatePositions();
        onRangeChange(range); // fast recolor only - does NOT rebuild this widget's DOM
      };
      const onUp = (ev) => {
        try { handleEl.releasePointerCapture(e.pointerId); } catch (err) {}
        widget.classList.remove("dragging");
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    });
  }

  dragHandle(handleMin, "min");
  dragHandle(handleMax, "max");
  updatePositions();

  return widget;
}

// ---------------------------------------------------------------------------
// Map resize handle (Redo 6) - lets the user drag #svg-wrap taller/shorter.
// Redo 9: since #svg-wrap is now the Leaflet map's own container, every height change here MUST
// be followed by map.invalidateSize() - Leaflet caches its container's pixel dimensions
// internally and does not auto-detect external CSS/JS resizes, so skipping this would leave the
// map rendering into a stale/wrong-sized viewport (tiles cut off, mispositioned markers).
// ---------------------------------------------------------------------------

function setupMapResize() {
  const wrap = el("svg-wrap");
  const handle = el("map-resize-handle");
  const MIN_H = 250;
  const maxH = () => window.innerHeight * 0.85;

  // Restore a previously-chosen height (persists across page reloads, not just within a session).
  try {
    const saved = parseFloat(localStorage.getItem("mapHeightPx"));
    if (!isNaN(saved)) {
      wrap.style.height = Math.min(Math.max(saved, MIN_H), maxH()) + "px";
      if (state.leafletMap) state.leafletMap.invalidateSize();
    }
  } catch (err) {}

  let dragging = false, startY = 0, startH = 0;
  handle.addEventListener("mousedown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = wrap.getBoundingClientRect().height;
    document.body.style.cursor = "ns-resize";
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const newH = Math.min(Math.max(startH + (e.clientY - startY), MIN_H), maxH());
    wrap.style.height = newH + "px";
    if (state.leafletMap) state.leafletMap.invalidateSize();
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    try { localStorage.setItem("mapHeightPx", parseFloat(wrap.style.height)); } catch (err) {}
  });
}

// ---------------------------------------------------------------------------
// Column-width resize handles (Redo 8) - generalizes setupMapResize()'s approach to the
// page's 3 columns. Only columns 1 and 3 are ever set directly (fixed px, via #layout's
// inline grid-template-columns); column 2 stays `1fr` and auto-absorbs whatever space is
// left, which is what makes it the "large" column that grows/shrinks as the others are
// dragged, with no need to redistribute space by hand.
// Redo 9: column 2 contains the Leaflet map, so applyColumns() (called on drag, on the initial
// clamp-on-load, and on window resize) must call map.invalidateSize() every time it changes the
// grid-template-columns, for the same reason as setupMapResize() above.
// ---------------------------------------------------------------------------

function setupColumnResize() {
  const layout = el("layout");
  const MIN_COL = 260;    // floor for column 1 / column 3 width
  const MIN_MIDDLE = 300; // column 2 (the map/time column) never gets squeezed below this
  const GAP = 12;         // must match #layout's CSS `gap`
  const HANDLE_W = 6;     // must match .col-resize-handle CSS width

  let leftPx = 340, rightPx = 460; // defaults, matching style.css's starting values

  try {
    const sl = parseFloat(localStorage.getItem("colLeftPx"));
    if (!isNaN(sl)) leftPx = sl;
    const sr = parseFloat(localStorage.getItem("colRightPx"));
    if (!isNaN(sr)) rightPx = sr;
  } catch (err) {}

  function applyColumns() {
    layout.style.gridTemplateColumns = `${leftPx}px ${HANDLE_W}px 1fr ${HANDLE_W}px ${rightPx}px`;
    if (state.leafletMap) state.leafletMap.invalidateSize();
  }

  function contentWidth() {
    const rect = layout.getBoundingClientRect();
    const cs = getComputedStyle(layout);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;
    return rect.width - padL - padR;
  }

  // Budget available to the 3 panel columns alone (total content width minus the 4 row
  // gaps and 2 handle-column widths).
  function panelsBudget() {
    return contentWidth() - 4 * GAP - 2 * HANDLE_W;
  }
  function maxLeft() {
    return Math.max(MIN_COL, panelsBudget() - rightPx - MIN_MIDDLE);
  }
  function maxRight() {
    return Math.max(MIN_COL, panelsBudget() - leftPx - MIN_MIDDLE);
  }

  // Clamp the restored/default values against the actual viewport before first paint.
  leftPx = Math.min(Math.max(leftPx, MIN_COL), maxLeft());
  rightPx = Math.min(Math.max(rightPx, MIN_COL), maxRight());
  applyColumns();

  function persist() {
    try {
      localStorage.setItem("colLeftPx", String(leftPx));
      localStorage.setItem("colRightPx", String(rightPx));
    } catch (err) {}
  }

  function bindHandle(handleEl, which) {
    let dragging = false, startX = 0, startLeft = 0, startRight = 0;
    handleEl.addEventListener("mousedown", (e) => {
      dragging = true;
      startX = e.clientX;
      startLeft = leftPx;
      startRight = rightPx;
      document.body.style.cursor = "col-resize";
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      if (which === "left") {
        // col-resize-1: dragging right grows column 1 (moves the col1/col2 boundary right).
        leftPx = Math.min(Math.max(startLeft + dx, MIN_COL), maxLeft());
      } else {
        // col-resize-2: dragging right moves the col2/col3 boundary right, which SHRINKS
        // column 3 - sign flipped relative to the left handle.
        rightPx = Math.min(Math.max(startRight - dx, MIN_COL), maxRight());
      }
      applyColumns();
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      document.body.style.cursor = "";
      persist();
    });
  }

  bindHandle(el("col-resize-1"), "left");
  bindHandle(el("col-resize-2"), "right");

  window.addEventListener("resize", () => {
    leftPx = Math.min(leftPx, maxLeft());
    rightPx = Math.min(rightPx, maxRight());
    applyColumns();
  });
}

// ---------------------------------------------------------------------------
// Tooltip
//
// Unchanged (Redo 9): still the existing custom #tooltip div and showTooltip()/moveTooltip()/
// hideTooltip() functions - each Leaflet layer's mouseover/mousemove/mouseout events are wired to
// call these same functions, passing through e.originalEvent (the native DOM event, which carries
// clientX/clientY, exactly what moveTooltip() already reads), so this is a low-risk swap rather
// than a rewrite of tooltip behavior.
// ---------------------------------------------------------------------------

function showTooltip(evt, text) {
  const tip = el("tooltip");
  tip.textContent = text;
  tip.style.display = "block";
  moveTooltip(evt);
}
function moveTooltip(evt) {
  const wrap = el("svg-wrap").getBoundingClientRect();
  const tip = el("tooltip");
  tip.style.left = (evt.clientX - wrap.left + 14) + "px";
  tip.style.top = (evt.clientY - wrap.top + 10) + "px";
}
function hideTooltip() {
  el("tooltip").style.display = "none";
}

function showBusTooltip(evt, b) {
  const raw = state.atData ? state.atData.bus[String(b.bus)] : null;
  const noisy = raw != null ? noisyVoltage(raw, b.bus) : null;
  let text = `Bus ${b.bus} (${b.name || ""})\nvn_kv: ${b.vn_kv}\nrole: ${b.role}\ngeodata: ${b.geo_source}`;
  if (raw != null) {
    text += `\nvm_pu (clean): ${raw.toFixed(5)}`;
    text += `\nvm_pu (displayed, noise=${state.noiseLevel}): ${noisy.toFixed(5)}`;
  }
  showTooltip(evt, text);
}

function showLineTooltip(evt, line) {
  const raw = state.atData ? state.atData.line[String(line.line)] : null;
  const noisy = raw != null ? noisyLoading(raw, line.line) : null;
  let text = `Line ${line.line} (${line.name || ""})\n${line.from_bus} → ${line.to_bus}\n` +
    `type: ${line.std_type || "n/a"}\nlength: ${(line.length_km * 1000).toFixed(1)} m\n` +
    `max I: ${line.max_i_ka} kA\ngeodata: ${line.geo_source}`;
  if (raw != null) {
    text += `\nloading% (clean): ${raw.toFixed(3)}`;
    text += `\nloading% (displayed, noise=${state.noiseLevel}): ${noisy.toFixed(3)}`;
  }
  showTooltip(evt, text);
}

// ---------------------------------------------------------------------------
// Smart-meter table (optional secondary view)
// ---------------------------------------------------------------------------

async function refreshSmartMeterTable() {
  if (!state.gridId || !state.currentTimestampIso) return;
  const note = el("smart-meter-note");
  note.textContent = "Loading…";
  try {
    const url = `/api/${state.gridId}/smart_meter?timestamp=${encodeURIComponent(state.currentTimestampIso)}`;
    const resp = await fetchJSON(url);
    renderSmartMeterTable(resp);
  } catch (err) {
    note.textContent = `Could not load smart-meter table: ${err.message}`;
  }
}

function renderSmartMeterTable(resp) {
  const body = document.querySelector("#smart-meter-table tbody");
  body.innerHTML = "";
  const rows = [...resp.loads].sort((a, b) => a.load - b.load);
  for (const r of rows) {
    const [np, nq] = noisySmartMeterPQ(r.p_mw, r.q_mvar, r.load);
    const nv = noisySmartMeterVm(r.vm_pu, r.load);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.load}</td><td>${r.bus}</td>
      <td>${nv != null ? nv.toFixed(4) : "n/a"}</td>
      <td>${np.toExponential(3)}</td>
      <td>${nq.toExponential(3)}</td>`;
    body.appendChild(tr);
  }
  el("smart-meter-note").textContent =
    `${rows.length} loads at ${resp.timestamp} · noise level: ${state.noiseLevel} · magnitude only, no phase.`;
}

// ---------------------------------------------------------------------------
// PMU readings table (parallel to the smart-meter table above)
// ---------------------------------------------------------------------------

async function refreshPmuTable() {
  if (!state.gridId || !state.currentTimestampIso) return;
  const note = el("pmu-table-note");
  note.textContent = "Loading…";
  try {
    const url = `/api/${state.gridId}/pmu_readings?timestamp=${encodeURIComponent(state.currentTimestampIso)}&penetration=${encodeURIComponent(state.pmuLevel)}`;
    const resp = await fetchJSON(url);
    renderPmuTable(resp);
  } catch (err) {
    note.textContent = `Could not load PMU table: ${err.message}`;
  }
}

function renderPmuTable(resp) {
  const body = document.querySelector("#pmu-table tbody");
  body.innerHTML = "";
  const note = el("pmu-table-note");

  if (!resp.pmus || resp.pmus.length === 0) {
    note.textContent = state.pmuLevel === "0%"
      ? "No PMUs at 0% penetration."
      : `No PMUs found at penetration level ${state.pmuLevel}.`;
    return;
  }

  const rows = [...resp.pmus].sort((a, b) => a.bus - b.bus);
  for (const r of rows) {
    const nvm = noisyPmuVm(r.vm_pu, r.bus);
    const nva = noisyPmuVa(r.va_degree, r.bus);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.bus}</td>
      <td>${nvm != null ? nvm.toFixed(5) : "n/a"}</td>
      <td>${nva != null ? nva.toFixed(4) : "n/a"}</td>
      <td>${r.is_feeder_root ? "feeder-root" : "added"}</td>`;
    body.appendChild(tr);
  }
  note.textContent =
    `${rows.length} PMU(s) at ${resp.timestamp} · penetration: ${resp.penetration_level} · ` +
    `noise level: ${state.noiseLevel} · magnitude + phase.`;
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

async function main() {
  // Create the Leaflet map once, before anything else touches #svg-wrap (grid selection, the
  // resize handles' restore-on-load paths, etc. all assume state.leafletMap already exists).
  initMap();

  const [, noiseConfig, timeRange] = await Promise.all([
    loadIndex(),
    fetchJSON(`/data/noise_levels.json`),
    fetchJSON(`/data/time_range.json`),
  ]);
  state.noiseConfig = noiseConfig;
  state.timeRange = timeRange;
  setupTimeControl();
  setupMapResize();
  setupColumnResize();

  const layerCheckboxIds = {
    voltage: "layer-voltage",
    loading: "layer-loading",
    flow: "layer-flow",
    nodetype: "layer-nodetype",
    pmu: "layer-pmu",
    prosumer: "layer-prosumer",
  };
  for (const [layerKey, checkboxId] of Object.entries(layerCheckboxIds)) {
    el(checkboxId).addEventListener("change", (e) => {
      state.layers[layerKey] = e.target.checked;
      renderOverlay();
    });
  }
  el("noise-select").addEventListener("change", (e) => {
    state.noiseLevel = e.target.value;
    renderOverlay();
    refreshSmartMeterTable();
    refreshPmuTable();
  });
  el("pmu-select").addEventListener("change", (e) => {
    state.pmuLevel = e.target.value;
    renderPmuMarkers();
    refreshPmuTable();
  });

  // Marker/line size slider (Redo 5) - debounced like the time slider so dragging doesn't force a
  // full layer rebuild on every pixel of movement. renderTopologyStructure() rebuilds all Leaflet
  // layers at the new radius/stroke-width; renderOverlay() must follow to reapply colors/
  // flow-arrows/PMU-markers/colorbar on top of the freshly rebuilt layers. fitToGrid() is
  // deliberately NOT called here - it would reset the user's current pan/zoom, which this control
  // has no reason to touch.
  const sizeSlider = el("size-slider");
  const sizeSliderLabel = el("size-slider-label");
  let sizeDebounceHandle = null;
  sizeSlider.addEventListener("input", () => {
    sizeSliderLabel.textContent = `${parseFloat(sizeSlider.value).toFixed(1)}×`;
    if (sizeDebounceHandle) clearTimeout(sizeDebounceHandle);
    sizeDebounceHandle = setTimeout(() => {
      state.sizeScale = parseFloat(sizeSlider.value);
      renderTopologyStructure();
      renderOverlay();
    }, 120);
  });

  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.addEventListener("click", () => {
      const tab = btn.getAttribute("data-tab");
      for (const b of document.querySelectorAll(".tab-btn")) b.classList.toggle("active", b === btn);
      for (const p of document.querySelectorAll(".tab-panel")) p.classList.toggle("active", p.id === `tab-${tab}`);
    });
  }

  if (state.index.length) await selectGrid(state.index[0].grid_id);
}

main();
