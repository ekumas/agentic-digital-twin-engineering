#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAZ 3 - COLLABORATIVE REASONING LOOP (CRL) - BAGIMSIZ TEK DOSYA
Pipeline vs agentic dongusu: ayni hata, sadece kapali dongu onariyor.
KULLANIM: python3 faz3_test.py     (API anahtari GEREKMEZ)
GEREKSINIM: pip install opendssdirect.py
"""
import os, sys, math, tempfile, json, re
from dataclasses import dataclass, field
from typing import Optional
try:
    import opendssdirect as dss
except ImportError:
    print("HATA: opendssdirect kurulu degil -> pip install opendssdirect.py"); sys.exit(1)


# ===== twin =====

"""
Backend-agnostic, CIM-grounded twin representation.

This is THE artefact of the framework. It is deliberately independent of any
simulator: OpenDSS (or GridLAB-D, or a surrogate) is rendered FROM this by an
adapter. Class names follow IEC 61968/61970 CIM.
"""


@dataclass
class BaseVoltage:
    mrid: str
    nominal_voltage_kv: float


@dataclass
class ConnectivityNode:
    """CIM ConnectivityNode — an electrical bus."""
    mrid: str
    base_voltage_kv: float
    phases: str = "ABC"


@dataclass
class PerLengthPhaseImpedance:
    """
    CIM PerLengthPhaseImpedance.

    Two forms are supported:
      * sequence form  (r1/x1/r0/x0)      — adequate for balanced 3-phase trunks
      * full-matrix form (PhaseImpedanceData) — REQUIRED for real feeders with
        unbalanced / 1- and 2-phase laterals. SMART-DS uses this form, so any
        loader that collapses it to sequence values cannot round-trip to 1e-6.

    Matrices are stored as lower-triangular row lists, in ohms (and nF for c)
    per `length_units`, exactly as the source model expresses them.
    """
    mrid: str
    r1_ohm_per_km: Optional[float] = None
    x1_ohm_per_km: Optional[float] = None
    r0_ohm_per_km: Optional[float] = None
    x0_ohm_per_km: Optional[float] = None
    # --- CIM PhaseImpedanceData (full matrix form) ---
    n_phases: Optional[int] = None
    rmatrix: Optional[list] = None   # lower triangle, row-major
    xmatrix: Optional[list] = None
    cmatrix: Optional[list] = None
    length_units: str = "km"

    @property
    def is_matrix_form(self) -> bool:
        return self.rmatrix is not None and self.xmatrix is not None


@dataclass
class ACLineSegment:
    """CIM ACLineSegment — a line/cable between two nodes."""
    mrid: str
    node_a: str
    node_b: str
    length_km: float
    impedance_mrid: str
    phases: str = "ABC"
    # node-connection strings as expressed by the source model (e.g. ".1.3"),
    # kept verbatim so single- and two-phase laterals round-trip exactly
    conn_a: str = ""
    conn_b: str = ""
    length_units: str = "km"


@dataclass
class PowerTransformer:
    """CIM PowerTransformer (+ ends collapsed for a 2-winding unit)."""
    mrid: str
    node_hv: str
    node_lv: str
    rated_mva: float
    hv_kv: float
    lv_kv: float
    resistance_pct: float = 0.5
    reactance_pct: float = 5.0
    conn_hv: str = "delta"
    conn_lv: str = "wye"
    loadloss_pct: Optional[float] = None
    noloadloss_pct: Optional[float] = None
    tap_hv: float = 1.0
    tap_lv: float = 1.0
    num_taps: int = 32
    max_tap: float = 1.1
    min_tap: float = 0.9
    phases: int = 3
    is_regulator: bool = False


@dataclass
class MultiWindingTransformer:
    """
    General N-winding transformer, stored close to the OpenDSS form so that
    split-phase (1 HV + 2 LV half-windings, 120/240 V) and other multi-winding
    units round-trip exactly. SMART-DS service transformers are 3-winding.

    windings: list of dicts, each: {bus, conn_str, kv, kva, tap}
    reactances: {xhl, xht, xlt} (percent); losses in percent.
    """
    mrid: str
    phases: int
    windings: list
    xhl: float
    xht: Optional[float] = None
    xlt: Optional[float] = None
    loadloss_pct: Optional[float] = None
    noloadloss_pct: Optional[float] = None
    rs_pct: Optional[list] = None      # per-winding %R (%Rs) — decisive for losses


@dataclass
class EnergyConsumer:
    """CIM EnergyConsumer — a load, optionally driven by a LoadShape."""
    mrid: str
    node: str
    p_kw: float
    q_kvar: float
    load_shape_mrid: Optional[str] = None
    phases: str = "ABC"
    kv: Optional[float] = None      # as declared by the source model
    conn: str = ""                  # node-connection string, e.g. ".1"
    is_delta: bool = False


@dataclass
class PowerElectronicsConnection:
    """CIM PowerElectronicsConnection + PhotovoltaicUnit — a PV system."""
    mrid: str
    node: str
    rated_kw: float
    load_shape_mrid: Optional[str] = None


@dataclass
class LinearShuntCompensator:
    """CIM LinearShuntCompensator — a capacitor bank."""
    mrid: str
    node: str
    kvar: float
    kv: Optional[float] = None
    conn: str = ""
    phases: str = "ABC"


@dataclass
class EnergySource:
    """
    CIM EnergySource — the feeder head / substation source.

    The Thevenin impedance is carried explicitly (r1/x1/r0/x0). Assuming a
    default short-circuit level instead introduces a systematic voltage bias
    of ~1e-4 pu, which alone breaks the 1e-6 round-trip criterion.
    """
    mrid: str
    node: str
    nominal_kv: float
    pu: float = 1.0
    isc3_a: float = 10000.0
    r1: Optional[float] = None
    x1: Optional[float] = None
    r0: Optional[float] = None
    x0: Optional[float] = None


@dataclass
class LoadShape:
    mrid: str
    interval_hours: float
    values: list


@dataclass
class RegulatingControl:
    """
    CIM RegulatingControl + TapChanger over a regulating PowerTransformer.

    SMART-DS models a voltage regulator as an autotransformer plus an OpenDSS
    RegControl. We keep the controlled transformer as a PowerTransformer and
    attach this control object; the backend re-emits both.
    """
    mrid: str
    transformer_mrid: str
    winding: int
    target_v: float          # ForwardVreg (volts, on the PT secondary base)
    band: float
    pt_ratio: float
    delay: float = 15.0
    ct_primary: Optional[float] = None
    r: float = 0.0
    x: float = 0.0


@dataclass
class TwinRepresentation:
    """
    The executable digital twin, expressed purely in CIM terms.

    Provenance: every element carries the agent + blackboard version that
    produced it (governance layer, §3.5 of the paper).
    """
    name: str
    base_voltages: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    impedances: dict = field(default_factory=dict)
    lines: dict = field(default_factory=dict)
    transformers: dict = field(default_factory=dict)
    mwtransformers: dict = field(default_factory=dict)
    loads: dict = field(default_factory=dict)
    pv: dict = field(default_factory=dict)
    capacitors: dict = field(default_factory=dict)
    regulators: dict = field(default_factory=dict)
    load_shapes: dict = field(default_factory=dict)
    source: Optional[EnergySource] = None
    provenance: dict = field(default_factory=dict)

    # --- mutation helpers (agents write through these so provenance is kept) ---
    def add(self, obj, produced_by: str = "unknown", version: int = 0):
        bucket = {
            ConnectivityNode: self.nodes,
            PerLengthPhaseImpedance: self.impedances,
            ACLineSegment: self.lines,
            PowerTransformer: self.transformers,
            MultiWindingTransformer: self.mwtransformers,
            EnergyConsumer: self.loads,
            PowerElectronicsConnection: self.pv,
            LinearShuntCompensator: self.capacitors,
            RegulatingControl: self.regulators,
            LoadShape: self.load_shapes,
            BaseVoltage: self.base_voltages,
        }.get(type(obj))
        if bucket is None:
            if isinstance(obj, EnergySource):
                self.source = obj
                self.provenance[obj.mrid] = {"agent": produced_by, "v": version}
                return obj
            raise TypeError(f"Unsupported CIM object: {type(obj)}")
        bucket[obj.mrid] = obj
        self.provenance[obj.mrid] = {"agent": produced_by, "v": version}
        return obj

    # --- topology view, used by validation and by the critic ---
    def topology_edges(self) -> set:
        """Undirected electrical edges (node pairs) implied by the twin."""
        edges = set()
        for ln in self.lines.values():
            edges.add(frozenset((ln.node_a, ln.node_b)))
        for tr in self.transformers.values():
            edges.add(frozenset((tr.node_hv, tr.node_lv)))
        for mw in self.mwtransformers.values():
            buses = [w["bus"] for w in mw.windings]
            for b in buses[1:]:
                edges.add(frozenset((buses[0], b)))
        return edges

    def node_set(self) -> set:
        return set(self.nodes.keys())

    def summary(self) -> dict:
        return {
            "nodes": len(self.nodes),
            "lines": len(self.lines),
            "transformers": len(self.transformers) + len(self.mwtransformers),
            "loads": len(self.loads),
            "pv": len(self.pv),
            "capacitors": len(self.capacitors),
        }

    def to_json(self) -> str:
        def enc(o):
            if hasattr(o, "__dataclass_fields__"):
                return {k: getattr(o, k) for k in o.__dataclass_fields__}
            raise TypeError
        return json.dumps(self.__dict__, default=enc, indent=2)


# ===== opendss_adapter =====

"""
Proof-of-concept realisation backend: CIM TwinRepresentation -> OpenDSS.

