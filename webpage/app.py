"""
Step 11 (redesign) - Data-viewer webpage backend.

Small local Flask app. Serves:
  - the static HTML/CSS/JS page (unchanged in spirit from the original Step 11 build)
  - the pre-built, timestamp-independent JSON under code/webpage/data/ (index, topology, meta,
    noise config, PMU-penetration bus lists, shared time range) as plain static files
  - a LIVE API for timestamp-dependent ground truth, reading straight from
    data/ground-truth-full/<grid_id>/{bus,line}.parquet - this data is far too large (1.1 GB
    across 35 grids, one grid alone is 379 MB) to ship to the browser wholesale or pre-explode
    into one JSON file per timestamp (2,688 timestamps x 35 grids), so it is served on demand
    instead, with the parsed-and-indexed DataFrame for each grid cached in-process after its
    first request.

Endpoints:
  GET /                                            -> redirects to /static/index.html
  GET /static/<path>                               -> the page itself (html/js/css)
  GET /data/<path>                                 -> pre-built static JSON (index.json, per-grid
                                                       topology.json/meta.json/pmu_penetration.json,
                                                       noise_levels.json, time_range.json)
  GET /api/<grid_id>/at?timestamp=<iso8601>        -> {timestamp, bus: {bus_id: vm_pu, ...},
                                                        line: {line_id: loading_percent, ...}}
  GET /api/<grid_id>/smart_meter?timestamp=<iso8601> -> [{load, bus, p_mw, q_mvar, vm_pu}, ...]
                                                        (optional smart-meter table; joins
                                                        data/load-profiles/<...>.csv with the
                                                        same ground-truth bus voltages)
  GET /api/<grid_id>/pmu_readings?timestamp=<iso8601>&penetration=<level>
                                                      -> {pmus: [{bus, vm_pu, va_degree,
                                                        is_feeder_root}, ...]} for every bus
                                                        selected at that PMU-penetration level
                                                        (data/webpage/data/<grid_id>/pmu_penetration.json,
                                                        the same file the topology overlay's PMU
                                                        markers already use); empty list at 0%.

This is a local-only tool (no auth) meant to run on Mohammad's own machine - not hardened for
untrusted input beyond basic clamping/error handling so a bad request doesn't crash the process.
"""
import functools
import threading
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from flask import Flask, abort, jsonify, request, redirect, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "ground-truth-full"
ORGANIZED_DIR = PROJECT_ROOT / "data" / "organized"
LOAD_PROFILES_DIR = PROJECT_ROOT / "data" / "load-profiles"
WEBPAGE_DIR = Path(__file__).resolve().parent
STATIC_DATA_DIR = WEBPAGE_DIR / "data"   # pre-built JSON from build_data.py
STATIC_DIR = WEBPAGE_DIR / "static"      # html/js/css

app = Flask(__name__)

# ---------------------------------------------------------------------------
# grid_id <-> folder path helpers (must match build_data.py's grid_id_for_folder)
# ---------------------------------------------------------------------------

def folder_rel_for_grid_id(grid_id: str) -> str:
    """'LV__Alps-Periurban__5238-11_1_3_grid' -> 'data/organized/LV/Alps-Periurban/5238-11_1_3_grid'"""
    return "data/organized/" + "/".join(grid_id.split("__"))


def ground_truth_dir(grid_id: str) -> Path:
    d = GROUND_TRUTH_DIR / grid_id
    if not d.exists():
        abort(404, description=f"Unknown grid_id or missing ground-truth data: {grid_id}")
    return d


def load_profile_csv_path(grid_id: str) -> Path:
    rel = "/".join(grid_id.split("__"))
    return LOAD_PROFILES_DIR / (rel + ".csv")


# ---------------------------------------------------------------------------
# In-process caches. A handful of small helper functions, each lru_cache'd by
# grid_id, so the (up to ~380 MB) Parquet/CSV files are parsed at most once per
# grid per server run rather than on every request. A lock guards the very
# first load of each grid in case two requests race in (Flask's dev server can
# be multithreaded); duplicate work in that race is harmless, just wasteful.
# ---------------------------------------------------------------------------
_load_lock = threading.Lock()


@functools.lru_cache(maxsize=64)
def get_bus_df(grid_id: str) -> pd.DataFrame:
    with _load_lock:
        path = ground_truth_dir(grid_id) / "bus.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "bus", "vm_pu"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=64)
def get_line_df(grid_id: str) -> pd.DataFrame:
    with _load_lock:
        path = ground_truth_dir(grid_id) / "line.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "line", "loading_percent"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=64)
def get_unique_timestamps(grid_id: str) -> pd.DatetimeIndex:
    return get_bus_df(grid_id).index.unique().sort_values()


@functools.lru_cache(maxsize=64)
def get_bus_full_df(grid_id: str) -> pd.DataFrame:
    """Like get_bus_df but also carries va_degree - needed for PMU readings (magnitude+phase),
    not just the vm_pu-only voltage overlay. Kept as a separate cached frame (rather than adding
    va_degree to get_bus_df) so the existing overlay endpoint's cached data/behaviour is untouched.
    """
    with _load_lock:
        path = ground_truth_dir(grid_id) / "bus.parquet"
        table = pq.read_table(path, columns=["timestamp_utc", "bus", "vm_pu", "va_degree"])
        df = table.to_pandas()
        df = df.set_index("timestamp_utc").sort_index()
        return df


@functools.lru_cache(maxsize=64)
def get_pmu_penetration(grid_id: str) -> dict:
    """The pre-built code/webpage/data/<grid_id>/pmu_penetration.json (build_data.py) - same file
    the frontend's PMU-marker overlay already uses, so the readings table stays in exact sync with
    which buses are drawn as PMU markers at a given penetration level."""
    path = STATIC_DATA_DIR / grid_id / "pmu_penetration.json"
    if not path.exists():
        abort(404, description=f"No PMU-penetration data for grid_id: {grid_id}")
    import json
    with open(path) as f:
        return json.load(f)


@functools.lru_cache(maxsize=64)
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


@functools.lru_cache(maxsize=64)
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
# Routes: static page + pre-built JSON
# ---------------------------------------------------------------------------

@app.route("/")
def root():
    return redirect("/static/index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/data/<path:filename>")
def static_data_files(filename):
    return send_from_directory(STATIC_DATA_DIR, filename)


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
    # Local dev tool - avoid the browser serving a stale cached copy of app.js/JSON after an
    # edit (a real issue hit during the original Step 11 build with plain http.server).
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
