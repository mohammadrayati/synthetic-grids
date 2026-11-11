"""A grid as a pandapower net, plus smart-meter style measurements from the dataset.
Needs the pandapower extra: pip install "synthetic-grids[pandapower] @ git+https://github.com/mohammadrayati/synthetic-grids.git"

    from synthetic_grids.pandapower_net import cable_catalogue, to_pandapower, measurements
"""
import networkx as nx
import numpy as np
import pandapower as pp

from . import line_catalogue, load_power, power_flow, topology


def cable_catalogue(data_dir=None, voltage_rating=None, max_parallel=1):
    """{std_type: [r_ohm_per_km, x_ohm_per_km]}, sorted by R, highest first.
    voltage_rating: keep only cables rated "LV", "MV" or "HV". max_parallel: also add N cables in
    parallel (named "Nx <std_type>", R/N and X/N) for N up to this; MV grids have up to 4."""
    df = line_catalogue(data_dir)
    if voltage_rating:
        df = df[df.voltage_rating == voltage_rating]
    cables = {f"{n}x {row.std_type}" if n > 1 else row.std_type: [row.r_ohm_per_km / n, row.x_ohm_per_km / n]
              for n in range(1, max_parallel + 1) for row in df.itertuples()}
    return dict(sorted(cables.items(), key=lambda c: -c[1][0]))


def merge_chains(lines, keep):
    """Replace every bus with exactly two lines and not in keep by one line through it: lengths,
    R, X and C add up (per km values become the length-weighted mean), max_i is the smaller."""
    lines = lines.assign(R=lines.r_ohm_per_km * lines.length_km / lines.parallel,
                         X=lines.x_ohm_per_km * lines.length_km / lines.parallel,
                         C=lines.c_nf_per_km * lines.length_km * lines.parallel,
                         I=lines.max_i_ka * lines.parallel).set_index("line")
    g = nx.MultiGraph()
    g.add_edges_from((r.from_bus, r.to_bus, i) for i, r in lines.iterrows())
    for bus in [b for b in g if g.degree(b) == 2 and b not in keep]:
        (_, a, la), (_, b, lb) = g.edges(bus, keys=True)
        merged = lines.loc[la, ["R", "X", "C", "length_km"]] + lines.loc[lb, ["R", "X", "C", "length_km"]]
        lines.loc[la, ["R", "X", "C", "length_km"]] = merged
        lines.loc[la, "I"] = min(lines.at[la, "I"], lines.at[lb, "I"])
        lines.loc[la, ["from_bus", "to_bus"]] = a, b
        lines = lines.drop(lb)
        g.remove_node(bus)
        g.add_edge(a, b, la)
    L = lines.length_km
    return lines.assign(r_ohm_per_km=lines.R / L, x_ohm_per_km=lines.X / L, c_nf_per_km=lines.C / L,
                        max_i_ka=lines.I, parallel=1).reset_index()


def to_pandapower(grid_id, data_dir=None, sn_mva=10.0, merge_unmetered_chains=False):
    """pandapower net of the grid below its transformer (the root bus gets the ext_grid), with
    buses renumbered 0..n-1 in BFS order from the root and lines oriented parent -> child.
    merge_unmetered_chains: merge each chain of buses without a load (degree 2, no smart meter)
    into one line, as in a degree-simplified network. Returns (net, old_bus_ids, load_ids, metered_buses):
    old_bus_ids[i] is the dataset's id of bus i, load_ids[k] the dataset's id of net.load row k,
    metered_buses the (new) ids of buses with a load, i.e. with a smart meter measuring |V|."""
    tables = topology(grid_id, data_dir)
    buses = tables["buses"].set_index("bus")
    lines, trafos, loads = tables["lines"], tables["trafos"], tables["loads"]
    if len(trafos) > 1:
        raise ValueError(f"{grid_id} has {len(trafos)} transformers; only single-feeder grids are supported")
    root = int(trafos.lv_bus.iloc[0]) if len(trafos) else int(tables["ext_grid"].bus.iloc[0])
    if merge_unmetered_chains:
        lines = merge_chains(lines, keep=set(loads.bus) | {root})

    g = nx.Graph()
    g.add_edges_from(zip(lines.from_bus, lines.to_bus))
    old_bus_ids = list(nx.bfs_tree(g, root))
    new = {b: i for i, b in enumerate(old_bus_ids)}

    net = pp.create_empty_network(sn_mva=sn_mva)
    for b in old_bus_ids:
        pp.create_bus(net, vn_kv=buses.at[b, "vn_kv"])
    pp.create_ext_grid(net, bus=0, vm_pu=1.0)
    for row in lines.itertuples():
        # BFS order: a parent always has a lower new id than its child
        a, b = sorted((new[row.from_bus], new[row.to_bus]))
        pp.create_line_from_parameters(
            net, a, b, row.length_km, row.r_ohm_per_km, row.x_ohm_per_km, row.c_nf_per_km,
            row.max_i_ka, name=row.name, parallel=row.parallel)
    for row in loads.itertuples():
        pp.create_load(net, bus=new[row.bus], p_mw=0.0, q_mvar=0.0)
    metered_buses = sorted({new[b] for b in loads.bus})
    return net, old_bus_ids, loads["load"].tolist(), metered_buses