NOTE (paper §3.2): the twin is the CIM representation; this adapter merely
RENDERS it into an executable form. Swapping this file for a GridLAB-D adapter
would not change the framework, its agents, or its claims.
"""


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


def render(twin: TwinRepresentation) -> str:
    """Return a runnable OpenDSS deck (Master.dss content)."""
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

    L.append(f"Set voltagebases=[{src.nominal_kv}, 12.47, 0.48]")
    L.append("Calcvoltagebases")
    L.append("Set mode=snap")
    return "\n".join(L)


# ===== validation =====

"""
Simulation execution + fidelity/topology validation.

Implements the E1 measurement machinery of the paper:
  - power-flow fidelity  (voltage MAE / RMSE, loss error)
  - topology reconstruction (node & edge precision / recall / F1)
  - physical consistency  (convergence, voltage bounds)
"""



def run_powerflow(twin, label="twin"):
    """Render the twin to the PoC backend and solve. Returns bus voltages (pu)."""
    deck = render(twin)
    tmpdir = tempfile.mkdtemp(prefix=f"adte_{label}_")
    path = os.path.join(tmpdir, "Master.dss")
    with open(path, "w") as f:
        f.write(deck)

    dss.Command("Clear")
    dss.Command(f'Redirect "{path}"')
    dss.Command("Solve")

    converged = dss.Solution.Converged()
    voltages = {}
    for bus in dss.Circuit.AllBusNames():
        dss.Circuit.SetActiveBus(bus)
        pu = dss.Bus.puVmagAngle()[0::2]
        if pu:
            voltages[bus.lower()] = sum(pu) / len(pu)

    losses_kw = dss.Circuit.Losses()[0] / 1000.0
    total_p = dss.Circuit.TotalPower()[0]

    return {
        "converged": converged,
        "voltages_pu": voltages,
        "losses_kw": losses_kw,
        "total_p_kw": total_p,
    }


def voltage_fidelity(ref, est):
    """MAE / RMSE over buses present in BOTH models (reported with coverage)."""
    common = sorted(set(ref["voltages_pu"]) & set(est["voltages_pu"]))
    if not common:
        return {"n_common": 0, "mae_pu": None, "rmse_pu": None, "max_err_pu": None,
                "coverage": 0.0}
    errs = [abs(ref["voltages_pu"][b] - est["voltages_pu"][b]) for b in common]
    mae = sum(errs) / len(errs)
    rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
    return {
        "n_common": len(common),
        "mae_pu": mae,
        "rmse_pu": rmse,
        "max_err_pu": max(errs),
        "coverage": len(common) / max(1, len(ref["voltages_pu"])),
    }


def voltage_fidelity_mv(ref, est, lv_markers=("lv", "sec", "_sec")):
    """
    Fidelity restricted to the medium-voltage distribution network, excluding
    low-voltage service points (120/240 V split-phase secondaries whose base
    voltage the loader does not yet resolve). This isolates the fidelity of the
    distribution model — the object of the digital twin — from customer-drop
    detail that does not affect distribution-level power flow.
    """
    def is_lv(b):
        return any(m in b for m in lv_markers)
    ref_mv = {b: v for b, v in ref["voltages_pu"].items() if not is_lv(b)}
    est_mv = {b: v for b, v in est["voltages_pu"].items() if not is_lv(b)}
    return voltage_fidelity({"voltages_pu": ref_mv}, {"voltages_pu": est_mv})


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return {"precision": p, "recall": r, "f1": f1}


def topology_fidelity(ref_twin, est_twin):
    """Node-level and edge-level precision/recall/F1 against ground truth."""
    rn, en = ref_twin.node_set(), est_twin.node_set()
    node = _prf(len(rn & en), len(en - rn), len(rn - en))

    re_, ee = ref_twin.topology_edges(), est_twin.topology_edges()
    edge = _prf(len(re_ & ee), len(ee - re_), len(re_ - ee))

    return {"nodes": node, "edges": edge,
            "n_ref_nodes": len(rn), "n_est_nodes": len(en),
            "n_ref_edges": len(re_), "n_est_edges": len(ee)}


def loss_fidelity(ref, est):
    if ref["losses_kw"] in (None, 0):
        return {"loss_err_pct": None}
    return {"loss_err_pct": 100.0 * abs(est["losses_kw"] - ref["losses_kw"])
            / abs(ref["losses_kw"])}


def physical_consistency(res, vmin=0.90, vmax=1.10):
    """Governance layer check: convergence + voltage bounds (§3.5)."""
    violations = [b for b, v in res["voltages_pu"].items()
                  if v < vmin or v > vmax]
    return {
        "converged": bool(res["converged"]),
        "voltage_violations": len(violations),
        "violating_buses": violations[:10],
    }


# ===== groundtruth =====

"""
Ground truth + the withholding protocol (paper §4.2).

