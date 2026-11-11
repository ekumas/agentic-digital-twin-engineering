#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAZ 2 - LLM ANLAMSAL CIKARIM DENEYI (E2) - BAGIMSIZ TEK DOSYA
KULLANIM:
    export ANTHROPIC_API_KEY="sk-ant-..."   # VEYA  export OPENAI_API_KEY="sk-..."
    python3 faz2_test.py
Anahtar yoksa kural-tabanli satirlar calisir, LLM satirlari atlanir.
Anahtari ASLA koda yazmayin. GEREKSINIM: pip install opendssdirect.py
"""
import os, sys, math, tempfile, json, re
import urllib.request, urllib.error
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


def rb_reconstruct(raw: dict) -> tuple:
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


# ===== llm_semantic =====

"""
LLM semantic-inference agent — the core of the "agentic" contribution (E2).

Where the rule-based baseline (agents/rule_based.py) collapses on heterogeneous
input — because it needs exact column names and complete structured records —
this agent recovers the missing structure by *interpreting* free-text
maintenance notes and renamed measurement columns, then emitting CIM-shaped
structured output.

Key design points that map onto the paper's claims:
  * LLM-enabled, not LLM-dependent: the provider is chosen at run time from the
    environment (ANTHROPIC_API_KEY or OPENAI_API_KEY); any capable model fits.
  * CIM grounding constrains the LLM: we force JSON output against an explicit
    schema and validate it, rather than accepting free prose. This is the
    concrete mechanism behind "CIM grounding reduces hallucination".
  * The LLM interprets, it does not compute: it recovers conductor class,
    length, ratings and column meanings; all numerical solving stays in OpenDSS.

The API key is read from the environment and never logged. Set one of:
    export ANTHROPIC_API_KEY=...      # uses Claude
    export OPENAI_API_KEY=...         # uses GPT
"""


DEFAULT_MV_KV = 12.47


# --------------------------------------------------------------------------- #
# Provider abstraction — chosen from the environment.                         #
# --------------------------------------------------------------------------- #
class LLMUnavailable(RuntimeError):
    pass


def _detect_provider():
    """Return (provider, model, key) from the environment, or raise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("anthropic",
                os.environ.get("ADTE_LLM_MODEL", "claude-sonnet-4-5"),
                os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("OPENAI_API_KEY"):
        return ("openai",
                os.environ.get("ADTE_LLM_MODEL", "gpt-4o"),
                os.environ["OPENAI_API_KEY"])
    raise LLMUnavailable(
        "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your "
        "environment (do not hard-code it). The rule-based baseline still runs "
        "without a key; only the LLM agent needs one.")


def _call_llm(system: str, user: str, max_tokens: int = 8000) -> str:
    """Single-turn completion. Returns raw text. Provider-agnostic."""
    provider, model, key = _detect_provider()

    if provider == "anthropic":
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            })
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))

    else:  # openai
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {key}",
            })
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str):
    """Pull the first JSON object out of a model response, tolerating fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


# --------------------------------------------------------------------------- #
# The CIM-constrained extraction prompt.                                      #
# --------------------------------------------------------------------------- #
_SYSTEM = """You are a power-distribution data engineer. You convert messy, \
heterogeneous utility records — free-text maintenance notes, renamed telemetry \
columns, partial asset rows — into a strict structured form for building a \
CIM-grounded network model.

You NEVER invent values that are not supported by the input. If a value is not \
recoverable, use null. You only INTERPRET what is present (e.g. read a \
conductor class or a span length out of a maintenance note, or recognise that \
a column named 'p_val' is real power in kW).

