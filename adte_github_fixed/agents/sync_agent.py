"""
Monitoring / Synchronisation agent (E3).

A digital twin is only "living" if it tracks its physical counterpart as that
counterpart changes. This agent closes the observe-detect-resynchronise loop:

  1. it holds the current twin and a running comparison against incoming
     measurements from the physical system;
  2. when the measured state diverges from the twin's prediction beyond a
     tolerance, it raises a *change-detected* event (this is the detection
     latency we report);
  3. it re-grounds the twin — re-running construction against the new
     observables — and confirms that the updated twin again tracks reality.

The change we exercise is the SMART-DS base->solar-plus-storage transition,
reproduced in controlled form: distributed PV is added to the feeder, which
shifts the voltage profile (PV back-feed raises voltages). A static twin keeps
predicting the old profile and its error grows; the synchronised twin detects
the shift and re-grounds, restoring tracking.
"""
from core.validation import run_powerflow, voltage_fidelity


def measured_state(physical_twin):
    """The physical system's true state, as it would arrive from telemetry."""
    return run_powerflow(physical_twin, "physical")


def tracking_error(twin, phys_solve):
    """How well the twin currently predicts the measured physical state."""
    try:
        est = run_powerflow(twin, "twin")
    except Exception:
        return None
    return voltage_fidelity(phys_solve, est)["mae_pu"]


def detect_change(twin, phys_solve, tol=0.002):
    """
    Change-detection test: has the physical state drifted from what the twin
    predicts, beyond tolerance? Returns (changed, error).

    tol is in per-unit voltage; 0.002 pu is a tight but realistic synchronisation
    threshold for a distribution feeder (well below the ±0.05 pu operating band),
    so that genuine physical change is caught while numerical noise is not.
    """
    err = tracking_error(twin, phys_solve)
    if err is None:
        return True, None
    return (err > tol), err


def synchronise(reconstruct_fn, new_observables, tol=0.01):
    """
    Re-ground the twin against the new observables and return the updated twin.
    This is the 'evolve' step: construction is re-run on the changed system.
    """
    twin, issues = reconstruct_fn(new_observables)
    return twin, issues
