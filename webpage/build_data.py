"""
Step 11 (redesign) - Data-viewer webpage: static-data build script.

Pre-converts the parts of data/organized/<...>/*.csv (Step 10's export) that are small and
timestamp-independent into small JSON files under code/webpage/data/. Anything that is
timestamp-dependent (bus voltage / line loading at a specific point in the 4-week, 15-minute
time series) is now served LIVE by code/webpage/app.py straight from
data/ground-truth-full/<grid_id>/{bus,line}.parquet - it is not pre-exploded here, since
2,688 timestamps x 35 grids would again hit the "too many small files" problem this project
already avoided once at the 20-snapshot scale (see steps/step-11-web-viewer.md).

Run once (or re-run any time data/organized/ or data/ground-truth-full/ changes) with the
project venv:
    code/.venv/bin/python code/webpage/build_data.py

Output layout, all under code/webpage/data/:
    index.json                 <- the 35-grid list (from data/organized/index.csv)
    noise_levels.json          <- data/noise-model-config.csv in full (6 levels x device/quantity)
    time_range.json            <- the shared 4-week timestamp grid (start/end/step/count), derived
                                   from the ground-truth Parquet files themselves, not hardcoded
    <grid_id>/meta.json        <- per-grid summary (bus/line/trafo/load counts, source path)
    <grid_id>/topology.json    <- buses/lines/trafos/ext_grid/loads, geodata joined in (unchanged
                                   from the original Step 11 build)
    <grid_id>/pmu_penetration.json  <- per-level list of "PMU-equipped" buses (0/5/10/.../30%),
                                   from data/organized/<...>/pmu_penetration_selection.csv - this
                                   IS small enough to precompute (max 7 levels x <=30% of buses).

<grid_id> is index.csv's `folder` column with the `data/organized/` prefix stripped and `/`
replaced by `__` - this must exactly match data/ground-truth-full/'s own directory names
(verified: e.g. 'data/organized/LV/Alps-Periurban/5238-11_1_3_grid' <-> grid_id
'LV__Alps-Periurban__5238-11_1_3_grid' <-> data/ground-truth-full/LV__Alps-Periurban__5238-11_1_3_grid/).
"""
import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pyproj

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ORGANIZED_DIR = PROJECT_ROOT / "data" / "organized"
GROUND_TRUTH_DIR = PROJECT_ROOT / "data" / "ground-truth-full"
LOAD_PROFILES_DIR = PROJECT_ROOT / "data" / "load-profiles"
OUT_DIR = Path(__file__).resolve().parent / "data"

# Step 11 Redo 9: one reusable transformer, EPSG:2056 (Swiss LV95, meters, easting/northing) ->
# EPSG:4326 (WGS84, lat/lon degrees), for the Leaflet/OSM basemap. always_xy=True makes
# .transform(x, y) take (easting, northing) and return (lon, lat) in that (x, y)-semantics order -
# NOT (lat, lon). Leaflet itself wants [lat, lon] ordering, so callers below swap the transformer's
# own (lon, lat) output when building lat/lon fields - see the two call sites.
_TO_WGS84 = pyproj.Transformer.from_crs("EPSG:2056", "EPSG:4326", always_xy=True)


def to_latlon(x, y):
    """EPSG:2056 (x=easting, y=northing) -> (lat, lon) in WGS84 degrees."""
    lon, lat = _TO_WGS84.transform(x, y)
    return lat, lon


def grid_id_for_folder(folder_rel: str) -> str:
    """'data/organized/LV/Alps-Periurban/5238-11_1_3_grid' -> 'LV__Alps-Periurban__5238-11_1_3_grid'"""
    rel = folder_rel.split("data/organized/", 1)[-1]
    return rel.replace("/", "__")


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


def load_profile_csv_path(grid_id: str) -> Path:
    """Mirrors app.py's load_profile_csv_path() exactly (same grid_id <-> path convention)."""
    rel = "/".join(grid_id.split("__"))
    return LOAD_PROFILES_DIR / (rel + ".csv")


def compute_prosumer_stats(grid_id: str) -> dict:
    """Redo 10 (prosumer/PV layer): for every load_<id>_p_mw column in this grid's full
    4-week/15-min load-profile CSV, flag it as a "prosumer" if it ever goes negative (net
    export - real behind-the-meter PV in the underlying ETH smart-meter data, not noise; see
    steps/step-11-web-viewer.md Redo 10). Returns {load_id (int): {is_prosumer, frac_export,
    peak_export_mw}} for every load column found. Missing/unreadable file -> {} (caller then
    defaults every load's is_prosumer to False rather than crashing the whole build)."""
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


