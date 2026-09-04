# Synthetic Swiss Distribution Grids

35 synthetic Swiss MV/LV distribution grids with real load data and power-flow-derived ground
truth, for testing impedance/topology estimation methods against known ground truth.

## Where the data comes from

1. **Topology** — ETH Zurich "Swiss-PDGs" database.
   - Code/data: https://github.com/aeonetos/Swiss-PDGs
   - Info page: https://rre.ethz.ch/research/models-and-data/Swiss-PDGs-Database.html
   - 879 MV grids + 34,920 LV grids, all Switzerland. Built from real OpenStreetMap data, not
     hand-made.
   - We selected 35 grids from it (see `grid_topology/index.csv`).

2. **Load profiles** — ETH Zurich / EKZ smart-meter dataset.
   - Link: https://doi.org/10.5281/zenodo.21398768
   - Name: "Dataset on residential electricity load profiles in Switzerland".
   - 2,447 real meters, 15-min resolution, 2023-2024.
   - We assigned these real profiles to the loads of each of the 35 selected grids.

3. **Ground truth (power flow)** — computed by us, not downloaded.
   - Real AC power flow (pandapower), full 4 weeks, every 15 min, for each grid.
   - Gives voltage, current, and loading for the whole grid at every timestamp.
   - Noise (0-5%) and PMU-penetration (0-30%) are added on top, for testing measurement setups
     only — the ground truth itself is clean.

No topology or load shape is hand-designed — everything comes from real datasets or real
power-flow simulation. This folder is self-contained: its webpage code is only ever edited here,
and its `data/` is fetched via `make download-data` from a Google Drive archive built by the main
`synthetic-grids-claude-agents` project's `make shared-data-archive` (the same archive
`impedance-estimation/` uses — see that project's README for why).

## How to use it

```
make setup           # create .venv, install requirements
make download-data    # fetch data/ (~1.3G) from Google Drive
make webpage          # http://localhost:8811
```

Or get a concrete, noisy scenario's smart-meter + PMU readings (full 4-week time series) as CSV
files, server-free, with `get_scenario_data.py`:

```
python get_scenario_data.py --grid LV__Alps-Periurban__5238-11_1_3_grid \
  --noise 2% --pmu-penetration 10% --out-dir out/
```

`--grid` is a grid's `data/grid_topology/index.csv` `folder` column with `/` replaced by `__`
(e.g. `LV/Alps-Periurban/5238-11_1_3_grid` -> `LV__Alps-Periurban__5238-11_1_3_grid`) — see that
CSV for the full list of 35. One example per voltage level:

| voltage level | `--grid` |
| --- | --- |
| LV  | `LV__Alps-Periurban__5238-11_1_3_grid` |
| MV  | `MV__153_0_grid` |
| MV_LV | `MV_LV__ML_0_0_grid` |

## What's in `data/`

Plain CSV/Parquet only — no pandapower or any other special package needed to read any of it.

- `grid_topology/<voltage_level>/<category>/<grid_name>/` — per-grid topology and geodata
  (`buses.csv`, `lines.csv`, `trafos.csv`, `ext_grid.csv`, `loads.csv`, `bus_geodata.csv`,
  `line_geodata.csv`) and PMU-penetration bus assignments (`pmu_penetration_selection.csv`).
  `grid_topology/index.csv` lists all 35 grids.
- `power_flow_results/<grid_id>/` — full 4-week/15-min power-flow results (voltages, angles, line
  loadings/currents) — the actual ground truth, as Parquet.
- `load_power/<voltage_level>/[<category>/]<grid_name>.csv` — the real ETH-derived p_mw/q_mvar
  time series per load (power-flow input).
- `noise-model-config.csv` — the 6 noise levels x device x quantity reference table.
- `pandapower_line_std_types.csv` — the cable catalogue (not used by the webpage itself, carried
  for `impedance-estimation/`'s use, which reads the same archive).

## The webpage

Pick a grid (left), scrub the time slider (middle, over the full 4-week window, real OSM basemap),
toggle map layers (voltage, line loading, load-flow arrows, node type, PMU markers, PV/prosumer
loads — draggable colorbar ranges), and set a noise level / PMU-penetration scenario (right) to see
the resulting smart-meter and PMU measurement tables.
