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
import math
import random
from core.twin import (
    TwinRepresentation, ConnectivityNode, PerLengthPhaseImpedance,
    ACLineSegment, PowerTransformer, EnergyConsumer, EnergySource,
    LinearShuntCompensator, PowerElectronicsConnection, LoadShape,
)


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


def add_pv_scenario(twin: TwinRepresentation, n_pv: int = 3, kw: float = 300.0,
                    snapshot_active: bool = False):
    """The base -> solar scenario transition used for the E3 sync experiment.

    snapshot_active=True omits the daily shape so PV generates at Pmpp — the
    mid-day solar peak, the state of maximum back-feed that most stresses a twin
    that does not know the PV exists. Used to make the physical change in E3
    unambiguous.
    """
    shape = [0.0] * 6 + [round(math.sin(math.pi * (h - 6) / 12), 4) for h in range(6, 18)] + [0.0] * 6
    twin.add(LoadShape("pv_daily", 1.0, [max(0.0, v) for v in shape]), "scenario")
    nodes = list(twin.nodes.keys())
    mv = [n for n in nodes if n.startswith("n")][:n_pv]
    for i, node in enumerate(mv, 1):
        twin.add(PowerElectronicsConnection(
            f"pv{i}", node, rated_kw=kw,
            load_shape_mrid=None if snapshot_active else "pv_daily"), "scenario")
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