def measurements(net, old_bus_ids, load_ids, grid_id, n_points, node_order=None, data_dir=None, rise_tol=None):
    """Run to_pandapower's net at n_points timestamps spread over the dataset's window, with each
    load set to its load_power value and the root voltage set to the dataset's own power-flow
    result. Returns (vm_pu, v_pu, i_pu, timestamps, max_dv), arrays of shape (n_points, n_bus) with
    columns in node_order (default: bus index order):
      vm_pu |V|; v_pu complex V; i_pu conj(S / S_base) / |V|, the bus's current with its angle
      relative to the bus voltage; max_dv the largest |V| difference to the dataset's power-flow
      results, as a check.

    rise_tol: use only timestamps where no bus is more than rise_tol above its parent, i.e. where
    |V| falls away from the root everywhere (PV export breaks that); None uses every timestamp."""
    lp = load_power(grid_id, data_dir)
    stored = power_flow(grid_id, data_dir, "bus").set_index(["timestamp_utc", "bus"])

    vm_wide = stored.vm_pu.unstack("bus")[old_bus_ids]
    parent = [old_bus_ids[a] for a in net.line.from_bus]
    child = [old_bus_ids[b] for b in net.line.to_bus]
    candidates = vm_wide.index.intersection(lp.index)
    if rise_tol is not None:
        falling = (vm_wide.loc[candidates, child].to_numpy() <= vm_wide.loc[candidates, parent].to_numpy() + rise_tol).all(axis=1)
        candidates = candidates[falling]
    if len(candidates) < n_points:
        raise ValueError(f"{grid_id}: only {len(candidates)} usable timestamps (|V| falling away from the root "
                         f"everywhere within {rise_tol} pu), {n_points} needed")
    timestamps = candidates[np.linspace(0, len(candidates) - 1, n_points).astype(int)]

    n_bus = len(net.bus)
    vm_out = np.zeros((n_points, n_bus))
    v_out = np.zeros((n_points, n_bus), dtype=complex)
    i_out = np.zeros((n_points, n_bus), dtype=complex)
    max_dv = 0.0
    for t, ts in enumerate(timestamps):
        net.load["p_mw"] = lp.loc[ts, [f"load_{k}_p_mw" for k in load_ids]].to_numpy()
        net.load["q_mvar"] = lp.loc[ts, [f"load_{k}_q_mvar" for k in load_ids]].to_numpy()
        root = stored.loc[(ts, old_bus_ids[0])]
        net.ext_grid.loc[0, ["vm_pu", "va_degree"]] = root.vm_pu, root.va_degree
        pp.runpp(net, calculate_voltage_angles=True)

        vm = net.res_bus.vm_pu.to_numpy()
        max_dv = max(max_dv, float(np.abs(vm - vm_wide.loc[ts].to_numpy()).max()))
        vm_out[t] = vm
        v_out[t] = vm * np.exp(1j * np.deg2rad(net.res_bus.va_degree.to_numpy()))
        s_pu = (net.res_bus.p_mw.to_numpy() + 1j * net.res_bus.q_mvar.to_numpy()) / net.sn_mva
        i_out[t] = np.conj(s_pu) / vm

    order = [net.bus.index.get_loc(n) for n in (net.bus.index if node_order is None else node_order)]
    return vm_out[:, order], v_out[:, order], i_out[:, order], timestamps, max_dv
