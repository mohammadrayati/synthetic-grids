"""
Data-viewer webpage backend. Small local Flask app. Serves:
  - the static HTML/CSS/JS page
  - timestamp-independent JSON (index, per-grid topology/meta/pmu_penetration, noise config,
    shared time range) computed LIVE from data/grid_topology/*.csv and
    data/power_flow_results/*.parquet on first request, then cached in-process
    (functools.lru_cache) - no separate build step, no prebuilt files on disk.
  - a LIVE API for timestamp-dependent ground truth, reading straight from
    data/power_flow_results/<grid_id>/{bus,line}.parquet - too large (1.1 GB across 35 grids) to
    ship to the browser wholesale or precompute per timestamp (2,688 timestamps x 35 grids), so
    it's served on demand, with the parsed-and-indexed DataFrame for each grid cached after its
    first request.

Endpoints:
  GET /                                            -> redirects to /static/index.html
  GET /static/<path>                               -> the page itself (html/js/css)
  GET /data/index.json                             -> the 35-grid list
  GET /data/noise_levels.json                      -> data/noise-model-config.csv in full
  GET /data/time_range.json                        -> shared 4-week timestamp grid (start/end/step/count)
  GET /data/<grid_id>/meta.json                    -> per-grid summary (bus/line/trafo/load counts)
  GET /data/<grid_id>/topology.json                -> buses/lines/trafos/ext_grid/loads, geodata joined in
  GET /data/<grid_id>/pmu_penetration.json         -> per-level list of PMU-equipped buses
  GET /api/<grid_id>/at?timestamp=<iso8601>        -> {timestamp, bus: {bus_id: vm_pu, ...},
                                                        line: {line_id: loading_percent, ...}}
  GET /api/<grid_id>/smart_meter?timestamp=<iso8601> -> [{load, bus, p_mw, q_mvar, vm_pu}, ...]
      /at takes an optional &stat=<mean|max|min|std|median|q10|q90>: values then come from
      data/power_flow_results_10s_stats/ (10-s power flow, statistics per 15-min window; only some
      grids have it, see meta.json's has_10s_stats). With stat=std, /at also returns
      std_scale = {bus, line}: the grid's 99th percentile of that std, for the color scale.
      /smart_meter and /pmu_readings take an optional &data=10s: every quantity is then a
      {stat: value} dict over all seven statistics.
  GET /api/<grid_id>/download?data=<15min|10s>&noise=<level>&penetration=<level>&start=<date>&end=<date>
      -> zip of smart_meter.csv + pmu.csv for every timestamp in [start, end] (dates inclusive),
         noise added server-side (seeded, same noise config as the page).
  GET /api/<grid_id>/pmu_readings?timestamp=<iso8601>&penetration=<level>
                                                      -> {pmus: [{bus, vm_pu, va_degree, p_mw,
                                                        q_mvar, is_feeder_root}, ...]} for every
                                                        bus selected at that PMU-penetration level;
                                                        empty list at 0%. p_mw/q_mvar are the net
                                                        injection of the bus's own attached loads
                                                        (0 for a bus with none) - see
                                                        net_injection_at_bus()'s docstring for why
                                                        this is used instead of branch power flow.

<grid_id> is index.csv's `folder` column with the `data/grid_topology/` prefix stripped and `/`
replaced by `__`, e.g. 'data/grid_topology/LV/Alps-Periurban/5238-11_1_3_grid' <->
'LV__Alps-Periurban__5238-11_1_3_grid' <-> data/power_flow_results/LV__Alps-Periurban__5238-11_1_3_grid/.

This is a local-only tool (no auth) - not hardened for untrusted input beyond basic
clamping/error handling so a bad request doesn't crash the process.
"""
import functools
import json
import os
import tempfile
import threading
import zipfile
import zlib
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pyproj
from flask import Flask, abort, jsonify, request, redirect, send_file, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORGANIZED_DIR = PROJECT_ROOT / "data" / "grid_topology"
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "power_flow_results"
LOAD_PROFILES_DIR = PROJECT_ROOT / "data" / "load_power"
HIGHRES_DIR = PROJECT_ROOT / "data" / "power_flow_results_10s_stats"
HIGHRES_STATS = ("mean", "max", "min", "std", "median", "q10", "q90")
WEBPAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEBPAGE_DIR / "static"

GRID_CACHE_SIZE = 64  # >= 35 grids (one cache entry per grid_id), generous headroom

app = Flask(__name__)

# EPSG:2056 (Swiss LV95, meters, easting/northing) -> EPSG:4326 (WGS84, lat/lon degrees), for the
# Leaflet/OSM basemap. always_xy=True makes .transform(x, y) take (easting, northing) and return
# (lon, lat) in that (x, y)-semantics order - NOT (lat, lon). Leaflet wants [lat, lon], so callers
# below swap the transformer's own (lon, lat) output.
_TO_WGS84 = pyproj.Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