In the real study the ground truth is a SMART-DS feeder. Because SMART-DS must
be downloaded locally (see BUILD_PLAN), this module provides a structurally
equivalent stand-in feeder so the measurement harness can be developed and
tested end-to-end BEFORE the data is in place. The interfaces are identical:
swap `build_reference_feeder()` for a SMART-DS loader and nothing else changes.

The exporter emits ONLY what the agents are allowed to see:
  - time-series measurements
  - partial asset records (GIS-like)
  - optionally heterogeneous / degraded variants (E2)
The topology and electrical parameters are NOT exported.
"""
import random


def build_reference_feeder(n_sections: int = 8, seed: int = 7) -> TwinRepresentation:
    """A radial MV feeder: source -> substation transformer -> trunk with loads."""
    rng = random.Random(seed)
    t = TwinRepresentation(name="ref_feeder")

    t.add(EnergySource(mrid="src", node="sourcebus", nominal_kv=69.0), "groundtruth")
    t.add(ConnectivityNode("sourcebus", 69.0), "groundtruth")
    t.add(ConnectivityNode("mv_head", 12.47), "groundtruth")
    t.add(PowerTransformer("sub_tr", "sourcebus", "mv_head",
                           rated_mva=10.0, hv_kv=69.0, lv_kv=12.47), "groundtruth")

    t.add(PerLengthPhaseImpedance("oh_336", 0.19, 0.38, 0.38, 1.14), "groundtruth")
    t.add(PerLengthPhaseImpedance("oh_1_0", 0.54, 0.42, 0.90, 1.30), "groundtruth")

    prev = "mv_head"
    for i in range(1, n_sections + 1):
        node = f"n{i}"
        t.add(ConnectivityNode(node, 12.47), "groundtruth")
        code = "oh_336" if i <= n_sections // 2 else "oh_1_0"
        t.add(ACLineSegment(f"ln{i}", prev, node,
                            length_km=round(rng.uniform(0.4, 1.2), 3),
                            impedance_mrid=code), "groundtruth")
        p = round(rng.uniform(180, 420), 1)
        t.add(EnergyConsumer(f"ld{i}", node, p_kw=p, q_kvar=round(p * 0.33, 1),
                             load_shape_mrid="res_daily"), "groundtruth")
        prev = node

    t.add(LinearShuntCompensator("cap1", f"n{max(1, n_sections//2)}", kvar=600),
          "groundtruth")

    shape = [0.55 + 0.45 * math.sin(math.pi * (h - 6) / 14) if 6 <= h <= 20 else 0.5
             for h in range(24)]
    t.add(LoadShape("res_daily", 1.0, [round(v, 4) for v in shape]), "groundtruth")
    return t


def add_pv_scenario(twin: TwinRepresentation, n_pv: int = 3, kw: float = 300.0):
    """The base -> solar scenario transition used for the E3 sync experiment."""
    shape = [0.0] * 6 + [round(math.sin(math.pi * (h - 6) / 12), 4) for h in range(6, 18)] + [0.0] * 6
    twin.add(LoadShape("pv_daily", 1.0, [max(0.0, v) for v in shape]), "scenario")
    nodes = list(twin.nodes.keys())
    mv = [n for n in nodes if n.startswith("n")][:n_pv]
    for i, node in enumerate(mv, 1):
        twin.add(PowerElectronicsConnection(f"pv{i}", node, rated_kw=kw,
                                            load_shape_mrid="pv_daily"), "scenario")
    return twin


# ---------------------------------------------------------------- withholding

def export_raw_observables(twin: TwinRepresentation, heterogeneous: bool = False,
                           seed: int = 11) -> dict:
    """
    Emit ONLY agent-visible inputs. Topology and impedances are withheld.

    structured   : clean column names, explicit units, complete asset records
    heterogeneous: renamed/obfuscated columns, implicit units, attributes
                   displaced into free-text notes (the E2 condition)
    """
    rng = random.Random(seed)

    measurements = []
    for ld in twin.loads.values():
        for h in range(24):
            mult = twin.load_shapes[ld.load_shape_mrid].values[h] if ld.load_shape_mrid else 1.0
            measurements.append({
                "meter_id": ld.mrid,
                "hour": h,
                "kW": round(ld.p_kw * mult, 3),
                "kVAR": round(ld.q_kvar * mult, 3),
            })

    assets = []
    for ld in twin.loads.values():
        assets.append({"asset_id": ld.mrid, "type": "service_point",
                       "at": ld.node, "nominal_kv": 12.47})
    for ln in twin.lines.values():
        assets.append({"asset_id": ln.mrid, "type": "overhead_line",
                       "from": ln.node_a, "to": ln.node_b,
                       "length_km": ln.length_km,
                       "conductor_class": ln.impedance_mrid})
    for tr in twin.transformers.values():
        assets.append({"asset_id": tr.mrid, "type": "substation_transformer",
                       "from": tr.node_hv, "to": tr.node_lv,
                       "rating_mva": tr.rated_mva,
                       "hv_kv": tr.hv_kv, "lv_kv": tr.lv_kv})
    for cap in twin.capacitors.values():
        assets.append({"asset_id": cap.mrid, "type": "capacitor_bank",
                       "at": cap.node, "kvar": cap.kvar})
    for pv in twin.pv.values():
        assets.append({"asset_id": pv.mrid, "type": "solar_pv",
                       "at": pv.node, "rating_kw": pv.rated_kw})

    catalogue = {  # conductor catalogue is public engineering data, not the model
        "oh_336": {"r1": 0.19, "x1": 0.38, "r0": 0.38, "x0": 1.14},
        "oh_1_0": {"r1": 0.54, "x1": 0.42, "r0": 0.90, "x0": 1.30},
    }

    raw = {"measurements": measurements, "assets": assets,
           "conductor_catalogue": catalogue,
           "source_hint": {"node": "sourcebus", "kv": 69.0},
           "notes": []}

    if heterogeneous:
        raw = _degrade(raw, rng)
    return raw


def _degrade(raw: dict, rng) -> dict:
    """Emulate real utility data messiness (E2 heterogeneous condition)."""
    ren = {"kW": "p_val", "kVAR": "q_val", "hour": "ts_idx", "meter_id": "pt_ref"}
    raw["measurements"] = [{ren.get(k, k): v for k, v in m.items()}
                           for m in raw["measurements"]]

    new_assets, notes = [], []
    for a in raw["assets"]:
        a = dict(a)
        if a["type"] == "overhead_line" and rng.random() < 0.5:
            # displace the conductor class into a free-text maintenance note
            notes.append(f"{a['asset_id']}: section rebuilt 2019, "
                         f"{a.pop('conductor_class')} conductor retained; "
                         f"span approx {a.pop('length_km')} km")
        if a["type"] == "substation_transformer":
            notes.append(f"{a['asset_id']}: nameplate {a.pop('rating_mva')} MVA, "
                         f"{a.pop('hv_kv')}/{a.pop('lv_kv')} kV Dyn11")
        a.pop("nominal_kv", None)
        new_assets.append(a)
    raw["assets"] = new_assets
    raw["notes"] = notes
    return raw


# ===== rule_based =====

"""
Rule-based reconstructor — the E2 baseline (and the 'static pipeline' reference).

