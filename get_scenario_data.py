"""Get smart-meter + PMU measurement data for one grid/timestamp/noise-level/PMU-penetration
scenario. Run: python get_scenario_data.py --grid <grid_id> --timestamp <iso8601> --noise <0-5>
--pmu-penetration <0-30> [--out-dir DIR]

Writes smart_meter.csv (load, bus, p_mw, q_mvar, vm_pu) and pmu.csv (bus, vm_pu, va_degree,
is_feeder_root) to --out-dir (default: current directory).
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent / "data"


def grid_dir(grid_id: str) -> Path:
    return DATA / "grid_topology" / Path(*grid_id.split("__"))


def nearest(index: pd.Index, ts: pd.Timestamp):
    return index[np.argmin(np.abs(index - ts))]


def noise_std(cfg: pd.DataFrame, device: str, quantity: str, level: str) -> float:
    row = cfg[(cfg.device_type == device) & (cfg.quantity == quantity) & (cfg.noise_level == level)]
    return float(row.iloc[0].std_value) if len(row) else 0.0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--grid", required=True)
    p.add_argument("--timestamp", required=True)
    p.add_argument("--noise", required=True, help="e.g. 2%%")
    p.add_argument("--pmu-penetration", required=True, help="e.g. 10%%")
    p.add_argument("--out-dir", default=".")
    args = p.parse_args()

    gdir = grid_dir(args.grid)
    ts = pd.Timestamp(args.timestamp)
    cfg = pd.read_csv(DATA / "noise-model-config.csv")

    bus_df = pd.read_parquet(DATA / "power_flow_results" / args.grid / "bus.parquet")
    snap_ts = nearest(pd.Index(bus_df.timestamp_utc.unique()), ts)
    bus_snap = bus_df[bus_df.timestamp_utc == snap_ts].set_index("bus")

    lp = pd.read_csv(DATA / "load_power" / Path(*args.grid.split("__")).with_suffix(".csv"))
    lp["timestamp_utc"] = pd.to_datetime(lp.timestamp_utc, utc=True).dt.tz_localize(None)
    lp_ts = nearest(pd.Index(lp.timestamp_utc), ts)
    lp_row = lp[lp.timestamp_utc == lp_ts].iloc[0]

    loads = pd.read_csv(gdir / "loads.csv")
    sm_vm_std = noise_std(cfg, "smart_meter", "vm_pu", args.noise)
    sm_pq_std = noise_std(cfg, "smart_meter", "p_mw", args.noise)
    rows = []
    for _, ld in loads.iterrows():
        p_mw = lp_row[f"load_{ld.load}_p_mw"]
        q_mvar = lp_row[f"load_{ld.load}_q_mvar"]
        vm_pu = bus_snap.loc[ld.bus, "vm_pu"]
        s = np.hypot(p_mw, q_mvar)
        rows.append({
            "load": ld.load, "bus": ld.bus,
            "p_mw": p_mw + np.random.normal(0, s * sm_pq_std),
            "q_mvar": q_mvar + np.random.normal(0, s * sm_pq_std),
            "vm_pu": vm_pu + np.random.normal(0, vm_pu * sm_vm_std),
        })
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(Path(args.out_dir) / "smart_meter.csv", index=False)

    pmu_sel = pd.read_csv(gdir / "pmu_penetration_selection.csv")
    pmu_sel = pmu_sel[pmu_sel.penetration_level == args.pmu_penetration]
    pmu_vm_std = noise_std(cfg, "pmu", "vm_pu", args.noise)
    pmu_va_std = noise_std(cfg, "pmu", "va_degree", args.noise)
    prows = []
    for _, r in pmu_sel.iterrows():
        vm_pu = bus_snap.loc[r.bus, "vm_pu"]
        va_degree = bus_snap.loc[r.bus, "va_degree"]
        prows.append({
            "bus": r.bus,
            "vm_pu": vm_pu + np.random.normal(0, vm_pu * pmu_vm_std),
            "va_degree": va_degree + np.random.normal(0, pmu_va_std),
            "is_feeder_root": bool(r.is_feeder_root),
        })
    pd.DataFrame(prows).to_csv(Path(args.out_dir) / "pmu.csv", index=False)

    print(f"Wrote smart_meter.csv ({len(rows)} loads) and pmu.csv ({len(prows)} PMUs) to {args.out_dir}")


if __name__ == "__main__":
    main()
