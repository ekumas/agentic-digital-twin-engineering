"""
E-CRL ablation — what does the collaborative reasoning loop add over a retry?

Three control regimes wrap the SAME extraction component, the SAME critic and
the SAME stochastic fault model:

  PIPELINE     construct once, ship the result.
  BLIND RETRY  the critic is used only as an accept/reject gate. On reject the
               whole extraction is re-run from scratch (no localisation, no
               memory of what was already correct).
  CRL          the critic localises the fault to specific assets; the planner
               re-allocates a targeted re-extraction of only those assets and
               keeps everything already validated.

Fault model (identical for every regime and every call): each extraction call
on a line asset independently fails to recover it with probability p. A
targeted re-extraction is NOT guaranteed to succeed - it misses with the same
probability p. This removes the built-in advantage of the deterministic
demonstration in run_crl.py, where a focused retry always succeeds.

Outputs: acceptance rate within the iteration budget, final edge F1, and the
number of asset-level extraction calls (cost), swept over miss rate p and
feeder size N. Closed-form expectations are printed next to the simulation:

  blind retry acceptance = 1 - (1 - (1-p)^N)^K
  CRL acceptance         = (1 - p^K)^N

Run:  python3 run_crl_ablation.py            (no API key needed)
"""
import csv
import os
import random
import sys
import time

_PKG = os.path.dirname(os.path.abspath(__file__))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

from core.groundtruth import build_reference_feeder, export_raw_observables
from core.validation import topology_fidelity
from agents import rule_based
from agents.crl import critic_evaluate

K_BUDGET = 3                       # same max_iters as run_crl.py / Algorithm 1
MISS_RATES = [0.02, 0.05, 0.10, 0.20, 0.30]
FEEDER_SIZES = [8, 32, 128]        # line segments in the reference feeder
N_SEEDS = 2000


def scaled_feeder(n):
    """Reference feeder with n line sections. Total load and total trunk
    length are held at the 8-section baseline (per-section load and span are
    scaled by 8/n) so the feeder stays physically feasible at every size and
    the critic's physical-consistency check remains meaningful."""
    gt = build_reference_feeder(n_sections=n)
    k = 8.0 / n
    for ld in gt.loads.values():
        ld.p_kw = round(ld.p_kw * k, 3)
        ld.q_kvar = round(ld.q_kvar * k, 3)
    for ln in gt.lines.values():
        ln.length_km = round(ln.length_km * k, 4)
    return gt


def extract(raw, line_ids, have, rng, p, targets=None):
    """One extraction call. Attempts `targets` (default: every line); each
    attempted line is recovered with probability 1-p. Returns the new `have`
    set and the number of asset-level extraction attempts made."""
    attempt = list(line_ids) if targets is None else list(targets)
    got = set(have)
    for aid in attempt:
        if rng.random() >= p:
            got.add(aid)
        else:
            got.discard(aid)
    return got, len(attempt)


def build(raw, line_ids, have):
    """Run the real reconstructor on the assets recovered so far and surface
    the missing ones as unresolved issues (what a coverage check reports)."""
    missing = set(line_ids) - have
    active = dict(raw)
    active["assets"] = [a for a in raw["assets"] if a["asset_id"] not in missing]
    twin, issues = rule_based.reconstruct(active)
    issues = list(issues) + [f"{aid}: asset missing from reconstruction"
                             for aid in sorted(missing)]
    return twin, issues


def run_regime(regime, raw, gt, line_ids, p, rng):
    have, calls = set(), 0
    twin, ok, it = None, False, 0
    targets = None
    for it in range(1, (1 if regime == "pipeline" else K_BUDGET) + 1):
        if regime == "blind" or it == 1:
            have, c = extract(raw, line_ids, set(), rng, p)          # from scratch
        else:                                                         # CRL
            have, c = extract(raw, line_ids, have, rng, p, targets)  # targeted
        calls += c
        twin, issues = build(raw, line_ids, have)
        ok, diag = critic_evaluate(twin, issues, gt, None)
        if ok:
            break
        targets = [a for a in diag.get("assets", []) if a in line_ids]
    f1 = topology_fidelity(gt, twin)["edges"]["f1"]
    return ok, f1, calls, it


def main():
    t0 = time.time()
    rows = []
    print("=" * 86)
    print("E-CRL ablation: pipeline vs blind retry vs collaborative reasoning loop")
    print(f"budget K={K_BUDGET} iterations, {N_SEEDS} seeds per cell; identical "
          f"extraction, critic and fault model")
    print("=" * 86)
    hdr = (f"{'N':>4} {'p':>5} | {'regime':<12} {'accept':>7} {'(theory)':>9} "
           f"{'edge F1':>8} {'calls/N':>8} {'iters':>6}")
    print(hdr)
    print("-" * 86)
    for n in FEEDER_SIZES:
        gt = scaled_feeder(n)
        raw = export_raw_observables(gt, heterogeneous=False)
        line_ids = [a["asset_id"] for a in raw["assets"]
                    if a["type"] == "overhead_line"]
        assert len(line_ids) == n
        tw0, is0 = build(raw, line_ids, set(line_ids))
        assert critic_evaluate(tw0, is0, gt, None)[0], "perfect twin must pass"
        for p in MISS_RATES:
            theory = {
                "pipeline": (1 - p) ** n,
                "blind": 1 - (1 - (1 - p) ** n) ** K_BUDGET,
                "crl": (1 - p ** K_BUDGET) ** n,
            }
            for regime in ("pipeline", "blind", "crl"):
                acc = f1s = calls = iters = 0.0
                for s in range(N_SEEDS):
                    rng = random.Random(1000003 * n + 7919 * s + int(p * 1e4))
                    ok, f1, c, it = run_regime(regime, raw, gt, line_ids, p, rng)
                    acc += ok; f1s += f1; calls += c; iters += it
                row = {
                    "n_lines": n, "miss_rate": p, "regime": regime,
                    "acceptance": acc / N_SEEDS,
                    "acceptance_theory": theory[regime],
                    "edge_f1": f1s / N_SEEDS,
                    "calls_per_asset": calls / N_SEEDS / n,
                    "iterations": iters / N_SEEDS,
                }
                rows.append(row)
                print(f"{n:>4} {p:>5.2f} | {regime:<12} {row['acceptance']:>7.3f} "
                      f"{row['acceptance_theory']:>9.3f} {row['edge_f1']:>8.3f} "
                      f"{row['calls_per_asset']:>8.2f} {row['iterations']:>6.2f}")
            print("-" * 86)
    out = os.path.join(_PKG, "results_crl_ablation.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out}   ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