def to_latlon(x, y):
    lon, lat = _TO_WGS84.transform(x, y)
    return lat, lon


# ---------------------------------------------------------------------------
# grid_id <-> folder path helpers
# ---------------------------------------------------------------------------

def grid_id_for_folder(folder_rel: str) -> str:
    """'data/grid_topology/LV/Alps-Periurban/5238-11_1_3_grid' -> 'LV__Alps-Periurban__5238-11_1_3_grid'"""
    rel = folder_rel.split("data/grid_topology/", 1)[-1]
    return rel.replace("/", "__")


def folder_rel_for_grid_id(grid_id: str) -> str:
    """'LV__Alps-Periurban__5238-11_1_3_grid' -> 'data/grid_topology/LV/Alps-Periurban/5238-11_1_3_grid'"""
    return "data/grid_topology/" + "/".join(grid_id.split("__"))


def ground_truth_dir(grid_id: str) -> Path:
    d = GROUND_TRUTH_DIR / grid_id
    if not d.exists():
        abort(404, description=f"Unknown grid_id or missing ground-truth data: {grid_id}")
    return d


def load_profile_csv_path(grid_id: str) -> Path:
    rel = "/".join(grid_id.split("__"))
    return LOAD_PROFILES_DIR / (rel + ".csv")


def clean_records(df: pd.DataFrame) -> list:
    """DataFrame -> list of JSON-safe dict records (NaN/NaT -> None)."""
    return json.loads(df.to_json(orient="records"))


def parse_coords(coord_str: str):
    """'x1,y1;x2,y2;...' -> [[x1,y1],[x2,y2],...] ; returns [] for blank/NaN."""
    if not isinstance(coord_str, str) or not coord_str.strip():
        return []
    pts = []
    for pair in coord_str.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        x_str, y_str = pair.split(",")
        pts.append([float(x_str), float(y_str)])
    return pts


# ---------------------------------------------------------------------------
# In-process caches. Everything below is computed on first request and cached
# (functools.lru_cache) rather than precomputed to disk - the (up to ~380 MB)
# Parquet/CSV files are parsed at most once per grid per server run. A lock
# guards the very first load of each grid in case two requests race in
# (Flask's dev server can be multithreaded); duplicate work in that race is
# harmless, just wasteful.
# ---------------------------------------------------------------------------
_load_lock = threading.Lock()


@functools.lru_cache(maxsize=1)
def get_index_df() -> pd.DataFrame:
    df = pd.read_csv(ORGANIZED_DIR / "index.csv")
    df["grid_id"] = df["folder"].apply(grid_id_for_folder)
    return df


def grid_row_for_id(grid_id: str):
    df = get_index_df()
    matches = df[df["grid_id"] == grid_id]
    if matches.empty:
        abort(404, description=f"Unknown grid_id: {grid_id}")
    return matches.iloc[0]


@functools.lru_cache(maxsize=1)
def get_noise_levels() -> list:
    df = pd.read_csv(PROJECT_ROOT / "data" / "noise-model-config.csv")
    return clean_records(df)


@functools.lru_cache(maxsize=1)
def get_time_range() -> dict:
    """Read timestamp_utc from every grid's bus.parquet, confirm they all share the exact same
    4-week/15-min grid, and return one shared {start, end, step_minutes, count} block."""
    grid_ids = get_index_df()["grid_id"].tolist()
    all_ts = None
    for gid in grid_ids:
        path = GROUND_TRUTH_DIR / gid / "bus.parquet"
        ts = pq.read_table(path, columns=["timestamp_utc"])["timestamp_utc"].to_pandas()
        ts = pd.Index(ts.unique()).sort_values()
        if all_ts is None:
            all_ts = ts
        elif not ts.equals(all_ts):
            print(f"WARNING: {gid} has a different timestamp grid than the first grid checked "
                  f"({len(ts)} vs {len(all_ts)} timestamps, or different values) - the shared "
                  f"time_range will use the first grid's grid; per-grid /api calls still snap "
                  f"independently so this is not fatal, but flagging since it's unexpected.")

    step_minutes = int((all_ts[1] - all_ts[0]).total_seconds() / 60)
    return {
        "start": all_ts[0].isoformat() + "Z",
        "end": all_ts[-1].isoformat() + "Z",
        "step_minutes": step_minutes,
        "count": len(all_ts),
    }


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_meta(grid_id: str) -> dict:
    row = grid_row_for_id(grid_id)
    return {
        "grid_id": grid_id,
        "voltage_level": row.voltage_level,
        "category": row.category,
        "size_tier": row.size_tier,
        "n_bus": int(row.n_bus),
        "n_line": int(row.n_line),
        "n_trafo": int(row.n_trafo),
        "n_load": int(row.n_load),
        "has_10s_stats": (HIGHRES_DIR / grid_id / "bus.parquet").exists(),
    }


