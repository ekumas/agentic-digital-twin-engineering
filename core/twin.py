"""
Backend-agnostic, CIM-grounded twin representation.

This is THE artefact of the framework. It is deliberately independent of any
simulator: OpenDSS (or GridLAB-D, or a surrogate) is rendered FROM this by an
adapter. Class names follow IEC 61968/61970 CIM.
"""
from dataclasses import dataclass, field
from typing import Optional
import json


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
