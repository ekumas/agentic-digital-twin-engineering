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
from dataclasses import dataclass, field

from core.validation import (run_powerflow, physical_consistency,
                             voltage_fidelity, topology_fidelity)


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