def compute_prosumer_stats(grid_id: str) -> dict:
    """For every load_<id>_p_mw column in this grid's full 4-week/15-min load-profile CSV, flag it
    as a "prosumer" if it ever goes negative (net export - real behind-the-meter PV in the
    underlying ETH smart-meter data, not noise). Returns {load_id: {is_prosumer, frac_export,
    peak_export_mw}}. Missing/unreadable file -> {} (caller defaults every load's is_prosumer to
    False rather than erroring)."""
    path = load_profile_csv_path(grid_id)
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    n = len(df)
    stats = {}
    for col in df.columns:
        if not (col.startswith("load_") and col.endswith("_p_mw")):
            continue
        load_id = int(col[len("load_"):-len("_p_mw")])
        vals = df[col].to_numpy()
        n_export = int((vals < 0).sum())
        is_prosumer = n_export > 0
        stats[load_id] = {
            "is_prosumer": is_prosumer,
            "frac_export": (n_export / n) if n else 0.0,
            "peak_export_mw": float(-vals.min()) if is_prosumer else 0.0,
        }
    return stats


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_topology(grid_id: str) -> dict:
    grid_dir = PROJECT_ROOT / folder_rel_for_grid_id(grid_id)
    buses = pd.read_csv(grid_dir / "buses.csv")
    lines = pd.read_csv(grid_dir / "lines.csv")
    trafos = pd.read_csv(grid_dir / "trafos.csv")
    ext_grid = pd.read_csv(grid_dir / "ext_grid.csv")
    loads = pd.read_csv(grid_dir / "loads.csv")
    bus_geo = pd.read_csv(grid_dir / "bus_geodata.csv")
    line_geo = pd.read_csv(grid_dir / "line_geodata.csv")

    buses = buses.merge(bus_geo[["bus", "x", "y", "source"]].rename(columns={"source": "geo_source"}),
                         on="bus", how="left")

    ext_grid_buses = set(ext_grid["bus"].tolist())
    trafo_buses = set(trafos["hv_bus"].tolist()) | set(trafos["lv_bus"].tolist())

    def classify(bus_id):
        if bus_id in ext_grid_buses:
            return "ext_grid"
        if bus_id in trafo_buses:
            return "trafo"
        return "regular"

    buses["role"] = buses["bus"].apply(classify)

    bus_records = clean_records(buses)
    # Add lat/lon (WGS84) alongside the existing x/y (EPSG:2056) fields, for the Leaflet/OSM
    # basemap. x/y are left untouched.
    for rec in bus_records:
        if rec.get("x") is None or rec.get("y") is None:
            rec["lat"], rec["lon"] = None, None
            continue
        lat, lon = to_latlon(rec["x"], rec["y"])
        rec["lat"], rec["lon"] = lat, lon

    line_geo_map = {row.line: row.coords for row in line_geo.itertuples()}
    line_geo_src = {row.line: row.source for row in line_geo.itertuples()}
    line_records = clean_records(lines)
    for rec in line_records:
        coord_str = line_geo_map.get(rec["line"], "")
        rec["coords"] = parse_coords(coord_str)
        rec["geo_source"] = line_geo_src.get(rec["line"], "synthetic_straight")
        # coords_latlon: parallel list to coords, each point [lat, lon] (Leaflet's own
        # L.polyline/L.latLng order - NOT [lon, lat]).
        rec["coords_latlon"] = [list(to_latlon(x, y)) for x, y in rec["coords"]]

    # Prosumer/PV layer: join in per-load prosumer stats derived from the real 4-week
    # load-profile CSV (behind-the-meter PV showing up as negative P on ordinary loads - not a
    # separate sgen/gen element anywhere in this dataset). A missing/unmatched load defaults to
    # is_prosumer=False rather than erroring.
    prosumer_stats = compute_prosumer_stats(grid_id)
    load_records = clean_records(loads)
    for rec in load_records:
        stats = prosumer_stats.get(int(rec["load"]))
        if stats is None:
            rec["is_prosumer"] = False
            rec["frac_export"] = 0.0
            rec["peak_export_mw"] = 0.0
        else:
            rec.update(stats)

    return {
        "buses": bus_records,
        "lines": line_records,
        "trafos": clean_records(trafos),
        "ext_grid": clean_records(ext_grid),
        "loads": load_records,
    }


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_pmu_penetration(grid_id: str) -> dict:
    """penetration_level (e.g. '5%') -> {level_pct, buses: [...], feeder_root_bus}. Level '0%' is
    added explicitly (not in the CSV - 0% penetration means no PMU-equipped bus at all, still
    needs to exist as a selectable option per the spec's 7 levels)."""
    grid_dir = PROJECT_ROOT / folder_rel_for_grid_id(grid_id)
    df = pd.read_csv(grid_dir / "pmu_penetration_selection.csv")
    levels = {"0%": {"level_pct": 0, "buses": [], "feeder_root_bus": None}}
    for level, sub in df.groupby("penetration_level"):
        feeder_root = sub.loc[sub["is_feeder_root"], "bus"]
        levels[str(level)] = {
            "level_pct": int(sub["level_pct"].iloc[0]),
            "buses": sorted(int(b) for b in sub["bus"].tolist()),
            "feeder_root_bus": int(feeder_root.iloc[0]) if len(feeder_root) else None,
        }
    return levels


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_bus_df(grid_id: str) -> pd.DataFrame:
    with _load_lock:
        path = ground_truth_dir(grid_id) / "bus.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "bus", "vm_pu"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_line_df(grid_id: str) -> pd.DataFrame:
    with _load_lock:
        path = ground_truth_dir(grid_id) / "line.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "line", "loading_percent"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_unique_timestamps(grid_id: str) -> pd.DatetimeIndex:
    return get_bus_df(grid_id).index.unique().sort_values()


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_bus_full_df(grid_id: str) -> pd.DataFrame:
    """Like get_bus_df but also carries va_degree - needed for PMU readings (magnitude+phase),
    not just the vm_pu-only voltage overlay. Kept as a separate cached frame so the existing
    overlay endpoint's cached data/behaviour is untouched."""
    with _load_lock:
        path = ground_truth_dir(grid_id) / "bus.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "bus", "vm_pu", "va_degree"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=16)  # an MV_LV column is ~150 MB in memory
