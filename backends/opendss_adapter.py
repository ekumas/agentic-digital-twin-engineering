"""
Proof-of-concept realisation backend: CIM TwinRepresentation -> OpenDSS.

NOTE (paper §3.2): the twin is the CIM representation; this adapter merely
RENDERS it into an executable form. Swapping this file for a GridLAB-D adapter
would not change the framework, its agents, or its claims.
"""
from core.twin import TwinRepresentation


CIM_TO_OPENDSS = {
    "ACLineSegment": "Line",
    "PerLengthPhaseImpedance": "LineCode",
    "EnergyConsumer": "Load",
    "PowerTransformer": "Transformer",
    "LinearShuntCompensator": "Capacitor",
    "PowerElectronicsConnection": "PVSystem",
    "EnergySource": "Vsource",
    "LoadShape": "LoadShape",
}


def _phase_suffix(phases: str) -> str:
    return "." + ".".join({"A": "1", "B": "2", "C": "3"}[p] for p in phases)


def _tri(vals):
    """Render a lower-triangular matrix list as OpenDSS '(a | b c | d e f)'."""
    out, i, row = [], 0, 1
    while i < len(vals):
        out.append(" ".join(f"{v:.10g}" for v in vals[i:i + row]))
        i += row
        row += 1
    return "(" + " | ".join(out) + ")"


