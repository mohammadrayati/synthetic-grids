"""Get the full time series of smart-meter + PMU measurements for one grid/noise-level/
PMU-penetration scenario. Run: python get_scenario_data.py --grid <grid_id> --noise <0-5>
--pmu-penetration <0-30> [--out-dir DIR]

Writes, for the grid's full 4-week/15-min window, to --out-dir (default: current directory):
  smart_meter.csv (timestamp_utc, load, bus, p_mw, q_mvar, vm_pu)
  pmu.csv         (timestamp_utc, bus, vm_pu, va_degree, is_feeder_root)
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent / "data"


def grid_dir(grid_id: str) -> Path:
    return DATA / "grid_topology" / Path(*grid_id.split("__"))


def noise_std(cfg: pd.DataFrame, device: str, quantity: str, level: str) -> float:
    row = cfg[(cfg.device_type == device) & (cfg.quantity == quantity) & (cfg.noise_level == level)]
    return float(row.iloc[0].std_value) if len(row) else 0.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grid", required=True)
    p.add_argument("--noise", required=True, help="e.g. 2%%")
    p.add_argument("--pmu-penetration", required=True, help="e.g. 10%%")
    p.add_argument("--out-dir", default="data")
    args = p.parse_args()

    gdir = grid_dir(args.grid)
    cfg = pd.read_csv(DATA / "noise-model-config.csv")
    bus_df = pd.read_parquet(DATA / "power_flow_results" / args.grid / "bus.parquet")

    lp = pd.read_csv(DATA / "load_power" / Path(*args.grid.split("__")).with_suffix(".csv"))
    lp["timestamp_utc"] = pd.to_datetime(lp.timestamp_utc, utc=True).dt.tz_localize(None)
    loads = pd.read_csv(gdir / "loads.csv")[["load", "bus"]]

    load_ids = loads["load"].to_numpy()
    p_arr = lp[[f"load_{i}_p_mw" for i in load_ids]].to_numpy()
    q_arr = lp[[f"load_{i}_q_mvar" for i in load_ids]].to_numpy()
    n_ts, n_load = p_arr.shape
    sm = pd.DataFrame({
        "timestamp_utc": np.repeat(lp["timestamp_utc"].to_numpy(), n_load),
        "load": np.tile(load_ids, n_ts),
        "bus": np.tile(loads["bus"].to_numpy(), n_ts),
        "p_mw": p_arr.reshape(-1),
        "q_mvar": q_arr.reshape(-1),
    })
    sm = sm.merge(bus_df[["timestamp_utc", "bus", "vm_pu"]], on=["timestamp_utc", "bus"], how="left")

    sm_vm_std = noise_std(cfg, "smart_meter", "vm_pu", args.noise)
    sm_p_std = noise_std(cfg, "smart_meter", "p_mw", args.noise)
    sm_q_std = noise_std(cfg, "smart_meter", "q_mvar", args.noise)
    s = np.hypot(sm["p_mw"], sm["q_mvar"])
    sm["p_mw"] += np.random.normal(0, s * sm_p_std)
    sm["q_mvar"] += np.random.normal(0, s * sm_q_std)
    sm["vm_pu"] += np.random.normal(0, sm["vm_pu"] * sm_vm_std)

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    sm_cols = ["timestamp_utc", "load", "bus", "p_mw", "q_mvar", "vm_pu"]
    sm.sort_values(["timestamp_utc", "load"])[sm_cols].to_csv(Path(args.out_dir) / "smart_meter.csv", index=False)

    pmu_sel = pd.read_csv(gdir / "pmu_penetration_selection.csv")
    pmu_sel = pmu_sel[pmu_sel.penetration_level == args.pmu_penetration][["bus", "is_feeder_root"]]
    pmu_vm_std = noise_std(cfg, "pmu", "vm_pu", args.noise)
    pmu_va_std = noise_std(cfg, "pmu", "va_degree", args.noise)

    pmu = bus_df.merge(pmu_sel, on="bus")
    pmu["vm_pu"] += np.random.normal(0, pmu["vm_pu"] * pmu_vm_std)
    pmu["va_degree"] += np.random.normal(0, pmu_va_std, size=len(pmu))

    pmu_cols = ["timestamp_utc", "bus", "vm_pu", "va_degree", "is_feeder_root"]
    pmu.sort_values(["timestamp_utc", "bus"])[pmu_cols].to_csv(Path(args.out_dir) / "pmu.csv", index=False)

    print(f"Wrote smart_meter.csv ({len(sm)} rows, {n_load} loads x {n_ts} timestamps) and "
          f"pmu.csv ({len(pmu)} rows) to {args.out_dir}")


if __name__ == "__main__":
    main()
