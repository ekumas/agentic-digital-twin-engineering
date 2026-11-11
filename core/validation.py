"""
Simulation execution + fidelity/topology validation.

Implements the E1 measurement machinery of the paper:
  - power-flow fidelity  (voltage MAE / RMSE, loss error)
  - topology reconstruction (node & edge precision / recall / F1)
  - physical consistency  (convergence, voltage bounds)
"""
import math
import tempfile
import os
import opendssdirect as dss

from backends.opendss_adapter import render


def run_powerflow(twin, label="twin", mode="snap"):
    """Render the twin to the PoC backend and solve. Returns bus voltages (pu).
    mode is passed to the adapter ("snap" or "daily")."""
    deck = render(twin, mode=mode)
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
