# Synthesized Swiss Distribution Grids — Shareable Dataset

## What this is

35 synthetic Swiss MV/LV distribution grids with real load data and power-flow-derived ground
truth, built to compare two impedance/topology estimation methods (SUPSI's catalogue-prior method
and a regularization-based method) against known ground truth. Each grid has real topology, real
consumption data assigned to its loads, a full 4-week/15-minute power-flow simulation, a
configurable measurement noise model, and PMU-penetration scenarios.

## Sources

- **Topology**: ETH Zurich's "Swiss-PDGs" dataset — synthetic MV/LV grids with real OSM-embedded
  geometry (bus/line coordinates), pandapower-native, cables assigned from the pandapower standard
  line-type catalogue.
- **Load profiles**: ETH Zurich's real residential smart-meter dataset ("Dataset on residential
  electricity load profiles in Switzerland", Zenodo, CC-BY-4.0) — 2,447 installations, 15-minute
  resolution, 2023-2024. Real meters are assigned to synthetic loads (multiple meters aggregated
  per load for MV-level loads) to produce plausible time-varying consumption.

## Approach

Real Swiss-PDGs topology + real ETH meter data assigned to every load + a full 4-week/15-minute
pandapower power flow run per grid (the ground truth) + a configurable Gaussian noise model
(0-5%, smart-meter and PMU) + PMU-penetration scenarios (0-30% of buses, nested/cumulative,
including the feeder-root bus). No topology is hand-designed or hard-coded — grids, cables, and
load time series all come from real datasets or real power-flow simulation.

## What's in `data/`

- **`organized/<voltage_level>/<category>/<grid_name>/`** — one folder per grid (35 total):
  topology (`buses.csv`, `lines.csv`, `trafos.csv`, `ext_grid.csv`, `loads.csv`,
  `bus_geodata.csv`, `line_geodata.csv`), PMU-penetration bus assignments
  (`pmu_penetration_selection.csv`), and provenance of which real ETH meters were assigned to
  which synthetic load (`eth_load_assignment.csv`, `eth_load_meter_contributions.csv`).
  `organized/index.csv` lists all 35 grids with summary stats.
- **`ground-truth-full/<grid_id>/`** — the full 4-week/15-minute pandapower power-flow results
  (`bus.parquet`, `line.parquet`, `trafo.parquet`): voltages, angles, line loadings/currents, for
  every timestamp. This is the actual ground truth the webpage and any estimation method compare
  against.
- **`load-profiles/<voltage_level>/[<category>/]<grid_name>.csv`** — the real 4-week/15-minute
  ETH-derived p_mw/q_mvar time series for every load in each grid (input to the power flow above).
- **`noise-model-config.csv`** — the 6 noise levels (0-5%) x device (`smart_meter`/`pmu`) x
  quantity reference table used to add measurement noise on top of the clean ground truth.

See `load_example.py` for the actual column shapes and how the files link together — it's a
better reference than a written data dictionary.

**Note**: within the main `synthesized-grids` project, everything under `data/` here (plus
`webpage/static/`) is a symlink into the main project's own `data/`/`code/webpage/static/`, so
there's never a second physical copy of the same bytes on disk. This is transparent to everything
above — reads work exactly the same either way. It only matters if you're handing `share/` off on
its own (e.g. to SUPSI, or uploading to Zenodo): see "Exporting a standalone copy" below, since
symlinks don't survive being copied out of the main project's directory tree as-is.

## Running the webpage

```
pip install -r requirements.txt
./run_server.sh
```

Then open `http://localhost:8811/` in a browser.

## How the webpage works

Three columns:

- **Left**: pick one of the 35 grids (grouped by voltage level/category, with a text filter), and
  below it, "Map layers" — six independent on/off toggles, all on by default:
  - **Voltage colors** — bus fill colored by `vm_pu` at the current timestamp, diverging
    blue-green-red, default range 0.95-1.05 pu.
  - **Line loading / current colors** — line stroke colored by `loading_percent`, same diverging
    scheme, default range 0-100%.
  - **Load-flow arrows** — small arrows along each line, pointing from the feeder root toward the
    leaves (direction is fixed by the grid's radial topology — none of these grids have local
    generation, so power only ever flows outward), sized by that line's loading.
  - **Node type** — a colored ring around each bus (red = feeder root, orange = transformer, blue =
    regular) plus a size bump for non-regular buses.
  - **PMU bus markers** — purple diamonds at whichever buses carry a PMU under the selected
    penetration level.
  - **PV / prosumer loads** — amber stars at loads whose real ETH profile goes net-negative
    (exports power) at some point in the 4-week window — a genuine, if small, subset of the real
    consumption data, not a modeled generator. Sized by how often that load exports; hover for the
    exact export frequency and peak export magnitude. This layer doesn't change with the time
    slider — it's a whole-window summary, not a per-moment reading.

  For the two color layers, the little colorbar under the checkboxes has draggable handles — drag
  to narrow the highlighted value range (can't be widened past the 0.95-1.05 / 0-100% defaults).
  There's also a "Marker / line size" slider if the default sizes are hard to read on a given grid.

- **Middle**: a time slider over the full 4-week/15-minute window (2,688 steps), and the map itself
  — a real OpenStreetMap basemap (so cable routing shows in its actual geographic context — real
  street/village layout, not just abstract coordinates) with the grid drawn on top. Scroll to zoom,
  drag to pan, double-click to reset to the grid's own extent. The bottom edge of the map box is a
  drag handle if you want it taller.

- **Right**: "Scenario" — a noise level (0-5%, applied client-side, same value always reappears for
  the same grid/quantity/timestamp/level if you revisit it) and a PMU-penetration level (0-30% of
  buses, nested/cumulative, always including the feeder-root bus) — plus two tables: per-load
  smart-meter readings (magnitude only, matching a real sensor's constraint) and per-PMU readings
  (magnitude + phase) at the current timestamp/noise/penetration combination.

All three columns can be resized by dragging the thin bars between them.

The map reads live from `data/ground-truth-full/` and `data/organized/` (topology, PMU sets, the
prosumer-load flags) on every timestamp/grid change; the noise model reads `data/noise-model-config.csv`
and is computed in the browser, not precomputed.

## Loading data directly in Python

See `load_example.py` — a short, flat script (no server needed) showing how to read a specific
grid's ground truth at a specific timestamp, its PMU set at a specific penetration level, apply
the noise model, and read its load profiles, directly from the files under `data/`.

## Exporting a standalone copy

`share/`'s `data/` and `webpage/static/` are symlinks into the main `synthesized-grids` project
(see the note above) — fine for working inside that project, but symlinks are broken/meaningless
once `share/` is copied out on its own (e.g. to send to SUPSI, or to upload to Zenodo). To produce
a real, self-contained archive with the symlinks dereferenced into actual files:

```
make share-export
```

run from the main project's root (this is a Makefile target there, not inside `share/` itself). It
writes `share-export.tar.gz` to the project root — a tar archive with every symlink replaced by the
real file/directory it points to, safe to extract and hand off anywhere.