def build_topology(grid_dir: Path, grid_id: str) -> dict:
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
    # Step 11 Redo 9: add lat/lon (WGS84) alongside the existing x/y (EPSG:2056) fields, for the
    # Leaflet/OSM basemap. x/y are left completely untouched - nothing currently reading them
    # should break.
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
        # L.polyline/L.latLng order - NOT [lon, lat], different order than to_latlon()'s own
        # (lat, lon) return tuple would suggest if you weren't careful about it).
        rec["coords_latlon"] = [list(to_latlon(x, y)) for x, y in rec["coords"]]

    # Redo 10 (prosumer/PV layer): join in per-load prosumer stats derived from the real
    # 4-week load-profile CSV (behind-the-meter PV showing up as negative P on ordinary loads -
    # not a separate sgen/gen element anywhere in this dataset, see steps/step-11-web-viewer.md
    # Redo 10). A missing/unmatched load just defaults to is_prosumer=False rather than erroring.
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


def build_pmu_penetration(grid_dir: Path) -> dict:
    """penetration_level (e.g. '5%') -> {level_pct, buses: [...], feeder_root_bus}.
    Level '0%' is added explicitly (not in the CSV - 0% penetration means no PMU-equipped bus
    at all, still needs to exist as a selectable option per the spec's 7 levels)."""
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


def derive_time_range(grid_ids: list) -> dict:
    """Read timestamp_utc from every grid's bus.parquet, confirm they all share the exact same
    4-week/15-min grid (true as of this build - see step-11 redesign brief), and return one
    shared {start, end, step_minutes, count, timestamps} block rather than hardcoding it."""
    ranges = []
    all_ts = None
    for gid in grid_ids:
        path = GROUND_TRUTH_DIR / gid / "bus.parquet"
        ts = pq.read_table(path, columns=["timestamp_utc"])["timestamp_utc"].to_pandas()
        ts = pd.Index(ts.unique()).sort_values()
        ranges.append((gid, ts))
        if all_ts is None:
            all_ts = ts
        elif not ts.equals(all_ts):
            print(f"WARNING: {gid} has a different timestamp grid than the first grid checked "
                  f"({len(ts)} vs {len(all_ts)} timestamps, or different values) - the shared "
                  f"time_range.json will use the first grid's grid; per-grid /api calls still "
                  f"snap independently so this is not fatal, but flagging since it's unexpected.")

    step_minutes = int((all_ts[1] - all_ts[0]).total_seconds() / 60)
    return {
        "start": all_ts[0].isoformat() + "Z",
        "end": all_ts[-1].isoformat() + "Z",
        "step_minutes": step_minutes,
        "count": len(all_ts),
    }


def main():
    # Wipe and recreate OUT_DIR: avoids stale files lingering from the old (pre-redesign) build,
    # which used to write per-(snapshot,noise) smart_meter/pmu JSON files that no longer exist.
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    index_df = pd.read_csv(ORGANIZED_DIR / "index.csv")
    index_df["grid_id"] = index_df["folder"].apply(grid_id_for_folder)
    (OUT_DIR / "index.json").write_text(json.dumps(clean_records(index_df)))

    noise_cfg = pd.read_csv(PROJECT_ROOT / "data" / "noise-model-config.csv")
    (OUT_DIR / "noise_levels.json").write_text(json.dumps(clean_records(noise_cfg)))

    grid_ids = index_df["grid_id"].tolist()
    time_range = derive_time_range(grid_ids)
    (OUT_DIR / "time_range.json").write_text(json.dumps(time_range))
    print(f"time_range: {time_range}")

    n_grids = len(index_df)
    for i, row in enumerate(index_df.itertuples(), start=1):
        grid_dir = PROJECT_ROOT / row.folder
        grid_id = row.grid_id
        grid_out = OUT_DIR / grid_id
        grid_out.mkdir(parents=True, exist_ok=True)

        topo = build_topology(grid_dir, grid_id)
        (grid_out / "topology.json").write_text(json.dumps(topo))

        pmu_pen = build_pmu_penetration(grid_dir)
        (grid_out / "pmu_penetration.json").write_text(json.dumps(pmu_pen))

        meta = {
            "grid_id": grid_id,
            "folder": row.folder,
            "voltage_level": row.voltage_level,
            "category": row.category,
            "size_tier": row.size_tier,
            "n_bus": int(row.n_bus),
            "n_line": int(row.n_line),
            "n_trafo": int(row.n_trafo),
            "n_load": int(row.n_load),
            "source_grid_rel_path": row.source_grid_rel_path,
        }
        (grid_out / "meta.json").write_text(json.dumps(meta))

        print(f"[{i}/{n_grids}] built {grid_id} "
              f"(n_bus={row.n_bus}, n_line={row.n_line}, n_load={row.n_load})")

    print(f"\nDone. Output under {OUT_DIR}")


if __name__ == "__main__":
    main()