Return ONLY a JSON object, no prose, with this exact shape:
{
  "assets": [
    {"asset_id": str,
     "type": "overhead_line"|"substation_transformer"|"capacitor_bank"|"solar_pv"|"service_point",
     "from": str|null, "to": str|null, "at": str|null,
     "conductor_class": str|null, "length_km": number|null,
     "rating_mva": number|null, "hv_kv": number|null, "lv_kv": number|null,
     "kvar": number|null, "rating_kw": number|null}
  ],
  "measurement_columns": {"meter_id": str|null, "kW": str|null,
                          "kVAR": str|null, "hour": str|null},
  "notes_resolved": [ {"asset_id": str, "recovered": [str]} ]
}
The measurement_columns map your best guess of which ACTUAL column name in the \
data carries each quantity (e.g. {"kW": "p_val"})."""


def _build_user_prompt(raw: dict) -> str:
    assets = json.dumps(raw.get("assets", []), ensure_ascii=False)
    notes = json.dumps(raw.get("notes", []), ensure_ascii=False)
    cols = list(raw["measurements"][0].keys()) if raw.get("measurements") else []
    return (f"Asset rows (some fields may be missing):\n{assets}\n\n"
            f"Free-text maintenance notes:\n{notes}\n\n"
            f"Measurement columns present in the telemetry: {cols}\n\n"
            f"Recover every asset's missing fields from the notes, and map the "
            f"measurement columns. Return the JSON object only.")


# --------------------------------------------------------------------------- #
# Public API — mirrors agents.rule_based.reconstruct                          #
# --------------------------------------------------------------------------- #
def llm_reconstruct(raw: dict) -> tuple:
    """
    Return (twin, issues), same contract as the rule-based baseline.

    The LLM recovers structure from notes/renamed columns; we then build the
    twin deterministically from the LLM's *structured* output and validate it.
    """
    t = TwinRepresentation(name="llm_recon")
    issues = []

    # conductor catalogue is a clean lookup in both conditions
    for name, z in raw.get("conductor_catalogue", {}).items():
        t.add(PerLengthPhaseImpedance(name, r1_ohm_per_km=z["r1"],
                                      x1_ohm_per_km=z["x1"], r0_ohm_per_km=z["r0"],
                                      x0_ohm_per_km=z["x0"]), "llm_semantic")

    src = raw.get("source_hint")
    if src:
        t.add(ConnectivityNode(src["node"], src["kv"]), "llm_semantic")
        t.add(EnergySource("src", src["node"], src["kv"]), "llm_semantic")
    else:
        issues.append("no source identified")

    # --- the semantic-inference step -------------------------------------- #
    try:
        raw_text = _call_llm(_SYSTEM, _build_user_prompt(raw))
        if os.environ.get("ADTE_DEBUG"):
            
            print("\n----- LLM RAW RESPONSE (first 1200 chars) -----",
                  file=sys.stderr)
            print(raw_text[:1200], file=sys.stderr)
            print("----- END -----\n", file=sys.stderr)
        parsed = _extract_json(raw_text)
        if os.environ.get("ADTE_DEBUG"):
            
            n_assets = len(parsed.get("assets", []))
            types = {}
            for a in parsed.get("assets", []):
                types[a.get("type")] = types.get(a.get("type"), 0) + 1
            print(f"[debug] parsed {n_assets} assets: {types}", file=sys.stderr)
            print(f"[debug] measurement_columns: "
                  f"{parsed.get('measurement_columns')}", file=sys.stderr)
    except LLMUnavailable as e:
        raise
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as e:
        issues.append(f"LLM inference failed: {e}")
        return t, issues

    def ensure_node(n, kv=DEFAULT_MV_KV):
        if n and n not in t.nodes:
            t.add(ConnectivityNode(n, kv), "llm_semantic")

    for a in parsed.get("assets", []):
        typ = a.get("type")
        try:
            if typ == "substation_transformer":
                if a.get("rating_mva") is None or a.get("hv_kv") is None:
                    issues.append(f"{a['asset_id']}: transformer rating unrecovered")
                    continue
                ensure_node(a.get("from"), a["hv_kv"])
                ensure_node(a.get("to"), a.get("lv_kv") or DEFAULT_MV_KV)
                t.add(PowerTransformer(a["asset_id"], a["from"], a["to"],
                                       a["rating_mva"], a["hv_kv"],
                                       a.get("lv_kv") or DEFAULT_MV_KV), "llm_semantic")
            elif typ == "overhead_line":
                if not a.get("conductor_class") or a.get("length_km") is None:
                    issues.append(f"{a['asset_id']}: impedance/length unrecovered")
                    continue
                ensure_node(a.get("from")); ensure_node(a.get("to"))
                t.add(ACLineSegment(a["asset_id"], a["from"], a["to"],
                                    a["length_km"], a["conductor_class"]), "llm_semantic")
            elif typ == "capacitor_bank":
                ensure_node(a.get("at"))
                t.add(LinearShuntCompensator(a["asset_id"], a["at"], a.get("kvar", 0)),
                      "llm_semantic")
            elif typ == "solar_pv":
                ensure_node(a.get("at"))
                t.add(PowerElectronicsConnection(a["asset_id"], a["at"],
                                                 a.get("rating_kw", 0), "pv_daily"),
                      "llm_semantic")
        except (KeyError, TypeError) as e:
            issues.append(f"{a.get('asset_id', '?')}: malformed asset ({e})")

    # --- measurement columns recovered by the LLM ------------------------- #
    cmap = parsed.get("measurement_columns", {})
    meas = raw.get("measurements", [])
    id_c, p_c, q_c, h_c = (cmap.get("meter_id"), cmap.get("kW"),
                           cmap.get("kVAR"), cmap.get("hour"))
    if meas and all([id_c, p_c, q_c, h_c]) and id_c in meas[0]:
        series = {}
        for m in meas:
            series.setdefault(m[id_c], {})[m[h_c]] = (m[p_c], m[q_c])
        sp_node = {a["asset_id"]: a.get("at") for a in parsed.get("assets", [])
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
            vals = [byh[h][0] / peak_p if peak_p else 0.0 for h in sorted(byh)]
            t.add(LoadShape(shape_id, 1.0, [round(v, 4) for v in vals]), "llm_semantic")
            t.add(EnergyConsumer(mid, node, peak_p, peak_q, shape_id), "llm_semantic")
    else:
        issues.append("measurement columns not recovered")

    if any(pv for pv in t.pv.values()):
        t.add(LoadShape("pv_daily", 1.0,
                        [0.0]*6 + [0.2,0.45,0.7,0.9,1.0,1.0,0.95,0.8,0.6,0.35,0.15,0.05] + [0.0]*6),
              "llm_semantic")

    return t, issues


def provider_status() -> str:
    """Human-readable note on which provider will be used (no key leakage)."""
    try:
        provider, model, _ = _detect_provider()
        return f"LLM provider: {provider} (model {model})"
    except LLMUnavailable as e:
        return f"LLM unavailable: {e}"


# ===== E2 harness =====

"""
E2 — LLM necessity experiment.

