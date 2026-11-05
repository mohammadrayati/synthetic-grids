"""Read the synthetic grids dataset (the data/ folder fetched by `make download-data`).

grid_id is a grid's grid_topology/ path with "/" replaced by "__",
e.g. "LV__Alps-Periurban__5238-11_1_3_grid".

    import synthetic_grids as sg
    tables = sg.topology("LV__Alps-Periurban__5238-11_1_3_grid", data_dir="path/to/data")
    loads = sg.load_power(grid_id, data_dir)        # one row per timestamp
    bus = sg.power_flow(grid_id, data_dir, "bus")    # one row per (timestamp, bus)

data_dir defaults to $SYNTHETIC_GRIDS_DATA.
"""
import os
from pathlib import Path

import pandas as pd

TOPOLOGY_TABLES = ("buses", "lines", "trafos", "ext_grid", "loads", "bus_geodata", "line_geodata",
                   "pmu_penetration_selection")


def _data_dir(data_dir):
    data_dir = data_dir or os.environ.get("SYNTHETIC_GRIDS_DATA")
    if not data_dir:
        raise ValueError("Pass data_dir or set SYNTHETIC_GRIDS_DATA to the dataset's data/ folder "
                         "(fetched with `make download-data` in the synthetic-grids repository)")
    data_dir = Path(data_dir).expanduser()
    if not (data_dir / "grid_topology").is_dir():
        raise FileNotFoundError(f"{data_dir.resolve()} is not the dataset's data/ folder (no grid_topology/ in it)")
    return data_dir


def grids(data_dir=None):
    """grid_topology/index.csv with a grid_id column."""
    index = pd.read_csv(_data_dir(data_dir) / "grid_topology" / "index.csv")
    index.insert(0, "grid_id", index.folder.str.replace("data/grid_topology/", "", regex=False).str.replace("/", "__"))
    return index


def topology(grid_id, data_dir=None):
    """{table name: DataFrame} for the grid's CSVs in grid_topology/."""
    folder = _data_dir(data_dir) / "grid_topology" / Path(*grid_id.split("__"))
    return {name: pd.read_csv(folder / f"{name}.csv") for name in TOPOLOGY_TABLES}


def load_power(grid_id, data_dir=None):
    """Load P/Q time series: index timestamp_utc (tz-naive UTC), columns load_<i>_p_mw / load_<i>_q_mvar."""
    df = pd.read_csv(_data_dir(data_dir) / "load_power" / Path(*grid_id.split("__")).with_suffix(".csv"))
    df["timestamp_utc"] = pd.to_datetime(df.timestamp_utc, utc=True).dt.tz_localize(None)
    return df.set_index("timestamp_utc")


def power_flow(grid_id, data_dir=None, element="bus", ten_s_stats=False):
    """Power-flow results for element bus, line or trafo (or load, 10-s statistics only).
    ten_s_stats: the 10-s power flow's 15-min window statistics (only some grids have them)."""
    folder = "power_flow_results_10s_stats" if ten_s_stats else "power_flow_results"
    return pd.read_parquet(_data_dir(data_dir) / folder / grid_id / f"{element}.parquet")


def line_catalogue(data_dir=None):
    """The cable catalogue (pandapower standard line types)."""
    return pd.read_csv(_data_dir(data_dir) / "pandapower_line_std_types.csv")
