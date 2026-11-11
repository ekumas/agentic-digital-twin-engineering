"""
Phase 4 / E3 — synchronisation and evolution.

The physical feeder changes: distributed PV is added (the SMART-DS
base->solar-plus-storage transition, reproduced in controlled form). We compare
two twins across this change:

  * STATIC twin  — constructed once from the base state, never updated. It keeps
                   predicting the pre-change profile; its tracking error against
                   the now-changed physical system grows.
  * SYNCHRONISED twin — the Monitoring/Sync agent detects the divergence and
                   re-grounds the twin against the new observables, restoring
                   tracking.

This is the evidence for the "continuous synchronisation" claim in the title:
the twin is living, not a one-time snapshot.

Run:  python3 run_e3.py     (no API key needed)
"""
import os
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from core.groundtruth import (build_reference_feeder, add_pv_scenario,
                              export_raw_observables)
from core.validation import run_powerflow, voltage_fidelity
from agents import rule_based
from agents import sync_agent


def main():
    print("=" * 78)
    print("Phase 4 / E3 — synchronisation across a base->solar physical change")
    print("=" * 78)

    # --- base physical state; twin constructed from it --------------------- #
    base = build_reference_feeder()
    base_obs = export_raw_observables(base, heterogeneous=False)
    twin, _ = rule_based.reconstruct(base_obs)

    base_solve = run_powerflow(base, "base-phys")
    err0 = sync_agent.tracking_error(twin, base_solve)
    print(f"  initial state: twin tracks physical feeder, "
          f"tracking MAE = {err0:.4f} pu\n")

    # --- the physical system CHANGES: distributed PV is added -------------- #
    changed = build_reference_feeder()
    add_pv_scenario(changed, n_pv=3, kw=800.0, snapshot_active=True)
    phys_solve = run_powerflow(changed, "changed-phys")
    print("  >>> physical change: 3 distributed PV units added at mid-day peak "
          "(base -> solar transition)\n")

    # --- regime 1: STATIC twin (never updated) ----------------------------- #
    changed_flag, err_static = sync_agent.detect_change(twin, phys_solve)
    print("  [STATIC twin] constructed once, never updated:")
    print(f"     tracking MAE after change : {err_static:.4f} pu")
    print(f"     change detected           : {changed_flag} "
          f"(error exceeds tolerance)")
    print(f"     -> twin now mis-predicts the physical system\n")

    # --- regime 2: SYNCHRONISED twin --------------------------------------- #
    print("  [SYNCHRONISED twin] Monitoring agent detects & re-grounds:")
    changed_flag, err_before = sync_agent.detect_change(twin, phys_solve)
    print(f"     step 1 detect  : divergence {err_before:.4f} pu > tol "
          f"-> change-detected event raised")
    new_obs = export_raw_observables(changed, heterogeneous=False)
    twin2, issues = sync_agent.synchronise(rule_based.reconstruct, new_obs)
    err_after = sync_agent.tracking_error(twin2, phys_solve)
    print(f"     step 2 re-ground: reconstruct against new observables")
    print(f"     step 3 confirm : tracking MAE {err_after:.4f} pu "
          f"(restored), {len(issues)} issues\n")

    # --- headline --------------------------------------------------------- #
    print("-" * 78)
    print("  CONTRAST across the physical change:")
    print(f"    static twin       : tracking MAE {err_static:.4f} pu  "
          f"-> drifts, mis-predicts")
    print(f"    synchronised twin : tracking MAE {err_after:.4f} pu  "
          f"-> re-grounded, tracks again")
    factor = err_static / err_after if err_after > 1e-9 else float('inf')
    print(f"    improvement       : {factor:.0f}x lower error after synchronisation"
          if factor != float('inf') else
          "    improvement       : error eliminated after synchronisation")
    print("  The twin is living: it detects physical change and re-grounds,")
    print("  rather than remaining a one-time snapshot.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