Question (RQ2): is a semantic reasoner (LLM) actually needed, or does a
rule-based extractor suffice? Hypothesis H2: the two are comparable on
STRUCTURED input, but the rule-based baseline collapses on HETEROGENEOUS input
(free-text notes, renamed columns) while the LLM agent recovers the structure.

We build one reference feeder as ground truth, export both a structured and a
heterogeneous view of its observables, reconstruct each with both agents, and
score every reconstruction against the ground truth with the same metrics.

Run:
    export ANTHROPIC_API_KEY=...    # or OPENAI_API_KEY
    python3 run_e2.py

Without a key, the rule-based rows still run; the LLM rows report "skipped".
"""




def _ground_truth(twin):
    est = run_powerflow(twin, "gt")
    return est


def _score(gt_twin, gt_solve, recon_twin, issues):
    """Voltage + topology fidelity of a reconstruction vs ground truth."""
    try:
        rec = run_powerflow(recon_twin, "rec")
    except Exception as e:
        return {"converged": False, "mae": None, "edge_f1": None,
                "issues": len(issues), "error": str(e)[:60]}
    vf = voltage_fidelity(gt_solve, rec)
    tf = topology_fidelity(gt_twin, recon_twin)
    return {
        "converged": rec.get("converged", False),
        "mae": vf["mae_pu"],
        "node_f1": tf["nodes"]["f1"],
        "edge_f1": tf["edges"]["f1"],
        "issues": len(issues),
    }


def _fmt(s):
    if s.get("error"):
        return f"error: {s['error']}"
    mae = "n/a" if s["mae"] is None else f"{s['mae']:.3f}"
    return (f"converged={str(s['converged']):5s}  MAE={mae:>6s}  "
            f"edgeF1={s['edge_f1']:.2f}  issues={s['issues']}")


def main():
    print("=" * 74)
    print("E2 — LLM necessity: rule-based vs LLM, structured vs heterogeneous")
    print("=" * 74)
    print("  " + provider_status())

    gt = build_reference_feeder()
    gt_solve = _ground_truth(gt)
    print(f"  ground-truth feeder: {gt.summary()}  converged={gt_solve['converged']}")

    structured = export_raw_observables(gt, heterogeneous=False)
    hetero = export_raw_observables(gt, heterogeneous=True)

    rows = []

    # rule-based (no key needed)
    rb_s = _score(gt, gt_solve, *rb_reconstruct(structured))
    rb_h = _score(gt, gt_solve, *rb_reconstruct(hetero))
    rows.append(("rule-based", "structured", rb_s))
    rows.append(("rule-based", "heterogeneous", rb_h))

    # LLM agent (needs key)
    try:
        _detect_provider()
        have_llm = True
    except LLMUnavailable:
        have_llm = False

    if have_llm:
        llm_s = _score(gt, gt_solve, *llm_reconstruct(structured))
        llm_h = _score(gt, gt_solve, *llm_reconstruct(hetero))
        rows.append(("LLM-agent", "structured", llm_s))
        rows.append(("LLM-agent", "heterogeneous", llm_h))
    else:
        rows.append(("LLM-agent", "structured", {"skipped": True}))
        rows.append(("LLM-agent", "heterogeneous", {"skipped": True}))

    print("-" * 74)
    for agent, cond, s in rows:
        if s.get("skipped"):
            print(f"  {agent:11s} {cond:14s} skipped (no API key)")
        else:
            print(f"  {agent:11s} {cond:14s} {_fmt(s)}")
    print("-" * 74)

    # the H2 headline, if we have both
    if have_llm:
        print("\n  H2 check (edge F1, higher is better):")
        print(f"    structured   : rule-based {rb_s['edge_f1']:.2f}  vs  "
              f"LLM {rows[2][2]['edge_f1']:.2f}")
        print(f"    heterogeneous: rule-based {rb_h['edge_f1']:.2f}  vs  "
              f"LLM {rows[3][2]['edge_f1']:.2f}")
        print("    => expect: comparable on structured, LLM >> rule-based on heterogeneous")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