def get_highres_df(grid_id: str, element: str, column: str) -> pd.DataFrame:
    """One statistic column of data/power_flow_results_10s_stats/<grid_id>/<element>.parquet,
    indexed by timestamp_utc. Read per column: the MV_LV files are several hundred MB each."""
    path = HIGHRES_DIR / grid_id / f"{element}.parquet"
    if not path.exists():
        abort(404, description=f"No 10-s statistics for grid_id: {grid_id}")
    with _load_lock:
        df = pq.read_table(path, columns=["timestamp_utc", element, column]).to_pandas()
        return df.set_index("timestamp_utc").sort_index()


def parse_stat() -> str | None:
    stat = request.args.get("stat")
    if stat and stat not in HIGHRES_STATS:
        abort(400, description=f"Unknown stat {stat!r}; available: {list(HIGHRES_STATS)}")
    return stat or None


def highres_at(grid_id: str, element: str, column: str, ts: pd.Timestamp):
    """(snapped timestamp, {element id: value}) for one statistic column at one window."""
    df = get_highres_df(grid_id, element, column)
    snapped = snap_timestamp(pd.Index(df.index.unique()), ts)
    rows = df.loc[[snapped]]
    return snapped, dict(zip(rows[element].tolist(), rows[column].astype(float).tolist()))


def highres_rows(grid_id: str, element: str, ts: pd.Timestamp):
    """(snapped timestamp, every statistic column of one element file at one window). Reads by
    row-group filter instead of the per-column cache: all 7 stats of an MV_LV file don't fit it."""
    path = HIGHRES_DIR / grid_id / f"{element}.parquet"
    if not path.exists():
        abort(404, description=f"No 10-s statistics for grid_id: {grid_id}")
    snapped = snap_timestamp(get_unique_timestamps(grid_id), ts)
    df = pq.read_table(path, filters=[("timestamp_utc", "==", snapped)]).to_pandas()
    return snapped, df.set_index(element)


def stat_dicts(row, quantities) -> dict:
    return {q: {s: float(row[f"{q}_{s}"]) for s in HIGHRES_STATS} for q in quantities}


def is_10s() -> bool:
    data = request.args.get("data", "15min")
    if data not in ("15min", "10s"):
        abort(400, description=f"Unknown data {data!r}; available: ['15min', '10s']")
    return data == "10s"


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_std_scale(grid_id: str) -> dict:
    return {
        "bus": float(get_highres_df(grid_id, "bus", "vm_pu_std")["vm_pu_std"].quantile(0.99)),
        "line": float(get_highres_df(grid_id, "line", "loading_percent_std")["loading_percent_std"].quantile(0.99)),
    }


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_load_profile_df(grid_id: str) -> pd.DataFrame:
    with _load_lock:
        path = load_profile_csv_path(grid_id)
        if not path.exists():
            abort(404, description=f"No load-profile data for grid_id: {grid_id}")
        df = pd.read_csv(path)
        ts = pd.to_datetime(df["timestamp_utc"], utc=True).dt.tz_localize(None)
        df = df.drop(columns=["timestamp_utc"])
        df.index = ts
        df = df.sort_index()
        return df


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_loads_meta(grid_id: str) -> pd.DataFrame:
    path = PROJECT_ROOT / folder_rel_for_grid_id(grid_id) / "loads.csv"
    return pd.read_csv(path)[["load", "bus"]]