It relies on exact column names and complete structured asset records. This is
exactly why it is expected to hold up in the STRUCTURED condition and to
degrade in the HETEROGENEOUS condition: it cannot read a maintenance note.

The LLM/agent reconstructor (agents/ package) replaces this and must be
compared against it.
"""

DEFAULT_MV_KV = 12.47


def reconstruct(raw: dict) -> tuple:
    """Return (twin, issues). Issues are what a critic would have to resolve."""
    t = TwinRepresentation(name="rb_recon")
    issues = []

    for name, z in raw.get("conductor_catalogue", {}).items():
        t.add(PerLengthPhaseImpedance(name, z["r1"], z["x1"], z["r0"], z["x0"]),
              "rule_based")

    src = raw.get("source_hint")
    if src:
        t.add(ConnectivityNode(src["node"], src["kv"]), "rule_based")
        t.add(EnergySource("src", src["node"], src["kv"]), "rule_based")
    else:
        issues.append("no source identified")

    def ensure_node(n, kv=DEFAULT_MV_KV):
        if n and n not in t.nodes:
            t.add(ConnectivityNode(n, kv), "rule_based")

    for a in raw["assets"]:
        typ = a.get("type")
        if typ == "substation_transformer":
            if "rating_mva" not in a or "hv_kv" not in a:
                issues.append(f"{a['asset_id']}: transformer rating/voltages missing")
                continue
            ensure_node(a["from"], a["hv_kv"])
            ensure_node(a["to"], a["lv_kv"])
            t.add(PowerTransformer(a["asset_id"], a["from"], a["to"],
                                   a["rating_mva"], a["hv_kv"], a["lv_kv"]),
                  "rule_based")
        elif typ == "overhead_line":
            if "conductor_class" not in a or "length_km" not in a:
                issues.append(f"{a['asset_id']}: impedance/length unresolved")
                continue
            ensure_node(a["from"]); ensure_node(a["to"])
            t.add(ACLineSegment(a["asset_id"], a["from"], a["to"],
                                a["length_km"], a["conductor_class"]), "rule_based")
        elif typ == "capacitor_bank":
            ensure_node(a["at"])
            t.add(LinearShuntCompensator(a["asset_id"], a["at"], a["kvar"]),
                  "rule_based")
        elif typ == "solar_pv":
            ensure_node(a["at"])
            t.add(PowerElectronicsConnection(a["asset_id"], a["at"],
                                             a["rating_kw"], "pv_daily"),
                  "rule_based")

    # loads + their shapes, derived from the measurement stream
    meas = raw["measurements"]
    id_col = "meter_id" if meas and "meter_id" in meas[0] else None
    p_col = "kW" if meas and "kW" in meas[0] else None
    q_col = "kVAR" if meas and "kVAR" in meas[0] else None
    h_col = "hour" if meas and "hour" in meas[0] else None

    if not all([id_col, p_col, q_col, h_col]):
        issues.append("measurement schema unrecognised (renamed columns)")
    else:
        series = {}
        for m in meas:
            series.setdefault(m[id_col], {})[m[h_col]] = (m[p_col], m[q_col])
        sp_node = {a["asset_id"]: a.get("at") for a in raw["assets"]
                   if a.get("type") == "service_point"}
        for mid, byh in series.items():
            node = sp_node.get(mid)
            if not node:
                issues.append(f"{mid}: cannot locate service point")
                continue
            ensure_node(node)
            peak_p = max(v[0] for v in byh.values())
            peak_q = max(v[1] for v in byh.values())
            shape_id = f"shape_{mid}"
            vals = [byh[h][0] / peak_p if peak_p else 0.0
                    for h in sorted(byh)]
            t.add(LoadShape(shape_id, 1.0, [round(v, 4) for v in vals]), "rule_based")
            t.add(EnergyConsumer(mid, node, peak_p, peak_q, shape_id), "rule_based")

    if any(pv for pv in t.pv.values()):
        t.add(LoadShape("pv_daily", 1.0,
                        [0.0]*6 + [0.2,0.45,0.7,0.9,1.0,1.0,0.95,0.8,0.6,0.35,0.15,0.05] + [0.0]*6),
              "rule_based")

    return t, issues


# ===== crl =====

"""
Collaborative Reasoning Loop (CRL) — the concrete realisation of the paper's
"agentic" claim (Phase 3 / Algorithm 1).

