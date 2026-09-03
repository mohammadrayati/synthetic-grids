"""Minimal example: load ground-truth, PMU-penetration and load-profile data for one
grid/timestamp/noise-level/PMU-penetration combination, directly from share/data/, no server."""
import numpy as np
import pandas as pd
from pathlib import Path

GRID_ID = "LV__Alps-Periurban__5238-11_1_3_grid"
TIMESTAMP = "2024-03-10T08:00:00"
NOISE_LEVEL = "2%"
PMU_PENETRATION = "10%"

DATA_DIR = Path(__file__).resolve().parent / "data"
target_ts = pd.Timestamp(TIMESTAMP)

# --- ground-truth power flow: bus voltages and line loadings at the nearest timestamp ---
bus_df = pd.read_parquet(DATA_DIR / "ground-truth-full" / GRID_ID / "bus.parquet")
line_df = pd.read_parquet(DATA_DIR / "ground-truth-full" / GRID_ID / "line.parquet")

unique_ts = bus_df["timestamp_utc"].unique()
nearest_ts = unique_ts[np.argmin(np.abs(unique_ts - np.datetime64(target_ts)))]
print(f"Nearest available timestamp to {TIMESTAMP}: {nearest_ts}")

bus_snap = bus_df[bus_df["timestamp_utc"] == nearest_ts].sort_values("bus")
line_snap = line_df[line_df["timestamp_utc"] == nearest_ts].sort_values("line")
print("\nBus voltages (pu), first 5 buses:")
print(bus_snap[["bus", "vm_pu", "va_degree"]].head())
print("\nLine loadings (%), first 5 lines:")
print(line_snap[["line", "loading_percent"]].head())

# --- PMU-penetration selection: which buses carry a PMU at this level ---
grid_folder = DATA_DIR / "organized" / Path(*GRID_ID.split("__"))
pmu_sel = pd.read_csv(grid_folder / "pmu_penetration_selection.csv")
pmu_at_level = pmu_sel[pmu_sel["penetration_level"] == PMU_PENETRATION]
print(f"\nPMUs at {PMU_PENETRATION} penetration: {sorted(pmu_at_level['bus'].tolist())}")

# --- noise model: apply Gaussian noise to bus voltages per noise-model-config.csv ---
noise_cfg = pd.read_csv(DATA_DIR / "noise-model-config.csv")
row = noise_cfg[(noise_cfg["device_type"] == "smart_meter") &
                 (noise_cfg["quantity"] == "vm_pu") &
                 (noise_cfg["noise_level"] == NOISE_LEVEL)].iloc[0]
std = bus_snap["vm_pu"].to_numpy() * row["std_value"]  # relative_to_value basis
noisy_vm_pu = bus_snap["vm_pu"].to_numpy() + np.random.normal(0, std)
print(f"\nClean vs noisy vm_pu at noise level {NOISE_LEVEL} (std_value={row['std_value']}), first 5 buses:")
print(pd.DataFrame({"bus": bus_snap["bus"].to_numpy()[:5],
                     "clean_vm_pu": bus_snap["vm_pu"].to_numpy()[:5],
                     "noisy_vm_pu": noisy_vm_pu[:5]}))

# --- load profiles: real ETH-derived p_mw/q_mvar at the nearest timestamp ---
lp_path = DATA_DIR / "load-profiles" / Path(*GRID_ID.split("__")).with_suffix(".csv")
lp_df = pd.read_csv(lp_path)
lp_df["timestamp_utc"] = pd.to_datetime(lp_df["timestamp_utc"], utc=True).dt.tz_localize(None)
lp_unique_ts = lp_df["timestamp_utc"].unique()
lp_nearest_ts = lp_unique_ts[np.argmin(np.abs(lp_unique_ts - np.datetime64(target_ts)))]
lp_snap = lp_df[lp_df["timestamp_utc"] == lp_nearest_ts].iloc[0]
print(f"\nLoad profiles at nearest timestamp {lp_nearest_ts}, first 3 loads:")
for load_id in [0, 1, 2]:
    p = lp_snap.get(f"load_{load_id}_p_mw")
    q = lp_snap.get(f"load_{load_id}_q_mvar")
    print(f"  load {load_id}: p_mw={p}, q_mvar={q}")