@functools.lru_cache(maxsize=GRID_CACHE_SIZE)
def get_bus_loads_map(grid_id: str) -> dict:
    """bus_id -> list of load ids attached to that bus. Used to derive a PMU's net active/reactive
    power injection (sum of its bus's loads' p_mw/q_mvar) - a real PMU measures line/branch power
    flow, but no branch P/Q is stored anywhere in data/output/ (only vm_pu/va_degree for bus,
    i_ka/loading_percent for line), and adding it would require re-running power flow for every
    grid. Net load injection reuses data already computed for the smart-meter table and is exact
    for any bus with loads attached; a bus with none attached (a pass-through pole) correctly
    reports zero net injection - it does not carry current in isolation, only as part of a branch
    flow, which this approximation does not attempt to reconstruct."""
    loads_meta = get_loads_meta(grid_id)
    out: dict = {}
    for load_id, bus_id in zip(loads_meta["load"], loads_meta["bus"]):
        out.setdefault(int(bus_id), []).append(int(load_id))
    return out


def net_injection_at_bus(row: pd.Series, bus_id: int, bus_loads: dict) -> tuple:
    """Sum p_mw/q_mvar over every load attached to bus_id, reading columns off `row` (one row of
    get_load_profile_df, i.e. load_<id>_p_mw / load_<id>_q_mvar). Returns (0.0, 0.0) for a bus with
    no attached loads."""
    p_total, q_total = 0.0, 0.0
    for load_id in bus_loads.get(bus_id, []):
        p_col, q_col = f"load_{load_id}_p_mw", f"load_{load_id}_q_mvar"
        if p_col in row.index:
            p_total += float(row[p_col])
            q_total += float(row[q_col])
    return p_total, q_total


# ---------------------------------------------------------------------------
# Timestamp parsing / snapping
# ---------------------------------------------------------------------------

def parse_timestamp(raw: str) -> pd.Timestamp:
    try:
        ts = pd.Timestamp(raw)
    except (ValueError, TypeError):
        abort(400, description=f"Could not parse timestamp: {raw!r}")
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def snap_timestamp(unique_index: pd.DatetimeIndex, ts: pd.Timestamp) -> pd.Timestamp:
    """Clamp ts into [min, max] of unique_index, then snap to the nearest actual grid point."""
    tmin, tmax = unique_index[0], unique_index[-1]
    ts = max(tmin, min(tmax, ts))
    pos = unique_index.searchsorted(ts)
    if pos <= 0:
        return unique_index[0]
    if pos >= len(unique_index):
        return unique_index[-1]
    before, after = unique_index[pos - 1], unique_index[pos]
    return before if (ts - before) <= (after - ts) else after


# ---------------------------------------------------------------------------
# Routes: static page + live-computed JSON
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    return redirect("/static/index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/data/index.json")
def data_index():
    return jsonify(clean_records(get_index_df()))


@app.route("/data/noise_levels.json")
def data_noise_levels():
    return jsonify(get_noise_levels())


@app.route("/data/time_range.json")
def data_time_range():
    return jsonify(get_time_range())


@app.route("/data/<grid_id>/meta.json")
def data_meta(grid_id):
    return jsonify(get_meta(grid_id))


@app.route("/data/<grid_id>/topology.json")
def data_topology(grid_id):
    return jsonify(get_topology(grid_id))


@app.route("/data/<grid_id>/pmu_penetration.json")
def data_pmu_penetration(grid_id):
    return jsonify(get_pmu_penetration(grid_id))


# ---------------------------------------------------------------------------
# Routes: live ground-truth API
# ---------------------------------------------------------------------------

@app.route("/api/<grid_id>/at")
def api_at(grid_id):
    raw_ts = request.args.get("timestamp")
    if not raw_ts:
        abort(400, description="Missing required query param: timestamp")
    ts = parse_timestamp(raw_ts)

    stat = parse_stat()
    if stat:
        snapped, bus_map = highres_at(grid_id, "bus", f"vm_pu_{stat}", ts)
        _, line_map = highres_at(grid_id, "line", f"loading_percent_{stat}", ts)
        resp = {"grid_id": grid_id, "requested_timestamp": raw_ts, "stat": stat,
                "timestamp": snapped.isoformat() + "Z", "bus": bus_map, "line": line_map}
        if stat == "std":
            resp["std_scale"] = get_std_scale(grid_id)
        return jsonify(resp)

    bus_df = get_bus_df(grid_id)
    line_df = get_line_df(grid_id)
    unique_ts = get_unique_timestamps(grid_id)
    snapped = snap_timestamp(unique_ts, ts)

    bus_rows = bus_df.loc[[snapped]]
    line_rows = line_df.loc[[snapped]]

    bus_map = dict(zip(bus_rows["bus"].tolist(), bus_rows["vm_pu"].tolist()))
    line_map = dict(zip(line_rows["line"].tolist(), line_rows["loading_percent"].tolist()))

    return jsonify({
        "grid_id": grid_id,
        "requested_timestamp": raw_ts,
        "timestamp": snapped.isoformat() + "Z",
        "bus": bus_map,
        "line": line_map,
    })