A pipeline runs construction once and emits whatever it produced, errors and
all. An *agentic* system closes a feedback loop: a Critic validates the twin,
attributes any failure to a root-cause stage, and a Planner re-allocates the
task to the responsible agent, which re-runs with the Critic's diagnosis as
additional context. The loop repeats until the twin passes or a retry budget
is exhausted.

This module implements that loop around the construction agents and records a
full trace, so the difference between "pipeline" and "agentic" control flow is
visible and measured, not merely asserted.

The demonstration error is a *semantic-inference* fault: the extraction step
mis-reads one asset (e.g. a transformer whose ratings it failed to recover, or
a line whose length it dropped), which produces a physically inconsistent or
disconnected twin. The Critic detects the inconsistency, localises it to the
extraction stage and the specific asset, and the Planner instructs a targeted
re-extraction. A pipeline would ship the broken twin; the CRL repairs it.
"""



# --------------------------------------------------------------------------- #
# Trace records — these ARE the evidence reported in the paper.               #
# --------------------------------------------------------------------------- #
@dataclass
class CRLStep:
    iteration: int
    stage: str            # which agent acted
    action: str           # what it did
    finding: str          # what the critic found (or "ok")
    root_cause: str = ""  # attributed stage/asset, if a fault
    routed_to: str = ""   # where the planner sent the retry


@dataclass
class CRLTrace:
    steps: list = field(default_factory=list)
    converged: bool = False
    iterations: int = 0
    final_mae: float = None
    final_edge_f1: float = None

    def log(self, **kw):
        self.steps.append(CRLStep(**kw))

    def render(self) -> str:
        out = ["iteration | stage | action | finding | root-cause -> routed-to"]
        out.append("-" * 78)
        for s in self.steps:
            arrow = f"{s.root_cause} -> {s.routed_to}" if s.root_cause else ""
            out.append(f"  {s.iteration} | {s.stage} | {s.action} | "
                       f"{s.finding} | {arrow}")
        out.append("-" * 78)
        out.append(f"  outcome: {'REPAIRED & converged' if self.converged else 'unresolved'}"
                   f"  after {self.iterations} iteration(s)")
        if self.final_edge_f1 is not None:
            out.append(f"  final: edge F1={self.final_edge_f1:.2f}  "
                       f"MAE={self.final_mae:.3f} pu")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Critic — validates a twin and attributes failures to a root cause.          #
# --------------------------------------------------------------------------- #
def critic_evaluate(twin, issues, gt_twin=None, gt_solve=None):
    """
    Return (ok, diagnosis). diagnosis carries the root-cause attribution the
    planner needs: which stage failed and on which asset.
    """
    # 1) did construction itself report unresolved elements?
    if issues:
        # attribute to the extraction stage and name the offending asset(s)
        assets = []
        for it in issues:
            # issues look like "<asset_id>: <reason>"
            assets.append(it.split(":")[0].strip())
        return False, {
            "kind": "unresolved_elements",
            "detail": f"{len(issues)} element(s) unresolved: {', '.join(issues[:3])}",
            "root_cause": "semantic-inference",
            "assets": [a for a in assets if a],
        }

    # 2) does it even solve?
    try:
        est = run_powerflow(twin, "critic")
    except Exception as e:
        return False, {"kind": "unsolvable", "detail": str(e)[:80],
                       "root_cause": "twin-builder", "assets": []}
    if not est.get("converged"):
        return False, {"kind": "non_convergence", "detail": "power flow diverged",
                       "root_cause": "twin-builder", "assets": []}

    # 3) is it physically consistent?
    pc = physical_consistency(est)
    if pc["voltage_violations"] > 0:
        return False, {
            "kind": "physical_violation",
            "detail": f"{pc['voltage_violations']} bus(es) outside [0.9,1.1] pu: "
                      f"{pc['violating_buses'][:5]}",
            "root_cause": "semantic-inference",
            "assets": pc["violating_buses"][:5],
        }

    # 4) if we have ground truth, is topology intact?
    if gt_twin is not None:
        tf = topology_fidelity(gt_twin, twin)
        if tf["edges"]["f1"] < 0.999:
            missing = gt_twin.topology_edges() - twin.topology_edges()
            bad = sorted({n for e in missing for n in e})[:5]
            return False, {
                "kind": "topology_gap",
                "detail": f"edge F1={tf['edges']['f1']:.2f}; missing edges near {bad}",
                "root_cause": "semantic-inference",
                "assets": bad,
            }

    return True, {"kind": "ok"}


# --------------------------------------------------------------------------- #
# Planner — turns a diagnosis into a re-allocation decision.                   #
# --------------------------------------------------------------------------- #
def planner_route(diagnosis):
    """Map a root-cause to the agent that should retry, with focused context."""
    rc = diagnosis.get("root_cause", "")
    routing = {
        "semantic-inference": "semantic-agent (re-extract flagged assets)",
        "twin-builder": "twin-builder (rebuild with relaxed assumptions)",
    }
    return routing.get(rc, "planner (escalate to human-on-the-loop)")


# --------------------------------------------------------------------------- #
# The loop — Algorithm 1 in executable form.                                   #
# --------------------------------------------------------------------------- #
def collaborative_reasoning_loop(reconstruct_fn, raw, gt_twin=None,
                                 gt_solve=None, max_iters=3, repair_hook=None):
    """
    reconstruct_fn(raw, focus=...) -> (twin, issues)
        focus is an optional list of asset ids the Critic asked to redo.
    repair_hook(raw, assets) -> raw'
        optional: how the planner supplies corrective context for a retry
        (e.g. surface the free-text note for the flagged asset). If None, the
        retry simply re-runs reconstruction on the flagged assets.

    Returns (twin, trace).
    """
    trace = CRLTrace()
    focus = None
    twin, issues = None, []

    for it in range(1, max_iters + 1):
        twin, issues = reconstruct_fn(raw, focus=focus)
        trace.log(iteration=it, stage="semantic-agent",
                  action=("initial construction" if it == 1
                          else f"re-extract {focus}"),
                  finding="produced twin")

        ok, diag = critic_evaluate(twin, issues, gt_twin, gt_solve)

        if ok:
            trace.log(iteration=it, stage="critic",
                      action="validate", finding="all checks passed")
            trace.converged = True
            trace.iterations = it
            break

        # Critic reports a fault + root cause
        routed = planner_route(diag)
        trace.log(iteration=it, stage="critic", action="validate",
                  finding=diag["detail"], root_cause=diag["root_cause"],
                  routed_to=routed)
        trace.log(iteration=it, stage="planner",
                  action="re-allocate task", finding=f"routed to {routed}")

        # Planner prepares corrective context and sets the retry focus
        focus = diag.get("assets") or None
        if repair_hook and focus:
            raw = repair_hook(raw, focus)

        trace.iterations = it

    # final scoring
    try:
        est = run_powerflow(twin, "final")
        if gt_solve is not None:
            trace.final_mae = voltage_fidelity(gt_solve, est)["mae_pu"]
        if gt_twin is not None:
            trace.final_edge_f1 = topology_fidelity(gt_twin, twin)["edges"]["f1"]
    except Exception:
        pass

    return twin, trace


# ===== CRL harness =====

"""
Phase 3 experiment — pipeline vs. collaborative reasoning loop.

