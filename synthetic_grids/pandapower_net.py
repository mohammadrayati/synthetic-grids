"""A grid as a pandapower net, plus smart-meter style measurements from the dataset.
Needs the pandapower extra: pip install "synthetic-grids[pandapower] @ git+https://github.com/mohammadrayati/synthetic-grids.git"

    from synthetic_grids.pandapower_net import cable_catalogue, to_pandapower, measurements
"""
import networkx as nx
import numpy as np
import pandapower as pp

from . import line_catalogue, load_power, power_flow, topology


def cable_catalogue(data_dir=None):
    """{std_type: [r_ohm_per_km, x_ohm_per_km]}, sorted by R, highest first."""
    df = line_catalogue(data_dir).sort_values("r_ohm_per_km", ascending=False)
    return {row.std_type: [row.r_ohm_per_km, row.x_ohm_per_km] for row in df.itertuples()}


def to_pandapower(grid_id, data_dir=None, sn_mva=10.0):
    """pandapower net of the grid below its transformer (the root bus gets the ext_grid), with
    buses renumbered 0..n-1 in BFS order from the root and lines oriented parent -> child. Returns (net, old_bus_ids, load_ids, metered_buses):
    old_bus_ids[i] is the dataset's id of bus i, load_ids[k] the dataset's id of net.load row k,
    metered_buses the (new) ids of buses with a load, i.e. with a smart meter measuring |V|."""
    tables = topology(grid_id, data_dir)
    buses = tables["buses"].set_index("bus")
    lines, trafos, loads = tables["lines"], tables["trafos"], tables["loads"]
    if len(trafos) > 1:
        raise ValueError(f"{grid_id} has {len(trafos)} transformers; only single-feeder grids are supported")
    root = int(trafos.lv_bus.iloc[0]) if len(trafos) else int(tables["ext_grid"].bus.iloc[0])

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


def measurements(net, old_bus_ids, load_ids, grid_id, n_points, node_order=None, data_dir=None):
    """Run to_pandapower's net at n_points timestamps spread over the dataset's window, with each
    load set to its load_power value and the root voltage set to the dataset's own power-flow
    result. Returns (vm_pu, v_pu, i_pu, timestamps, max_dv), arrays of shape (n_points, n_bus) with
    columns in node_order (default: bus index order):
      vm_pu |V|; v_pu complex V; i_pu conj(S / S_base) / |V|, the bus's current with its angle
      relative to the bus voltage; max_dv the largest |V| difference to the dataset's power-flow
      results, as a check.

    Only timestamps where no bus is above its parent are used, i.e. where |V| falls away from
    the root everywhere (PV export breaks that)."""
    lp = load_power(grid_id, data_dir)
    stored = power_flow(grid_id, data_dir, "bus").set_index(["timestamp_utc", "bus"])

    vm_wide = stored.vm_pu.unstack("bus")[old_bus_ids]
    parent = [old_bus_ids[a] for a in net.line.from_bus]
    child = [old_bus_ids[b] for b in net.line.to_bus]
    falling = (vm_wide[child].to_numpy() <= vm_wide[parent].to_numpy()).all(axis=1)
    candidates = vm_wide.index[falling].intersection(lp.index)
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
        vm_out[t] = vm
        v_out[t] = vm * np.exp(1j * np.deg2rad(net.res_bus.va_degree.to_numpy()))
        s_pu = (net.res_bus.p_mw.to_numpy() + 1j * net.res_bus.q_mvar.to_numpy()) / net.sn_mva
        i_out[t] = np.conj(s_pu) / vm
        max_dv = max(max_dv, float(np.abs(vm - vm_wide.loc[ts].to_numpy()).max()))

    order = [net.bus.index.get_loc(n) for n in (net.bus.index if node_order is None else node_order)]
    return vm_out[:, order], v_out[:, order], i_out[:, order], timestamps, max_dv