@app.route("/api/<grid_id>/smart_meter")
def api_smart_meter(grid_id):
    raw_ts = request.args.get("timestamp")
    if not raw_ts:
        abort(400, description="Missing required query param: timestamp")
    ts = parse_timestamp(raw_ts)

    loads_meta = get_loads_meta(grid_id)
    if is_10s():
        snapped, bus_rows = highres_rows(grid_id, "bus", ts)
        _, load_rows = highres_rows(grid_id, "load", ts)
        out = [{"load": int(load_id), "bus": int(bus_id),
                **stat_dicts(load_rows.loc[load_id], ("p_mw", "q_mvar")),
                **stat_dicts(bus_rows.loc[bus_id], ("vm_pu",))}
               for load_id, bus_id in zip(loads_meta["load"], loads_meta["bus"])]
        return jsonify({"grid_id": grid_id, "requested_timestamp": raw_ts, "data": "10s",
                        "timestamp": snapped.isoformat() + "Z", "loads": out})

    bus_df = get_bus_df(grid_id)
    unique_ts = get_unique_timestamps(grid_id)
    snapped = snap_timestamp(unique_ts, ts)
    bus_rows = bus_df.loc[[snapped]]
    vm_by_bus = dict(zip(bus_rows["bus"].tolist(), bus_rows["vm_pu"].tolist()))

    lp_df = get_load_profile_df(grid_id)
    lp_unique_ts = pd.Index(lp_df.index.unique()).sort_values()
    lp_snapped = snap_timestamp(lp_unique_ts, ts)
    row = lp_df.loc[lp_snapped]
    if isinstance(row, pd.DataFrame):  # duplicate index safety net (shouldn't happen)
        row = row.iloc[0]

    out = []
    for load_id, bus_id in zip(loads_meta["load"], loads_meta["bus"]):
        p_col, q_col = f"load_{load_id}_p_mw", f"load_{load_id}_q_mvar"
        if p_col not in row.index:
            continue
        out.append({
            "load": int(load_id),
            "bus": int(bus_id),
            "p_mw": float(row[p_col]),
            "q_mvar": float(row[q_col]),
            "vm_pu": vm_by_bus.get(int(bus_id)),
        })

    return jsonify({
        "grid_id": grid_id,
        "requested_timestamp": raw_ts,
        "timestamp": lp_snapped.isoformat() + "Z",
        "loads": out,
    })


@app.route("/api/<grid_id>/pmu_readings")
def api_pmu_readings(grid_id):
    raw_ts = request.args.get("timestamp")
    if not raw_ts:
        abort(400, description="Missing required query param: timestamp")
    level = request.args.get("penetration")
    if not level:
        abort(400, description="Missing required query param: penetration")

    penetration = get_pmu_penetration(grid_id)
    if level not in penetration:
        abort(400, description=f"Unknown penetration level {level!r}; available: {sorted(penetration.keys())}")

    level_info = penetration[level]
    bus_ids = level_info.get("buses", [])
    feeder_root_bus = level_info.get("feeder_root_bus")

    ts = parse_timestamp(raw_ts)

    if not bus_ids:
        # 0% penetration (or any level with no PMUs assigned) - no measurements to return.
        unique_ts = get_unique_timestamps(grid_id)
        snapped = snap_timestamp(unique_ts, ts)
        return jsonify({
            "grid_id": grid_id,
            "penetration_level": level,
            "requested_timestamp": raw_ts,
            "timestamp": snapped.isoformat() + "Z",
            "pmus": [],
        })

    if is_10s():
        # P/Q: sum of the bus's load means, exact for the mean only (a max of a sum is not a sum of maxes).
        snapped, bus_rows = highres_rows(grid_id, "bus", ts)
        _, load_rows = highres_rows(grid_id, "load", ts)
        bus_loads = get_bus_loads_map(grid_id)
        pmus = [{"bus": int(bus_id), **stat_dicts(bus_rows.loc[bus_id], ("vm_pu", "va_degree")),
                 "p_mw_mean": float(load_rows.loc[bus_loads.get(bus_id, []), "p_mw_mean"].sum()),
                 "q_mvar_mean": float(load_rows.loc[bus_loads.get(bus_id, []), "q_mvar_mean"].sum()),
                 "is_feeder_root": bus_id == feeder_root_bus}
                for bus_id in bus_ids]
        return jsonify({"grid_id": grid_id, "penetration_level": level, "requested_timestamp": raw_ts,
                        "data": "10s", "timestamp": snapped.isoformat() + "Z", "pmus": pmus})

    bus_df = get_bus_full_df(grid_id)
    unique_ts = get_unique_timestamps(grid_id)
    snapped = snap_timestamp(unique_ts, ts)
    bus_rows = bus_df.loc[[snapped]]

    wanted = set(bus_ids)
    rows = bus_rows[bus_rows["bus"].isin(wanted)]
    by_bus = {int(r.bus): (float(r.vm_pu), float(r.va_degree)) for r in rows.itertuples()}

    bus_loads = get_bus_loads_map(grid_id)
    lp_df = get_load_profile_df(grid_id)
    lp_unique_ts = pd.Index(lp_df.index.unique()).sort_values()
    lp_snapped = snap_timestamp(lp_unique_ts, ts)
    lp_row = lp_df.loc[lp_snapped]
    if isinstance(lp_row, pd.DataFrame):  # duplicate index safety net (shouldn't happen)
        lp_row = lp_row.iloc[0]

    pmus = []
    for bus_id in bus_ids:
        vals = by_bus.get(int(bus_id))
        if vals is None:
            continue  # shouldn't happen (bus list comes from this grid's own topology), but don't crash
        vm_pu, va_degree = vals
        p_mw, q_mvar = net_injection_at_bus(lp_row, int(bus_id), bus_loads)
        pmus.append({
            "bus": int(bus_id),
            "vm_pu": vm_pu,
            "va_degree": va_degree,
            "p_mw": p_mw,
            "q_mvar": q_mvar,
            "is_feeder_root": bus_id == feeder_root_bus,
        })

    return jsonify({
        "grid_id": grid_id,
        "penetration_level": level,
        "requested_timestamp": raw_ts,
        "timestamp": snapped.isoformat() + "Z",
        "pmus": pmus,
    })