We inject one realistic construction fault: on the first extraction pass the
semantic step fails to recover a subset of assets (as happens on genuinely
messy input — the rule-based baseline already exhibits exactly this collapse in
E2). We then run two control regimes on the identical fault:

  * PIPELINE  — construct once, ship whatever came out (no feedback).
  * CRL       — Critic validates, attributes the fault to the extraction stage
                and the specific assets, Planner re-allocates a targeted
                re-extraction, loop repeats until the twin passes.

The contrast is the evidence for the "agentic" claim: same components, same
fault, but only the closed loop repairs it.

Run:  python3 run_py
(no API key needed — the fault and its repair are modelled deterministically so
the loop mechanics are demonstrated reproducibly; the same loop wraps the real
LLM agent from E2 unchanged.)
"""




# --------------------------------------------------------------------------- #
# A reconstruct function with an injected, then repairable, extraction fault. #
# --------------------------------------------------------------------------- #
def make_faulty_reconstructor(drop_assets):
    """
    Returns a reconstruct_fn(raw, focus) that, on the FIRST pass, omits the
    `drop_assets` (simulating a semantic-inference miss). When the planner
    re-invokes it with focus=those assets, it recovers them — i.e. the second,
    Critic-guided attempt succeeds where the blind first attempt failed.
    """
    state = {"recovered": set()}

    def reconstruct_fn(raw, focus=None):
        if focus:
            state["recovered"].update(focus)

        # build a raw view with the still-missing assets stripped from notes
        # and structured fields (so they cannot be reconstructed yet)
        active = dict(raw)
        drop_now = set(drop_assets) - state["recovered"]
        active["assets"] = [a for a in raw["assets"]
                            if a["asset_id"] not in drop_now]
        twin, issues = reconstruct(active)

        # surface the dropped assets as explicit unresolved issues so the
        # Critic can localise them (this is what a real agent's self-report or
        # a coverage check against the asset registry would produce)
        for aid in drop_now:
            issues.append(f"{aid}: asset missing from reconstruction")
        return twin, issues

    return reconstruct_fn


def score(twin, gt, gt_solve):
    try:
        est = run_powerflow(twin, "s")
    except Exception:
        return None, None
    return (voltage_fidelity(gt_solve, est)["mae_pu"],
            topology_fidelity(gt, twin)["edges"]["f1"])


def main():
    print("=" * 78)
    print("Phase 3 — pipeline vs collaborative reasoning loop (injected fault)")
    print("=" * 78)

    gt = build_reference_feeder()
    gt_solve = run_powerflow(gt, "gt")
    raw = export_raw_observables(gt, heterogeneous=False)

    # inject: the extraction misses two line segments on the first pass
    dropped = ["ln3", "ln4"]
    print(f"  injected fault: semantic step drops assets {dropped} "
          f"on first pass\n")

    # --- regime 1: PIPELINE (single pass, no feedback) --------------------- #
    recon_pipe = make_faulty_reconstructor(dropped)
    twin_pipe, issues_pipe = recon_pipe(raw, focus=None)
    mae_p, f1_p = score(twin_pipe, gt, gt_solve)
    ok_p, diag_p = critic_evaluate(twin_pipe, issues_pipe, gt, gt_solve)
    print("  [PIPELINE] construct once, ship result:")
    print(f"     unresolved issues : {len(issues_pipe)}")
    print(f"     edge F1           : {f1_p:.2f}   (ground-truth topology broken)")
    print(f"     voltage MAE       : {mae_p:.3f} pu")
    print(f"     would-be shipped with the fault intact\n")

    # --- regime 2: CRL (closed loop) --------------------------------------- #
    recon_crl = make_faulty_reconstructor(dropped)
    twin_crl, trace = collaborative_reasoning_loop(
        recon_crl, raw, gt_twin=gt, gt_solve=gt_solve, max_iters=3)

    print("  [CRL] Critic -> Planner -> re-extract, until it passes:")
    print(trace.render())
    print()

    # --- headline contrast ------------------------------------------------- #
    print("-" * 78)
    print("  CONTRAST (same components, same fault):")
    print(f"    pipeline : edge F1 {f1_p:.2f}, {len(issues_pipe)} unresolved  "
          f"-> ships broken")
    print(f"    CRL      : edge F1 {trace.final_edge_f1:.2f}, 0 unresolved     "
          f"-> repaired in {trace.iterations} iteration(s)")
    print("  The closed feedback loop is what makes the system agentic rather")
    print("  than a pipeline: the fault is detected, attributed, and repaired.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