def render(twin: TwinRepresentation, mode: str = "snap") -> str:
    """Return a runnable OpenDSS deck (Master.dss content).

    mode="snap"  : snapshot solve at nominal loading (reference-feeder
                   experiments E-CRL, ablation, E2, E3).
    mode="daily" : first step of a daily time-series solve, with load shapes
                   applied (SMART-DS round trip, E1).
    """
    L = []
    L.append(f"Clear")
    L.append(f"New Circuit.{twin.name}")

    src = twin.source
    if src.r1 is not None and src.x1 is not None:
        # exact Thevenin impedance (preferred — required for 1e-6 round-trip)
        L.append(
            f"Edit Vsource.Source bus1={src.node} basekv={src.nominal_kv} "
            f"pu={src.pu} phases=3 "
            f"R1={src.r1:.10g} X1={src.x1:.10g} "
            f"R0={src.r0 if src.r0 is not None else src.r1:.10g} "
            f"X0={src.x0 if src.x0 is not None else src.x1:.10g}"
        )
    else:
        L.append(
            f"Edit Vsource.Source bus1={src.node} basekv={src.nominal_kv} "
            f"pu={src.pu} phases=3 Isc3={src.isc3_a}"
        )

    for ls in twin.load_shapes.values():
        pts = ",".join(f"{v:.5f}" for v in ls.values)
        L.append(
            f"New LoadShape.{ls.mrid} npts={len(ls.values)} "
            f"interval={ls.interval_hours} mult=({pts})"
        )

    for z in twin.impedances.values():
        if z.is_matrix_form:
            # CIM PhaseImpedanceData -> OpenDSS matrix LineCode
            s = (f"New LineCode.{z.mrid} nphases={z.n_phases} "
                 f"units={z.length_units} "
                 f"rmatrix={_tri(z.rmatrix)} xmatrix={_tri(z.xmatrix)}")
            if z.cmatrix:
                s += f" cmatrix={_tri(z.cmatrix)}"
            L.append(s)
        else:
            nph = z.n_phases if z.n_phases else 3
            L.append(
                f"New LineCode.{z.mrid} nphases={nph} units={z.length_units} "
                f"R1={z.r1_ohm_per_km} X1={z.x1_ohm_per_km} "
                f"R0={z.r0_ohm_per_km} X0={z.x0_ohm_per_km}"
            )

    for tr in twin.transformers.values():
        kva = tr.rated_mva * 1000
        s = (f"New Transformer.{tr.mrid} phases={tr.phases} windings=2 "
             f"XHL={tr.reactance_pct}")
        if tr.loadloss_pct is not None:
            s += f" %loadloss={tr.loadloss_pct}"
        else:
            s += f" %Rs={tr.resistance_pct}"
        if tr.noloadloss_pct is not None:
            s += f" %noloadloss={tr.noloadloss_pct}"
        s += (f" wdg=1 bus={tr.node_hv} conn={tr.conn_hv} kv={tr.hv_kv} "
              f"kva={kva} tap={tr.tap_hv}")
        s += (f" wdg=2 bus={tr.node_lv} conn={tr.conn_lv} kv={tr.lv_kv} "
              f"kva={kva} tap={tr.tap_lv}")
        if tr.is_regulator:
            s += f" numtaps={tr.num_taps} maxtap={tr.max_tap} mintap={tr.min_tap}"
        L.append(s)

    for mw in twin.mwtransformers.values():
        buses = ", ".join(
            f"{w['bus']}" + (f".{w['conn_str']}" if w['conn_str'] else "")
            for w in mw.windings)
        conns = ", ".join(w["conn"] for w in mw.windings)
        kvs = ", ".join(str(w["kv"]) for w in mw.windings)
        kvas = ", ".join(str(w["kva"]) for w in mw.windings)
        taps = ", ".join(str(w["tap"]) for w in mw.windings)
        s = (f"New Transformer.{mw.mrid} phases={mw.phases} "
             f"windings={len(mw.windings)} "
             f"buses=({buses}) conns=({conns}) kvs=({kvs}) kvas=({kvas}) "
             f"taps=({taps}) xhl={mw.xhl}")
        if mw.xht is not None:
            s += f" xht={mw.xht}"
        if mw.xlt is not None:
            s += f" xlt={mw.xlt}"
        if mw.rs_pct:
            # per-winding resistances reproduce the loss split exactly;
            # preferred over the aggregate %loadloss
            s += " %Rs=(" + ", ".join(str(r) for r in mw.rs_pct) + ")"
        elif mw.loadloss_pct is not None:
            s += f" %loadloss={mw.loadloss_pct}"
        if mw.noloadloss_pct is not None:
            s += f" %noloadloss={mw.noloadloss_pct}"
        L.append(s)

    for ln in twin.lines.values():
        ca = ln.conn_a if ln.conn_a else _phase_suffix(ln.phases)
        cb = ln.conn_b if ln.conn_b else _phase_suffix(ln.phases)
        if ln.impedance_mrid == "__switch__":
            # closed switch — zero-impedance connection, as in the source model
            L.append(
                f"New Line.{ln.mrid} bus1={ln.node_a}{ca} bus2={ln.node_b}{cb} "
                f"switch=yes phases={len(ln.phases)}"
            )
        else:
            L.append(
                f"New Line.{ln.mrid} bus1={ln.node_a}{ca} bus2={ln.node_b}{cb} "
                f"linecode={ln.impedance_mrid} length={ln.length_km} "
                f"units={ln.length_units} phases={len(ln.phases)}"
            )

    for ld in twin.loads.values():
        kv = ld.kv if ld.kv is not None else twin.nodes[ld.node].base_voltage_kv
        conn = ld.conn if ld.conn else _phase_suffix(ld.phases)
        nph = len(ld.phases)
        shape = f" daily={ld.load_shape_mrid}" if ld.load_shape_mrid else ""
        L.append(
            f"New Load.{ld.mrid} bus1={ld.node}{conn} phases={nph} "
            f"conn={'delta' if ld.is_delta else 'wye'} "
            f"kV={kv} kW={ld.p_kw} kvar={ld.q_kvar} model=1{shape}"
        )

    for pv in twin.pv.values():
        node = twin.nodes[pv.node]
        shape = f" daily={pv.load_shape_mrid}" if pv.load_shape_mrid else ""
        L.append(
            f"New PVSystem.{pv.mrid} bus1={pv.node} phases=3 "
            f"kV={node.base_voltage_kv} kVA={pv.rated_kw} Pmpp={pv.rated_kw} "
            f"irradiance=1{shape}"
        )

    for cap in twin.capacitors.values():
        kv = cap.kv if cap.kv is not None else twin.nodes[cap.node].base_voltage_kv
        conn = cap.conn if cap.conn else _phase_suffix(cap.phases)
        L.append(
            f"New Capacitor.{cap.mrid} bus1={cap.node}{conn} "
            f"phases={len(cap.phases)} kV={kv} kvar={cap.kvar}"
        )

    for rg in twin.regulators.values():
        s = (f"New RegControl.{rg.mrid} transformer={rg.transformer_mrid} "
             f"winding={rg.winding} vreg={rg.target_v} band={rg.band} "
             f"ptratio={rg.pt_ratio} delay={rg.delay}")
        if rg.ct_primary:
            s += f" CTprim={rg.ct_primary}"
        if rg.r or rg.x:
            s += f" R={rg.r} X={rg.x}"
        L.append(s)

    # Build the voltage-base set from every kV level actually present in the
    # model, rather than a fixed list. Missing bases (notably the 0.12/0.24 kV
    # split-phase service secondaries) cause OpenDSS to snap LV buses to the
    # nearest listed base — e.g. mis-assigning 120 V nodes a 0.277 kV base
    # (480/sqrt3) — which corrupts their per-unit voltages while leaving the
    # actual volts correct. Collecting the real bases fixes this at the source.
    bases = set()
    if src.nominal_kv:
        bases.add(round(src.nominal_kv, 5))
    for tr in twin.transformers.values():
        if getattr(tr, "hv_kv", None):
            bases.add(round(tr.hv_kv, 5))
        if getattr(tr, "lv_kv", None):
            bases.add(round(tr.lv_kv, 5))
        # split-phase service transformers expose a line-to-line LV kv; the
        # line-to-neutral base (kv/2 for centre-tapped 240/120) must also be
        # present so 120 V nodes resolve correctly
        lv = getattr(tr, "lv_kv", None)
        if lv and lv < 0.5:
            bases.add(round(lv / 2.0, 5))
    for nd in twin.nodes.values():
        bv = getattr(nd, "base_voltage_kv", None)
        if bv:
            bases.add(round(bv, 5))
    if not bases:
        bases = {src.nominal_kv or 12.47, 12.47, 0.48}
    base_list = ", ".join(f"{b:g}" for b in sorted(bases))
    L.append(f"Set voltagebases=[{base_list}]")
    L.append("Calcvoltagebases")
    if mode == "daily":
        L.append("Set mode=daily")
        L.append("Set number=1")
        L.append("Set stepsize=1h")
    else:
        L.append("Set mode=snap")
    return "\n".join(L)