# ---------------------------------------------------------------------------
# Route: scenario download (smart_meter.csv + pmu.csv for a date range, noise applied)
# ---------------------------------------------------------------------------

def noise_sigma(device, quantity, level, value, p=None, q=None):
    """Noise std per row, same model as the page's (data/noise-model-config.csv); None = no noise."""
    for r in get_noise_levels():
        if (r["device_type"], r["quantity"], r["noise_level"]) == (device, quantity, level):
            if r["std_value"] <= 0:
                return None
            if r["noise_basis"] == "relative_to_value":
                return r["std_value"] * value.abs()
            if r["noise_basis"] == "relative_to_apparent_power":
                return r["std_value"] * np.hypot(p, q)
            return r["std_value"]
    return None


def add_noise(df, rng, level, ten_s, device, specs):
    """specs: (config quantity, column stem). One draw per row and quantity, sized from the
    (window-mean) value; for 10-s data it shifts every location stat by the same amount, so
    min <= q10 <= ... <= max still holds, and leaves std alone (an offset has no spread)."""
    sfx = "_mean" if ten_s else ""
    clean = df.copy()
    for quantity, col in specs:
        if col + sfx not in df:
            continue
        sigma = noise_sigma(device, quantity, level, clean[col + sfx],
                            clean.get("p_mw" + sfx), clean.get("q_mvar" + sfx))
        if sigma is None:
            continue
        offset = rng.standard_normal(len(df)) * sigma
        for c in ([f"{col}_{s}" for s in HIGHRES_STATS if s != "std"] if ten_s else [col]):
            if c in df:
                df[c] = clean[c] + offset


def read_highres_range(grid_id, element, columns, start, end, ids=None):
    filters = [("timestamp_utc", ">=", start), ("timestamp_utc", "<", end)]
    if ids is not None:
        filters.append((element, "in", ids))
    path = HIGHRES_DIR / grid_id / f"{element}.parquet"
    if not path.exists():
        abort(404, description=f"No 10-s statistics for grid_id: {grid_id}")
    return pq.read_table(path, columns=["timestamp_utc", element, *columns], filters=filters).to_pandas()


