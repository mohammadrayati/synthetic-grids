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
  GET /api/<grid_id>/pmu_readings?timestamp=<iso8601>&penetration=<level>
                                                      -> {pmus: [{bus, vm_pu, va_degree,
                                                        is_feeder_root}, ...]} for every bus
                                                        selected at that PMU-penetration level;
                                                        empty list at 0%.

<grid_id> is index.csv's `folder` column with the `data/grid_topology/` prefix stripped and `/`
replaced by `__`, e.g. 'data/grid_topology/LV/Alps-Periurban/5238-11_1_3_grid' <->
'LV__Alps-Periurban__5238-11_1_3_grid' <-> data/power_flow_results/LV__Alps-Periurban__5238-11_1_3_grid/.

This is a local-only tool (no auth) - not hardened for untrusted input beyond basic
clamping/error handling so a bad request doesn't crash the process.
"""
import functools
import json
import threading
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pyproj
from flask import Flask, abort, jsonify, request, redirect, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORGANIZED_DIR = PROJECT_ROOT / "data" / "grid_topology"
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "power_flow_results"
LOAD_PROFILES_DIR = PROJECT_ROOT / "data" / "load_power"
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

    loads_meta = get_loads_meta(grid_id)
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

    bus_df = get_bus_full_df(grid_id)
    unique_ts = get_unique_timestamps(grid_id)
    snapped = snap_timestamp(unique_ts, ts)
    bus_rows = bus_df.loc[[snapped]]

    wanted = set(bus_ids)
    rows = bus_rows[bus_rows["bus"].isin(wanted)]
    by_bus = {int(r.bus): (float(r.vm_pu), float(r.va_degree)) for r in rows.itertuples()}

    pmus = []
    for bus_id in bus_ids:
        vals = by_bus.get(int(bus_id))
        if vals is None:
            continue  # shouldn't happen (bus list comes from this grid's own topology), but don't crash
        vm_pu, va_degree = vals
        pmus.append({
            "bus": int(bus_id),
            "vm_pu": vm_pu,
            "va_degree": va_degree,
            "is_feeder_root": bus_id == feeder_root_bus,
        })

    return jsonify({
        "grid_id": grid_id,
        "penetration_level": level,
        "requested_timestamp": raw_ts,
        "timestamp": snapped.isoformat() + "Z",
        "pmus": pmus,
    })


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
