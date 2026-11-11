"""
Rule-based reconstructor — the E2 baseline (and the 'static pipeline' reference).

It relies on exact column names and complete structured asset records. This is
exactly why it is expected to hold up in the STRUCTURED condition and to
degrade in the HETEROGENEOUS condition: it cannot read a maintenance note.

The LLM/agent reconstructor (agents/ package) replaces this and must be
compared against it.
"""
from core.twin import (
    TwinRepresentation, ConnectivityNode, PerLengthPhaseImpedance,
    ACLineSegment, PowerTransformer, EnergyConsumer, EnergySource,
    LinearShuntCompensator, PowerElectronicsConnection, LoadShape,
)

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
