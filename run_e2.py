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
import os
import sys

_PKG = os.path.dirname(os.path.abspath(__file__))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from core.groundtruth import build_reference_feeder, export_raw_observables
from core.validation import (run_powerflow, voltage_fidelity, topology_fidelity)
from agents import rule_based
from agents import llm_semantic


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
    print("  " + llm_semantic.provider_status())

    gt = build_reference_feeder()
    gt_solve = _ground_truth(gt)
    print(f"  ground-truth feeder: {gt.summary()}  converged={gt_solve['converged']}")

    structured = export_raw_observables(gt, heterogeneous=False)
    hetero = export_raw_observables(gt, heterogeneous=True)

    rows = []

    # rule-based (no key needed)
    rb_s = _score(gt, gt_solve, *rule_based.reconstruct(structured))
    rb_h = _score(gt, gt_solve, *rule_based.reconstruct(hetero))
    rows.append(("rule-based", "structured", rb_s))
    rows.append(("rule-based", "heterogeneous", rb_h))

    # LLM agent (needs key)
    try:
        llm_semantic._detect_provider()
        have_llm = True
    except llm_semantic.LLMUnavailable:
        have_llm = False

    if have_llm:
        llm_s = _score(gt, gt_solve, *llm_semantic.reconstruct(structured))
        llm_h = _score(gt, gt_solve, *llm_semantic.reconstruct(hetero))
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