def scenario_frames(grid_id, ten_s, level, start, end):
    """(smart_meter, pmu) DataFrames for timestamps in [start, end), clean."""
    loads_meta = get_loads_meta(grid_id)
    pmu_info = get_pmu_penetration(grid_id)[level]
    pmu_buses = pmu_info["buses"]

    if ten_s:
        stat_cols = lambda *qs: [f"{q}_{s}" for q in qs for s in HIGHRES_STATS]
        load = read_highres_range(grid_id, "load", stat_cols("p_mw", "q_mvar"), start, end)
        load = load.merge(loads_meta, on="load")
        bus = read_highres_range(grid_id, "bus", stat_cols("vm_pu"), start, end)
        sm = load.merge(bus, on=["timestamp_utc", "bus"], how="left")
        sm = sm[["timestamp_utc", "load", "bus", *stat_cols("vm_pu", "p_mw", "q_mvar")]]
        pmu = read_highres_range(grid_id, "bus", stat_cols("vm_pu", "va_degree"), start, end, pmu_buses)
        load = load.rename(columns={"p_mw_mean": "p_mw", "q_mvar_mean": "q_mvar"})
    else:
        lp = get_load_profile_df(grid_id)
        lp = lp[(lp.index >= start) & (lp.index < end)]
        bus = get_bus_full_df(grid_id)
        bus = bus[(bus.index >= start) & (bus.index < end)].reset_index()
        load_ids = loads_meta["load"].to_numpy()
        n_ts, n_load = len(lp), len(load_ids)
        load = pd.DataFrame({
            "timestamp_utc": np.repeat(lp.index.to_numpy(), n_load),
            "load": np.tile(load_ids, n_ts),
            "bus": np.tile(loads_meta["bus"].to_numpy(), n_ts),
            "p_mw": lp[[f"load_{i}_p_mw" for i in load_ids]].to_numpy().reshape(-1),
            "q_mvar": lp[[f"load_{i}_q_mvar" for i in load_ids]].to_numpy().reshape(-1),
        })
        sm = load.merge(bus[["timestamp_utc", "bus", "vm_pu"]], on=["timestamp_utc", "bus"], how="left")
        sm = sm[["timestamp_utc", "load", "bus", "vm_pu", "p_mw", "q_mvar"]]
        pmu = bus[bus["bus"].isin(pmu_buses)]

    # PMU P/Q = net injection of the bus's loads (see get_bus_loads_map); 10-s: window mean only,
    # since a sum of means is the mean of the sum but a sum of maxes is not the max of the sum.
    p, q = ("p_mw_mean", "q_mvar_mean") if ten_s else ("p_mw", "q_mvar")
    inj = (load[load["bus"].isin(pmu_buses)].groupby(["timestamp_utc", "bus"])[["p_mw", "q_mvar"]]
           .sum().rename(columns={"p_mw": p, "q_mvar": q}).reset_index())
    pmu = pmu.merge(inj, on=["timestamp_utc", "bus"], how="left").fillna({p: 0.0, q: 0.0})
    pmu["is_feeder_root"] = pmu["bus"] == pmu_info["feeder_root_bus"]
    pmu = pmu[["timestamp_utc", "bus", *[c for c in pmu.columns if c not in ("timestamp_utc", "bus")]]]
    return (sm.sort_values(["timestamp_utc", "load"], ignore_index=True),
            pmu.sort_values(["timestamp_utc", "bus"], ignore_index=True))


@app.route("/api/<grid_id>/download")
def api_download(grid_id):
    ten_s = is_10s()
    noise = request.args.get("noise", "0%")
    level = request.args.get("penetration", "0%")
    if noise not in {r["noise_level"] for r in get_noise_levels()}:
        abort(400, description=f"Unknown noise level {noise!r}")
    if level not in get_pmu_penetration(grid_id):
        abort(400, description=f"Unknown penetration level {level!r}")
    tr = get_time_range()
    start = parse_timestamp(request.args.get("start") or tr["start"]).normalize()
    last_day = parse_timestamp(request.args.get("end") or tr["end"]).normalize()
    end = last_day + pd.Timedelta(days=1)
    if end <= start:
        abort(400, description="end is before start")

    sm, pmu = scenario_frames(grid_id, ten_s, level, start, end)
    rng = np.random.default_rng(zlib.crc32(f"{grid_id}:{ten_s}:{noise}:{level}:{start}:{end}".encode()))
    add_noise(sm, rng, noise, ten_s, "smart_meter",
              [("vm_pu", "vm_pu"), ("p_mw", "p_mw"), ("q_mvar", "q_mvar")])
    add_noise(pmu, rng, noise, ten_s, "pmu",
              [("vm_pu", "vm_pu"), ("va_degree", "va_degree"), ("far_p_mw", "p_mw"), ("far_q_mvar", "q_mvar")])

    name = (f"{grid_id}_{'10s' if ten_s else '15min'}_noise{noise.rstrip('%')}_"
            f"pmu{level.rstrip('%')}_{start:%Y%m%d}-{last_day:%Y%m%d}")
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for fname, df in (("smart_meter.csv", sm), ("pmu.csv", pmu)):
            df["timestamp_utc"] = df["timestamp_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")
            if ten_s:  # the 10-s files are float32; keep that instead of float64's 17 digits
                floats = df.select_dtypes("float64").columns
                df[floats] = df[floats].astype("float32")
            with zf.open(f"{name}/{fname}", "w", force_zip64=True) as fh:
                pacsv.write_csv(pa.Table.from_pandas(df, preserve_index=False), fh)
    fh = open(tmp, "rb")
    os.unlink(tmp)  # the open handle keeps the data until send_file closes it
    return send_file(fh, mimetype="application/zip", as_attachment=True, download_name=f"{name}.zip")


@app.after_request
def no_cache(response):
    # Local dev tool - avoid the browser serving a stale cached copy of app.js/JSON after an edit.
    response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": str(e.description) if hasattr(e, "description") else "not found"}), 404


@app.errorhandler(400)
def handle_400(e):
    return jsonify({"error": str(e.description) if hasattr(e, "description") else "bad request"}), 400


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8811, debug=False, threaded=True)
