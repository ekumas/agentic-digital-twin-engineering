# Agentic Digital Twin Engineering — Reproducibility Package

Reference implementation for the paper *"Autonomous Construction, Validation and
Continuous Synchronisation of Power Distribution Digital Twins: A
Standards-Grounded Multi-Agent Framework."*

**License:** MIT · **Language:** Python 3.10+ · **Backend:** OpenDSS (proof-of-concept)
· 

> **Citation.** If you use this code, please cite the paper. A BibTeX entry will
> be added here on publication.

## What this package shows, and on which feeder

The evaluation has two tiers. They answer different questions and run on
different feeders, so they are kept apart here exactly as in the paper.

| Tier | Question | Feeder | Is the model withheld from the agents? |
|---|---|---|---|
| 1 — representation and realisation fidelity (E1) | Can the CIM twin representation and the OpenDSS adapter carry a utility-scale feeder without loss? | NREL SMART-DS, SFO feeder `p27uhs0_1247--p27udt1338` (1 654 buses) | **No.** The published OpenDSS model is the input. It is lifted into the CIM representation, re-rendered and compared with the original (a round-trip test). |
| 2 — agent-driven construction (E-CRL, ablation, E2, E3) | Can agents build, repair and re-synchronise a twin from the inputs a utility actually holds? | Bundled reference feeder: radial 12.47 kV, 69 kV source, 10 MVA transformer, 8 line sections, 8 loads, 600 kvar capacitor | **Yes.** Agents receive only measurements, GIS-like asset records, a conductor catalogue and the source node. The executable model is used for scoring only. |

E1 does **not** test construction from withheld data. Agent-driven construction
of a utility-scale feeder from GIS layers with the model withheld is future work.

In tier 2 the asset records carry each element's end nodes, as GIS records do.
What the agents recover is the meaning of the fields, the element parameters,
the load model and a solvable twin.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (only `OpenDSSDirect.py`; the LLM agent uses
  the standard library `urllib`, no extra SDK)
- For the LLM rows of E2 only: an API key in the environment,
  `export ANTHROPIC_API_KEY=...` **or** `export OPENAI_API_KEY=...`
  (never hard-code the key; it is read from the environment and never logged).

## What maps to what in the paper

The **standalone/** scripts bundle every module into a single file and are the
easiest way to reproduce a result. The **module tree** (core/, backends/,
agents/, run_*.py) is the same code organised for reading and extension.

| Paper section | Result | Standalone script | Module runner |
|---|---|---|---|
| §5.1 (E1), Table 3, Fig. 2 | Round-trip fidelity on a SMART-DS feeder | `standalone/E1_loader_roundtrip.py` | `python -m tests.test_smartds_loader` |
| §5.2 (E-CRL), Table 4, Fig. 3 | Collaborative reasoning loop vs. pipeline on an injected fault | `standalone/E_CRL_agentic_loop.py` | `python run_crl.py` |
| §5.3 (E2), Table 5, Fig. 4 | LLM necessity: rule-based vs. LLM, structured vs. heterogeneous | `standalone/E2_llm_necessity.py` | `python run_e2.py` |
| §5.4 (E3), Table 6, Fig. 5 | Synchronisation across a base-to-solar physical change | `standalone/E3_synchronisation.py` | `python run_e3.py` |
| §5.5 (ablation), Table 7, Fig. 6 | Root-cause localisation vs. blind retry under a stochastic fault model | — | `python run_crl_ablation.py` |

## Quick start

```bash
pip install -r requirements.txt

# §5.2 — the loop repairs a fault a pipeline ships (no API key needed)
python standalone/E_CRL_agentic_loop.py

# §5.5 — ablation: pipeline vs blind retry vs loop (no API key needed, ~9 min)
python run_crl_ablation.py          # writes results_crl_ablation.csv

# §5.4 — twin detects a physical change and re-grounds (no API key needed)
python standalone/E3_synchronisation.py

# §5.3 — LLM necessity (LLM rows need an API key; rule-based rows run without)
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY
python standalone/E2_llm_necessity.py

# §5.1 — round trip.
#   With the SMART-DS feeder downloaded locally:
python standalone/E1_loader_roundtrip.py --master ./smartds_data
#   Without it, the script runs on a bundled SMART-DS-shaped synthetic feeder.
python standalone/E1_loader_roundtrip.py
```

## Expected results

- **E1 (round trip, SMART-DS).** The published model of feeder
  `p27uhs0_1247--p27udt1338` is lifted into the CIM representation and
  re-rendered. All 1 654 buses are recovered, the rendered twin converges, and it
  reproduces the original power flow: medium-voltage nodal voltage MAE is
  1.3e-4 pu (396 MV buses), 2.9e-4 pu on the 45 low-voltage 277 V buses, and
  total real-power losses agree to within 2.7 % (60.7 kW vs. 59.1 kW). The
  120/240 V split-phase customer service points are excluded from the MV figure
  (their per-unit voltages depend on a low-voltage base assignment outside the
  distribution model; their volts track the original to about 2 %). Diagnostic
  flags (`--vseviye`, `--volt`, `--kayip`, `--kaynak`, `--yuk`) break the
  round-trip error down by voltage level, element type and cause.
- **E-CRL (reference feeder).** The pipeline ships a broken twin (edge F1 0.88,
  2 unresolved, voltage MAE 0.594 pu); the collaborative reasoning loop repairs
  it in 2 iterations (edge F1 1.00, MAE 0.000). In this demonstration the fault
  is deterministic and the targeted re-extraction succeeds by construction; the
  ablation removes that assumption.
- **Ablation (reference feeder, 8 / 32 / 128 line sections).** Every extraction
  call, targeted ones included, misses a line with probability p; budget of 3
  iterations; 2 000 seeded runs per cell. Acceptance rate within the budget:

  | Line sections | p | Pipeline | Blind retry | Loop |
  |---|---|---|---|---|
  | 8 | 0.05 | 0.650 | 0.958 | 0.999 |
  | 32 | 0.05 | 0.194 | 0.483 | 0.996 |
  | 128 | 0.05 | 0.001 | 0.004 | 0.982 |
  | 128 | 0.10 | 0.000 | 0.000 | 0.870 |
  | 128 | 0.30 | 0.000 | 0.000 | 0.029 |

  The loop needs 1.02–1.39 extraction calls per asset against up to 3.00 for
  blind retry. Simulated rates match the closed forms `1-(1-(1-p)^N)^K` (blind
  retry) and `(1-p^K)^N` (loop) to within 0.02. For the 32- and 128-section
  feeders, total load and trunk length are held at the 8-section baseline so the
  feeder stays physically feasible. This is a controlled simulation under a
  stated fault model; it isolates localisation from retrying and does not
  compare several agents with a single agent.
- **E2 (reference feeder).** Rule-based and LLM are both exact on structured
  input (edge F1 1.00). On heterogeneous input the rule-based baseline collapses
  (edge F1 0.71, MAE 0.88 pu, 5 unresolved) while the LLM agent recovers full
  structure (edge F1 1.00, MAE 0.000). Default model: `claude-sonnet-4-5`
  (override with `ADTE_LLM_MODEL`), single-turn call, provider default sampling
  settings. Prompts are in `agents/llm_semantic.py`.
- **E3 (reference feeder).** Three 800 kW PV units are added at peak generation.
  A static twin drifts (tracking MAE 0.0056 pu); the synchronised twin detects
  the divergence (threshold 0.002 pu) and re-grounds to 0.000 pu.

Values shown as 0.000 are rounded to three decimals.

## Dataset (for E1 on SMART-DS)

NREL SMART-DS, publicly available via the Open Energy Data Initiative. The paper
uses `SMART-DS/v1.0/2017/SFO/P27U/scenarios/base_timeseries/opendss/p27uhs0_1247`
(feeder `p27uhs0_1247--p27udt1338`). SMART-DS is a synthetic dataset validated
against real utility feeders. E-CRL, the ablation, E2 and E3 run on the bundled
reference feeder and need no download.

## Layout

```
core/                  CIM twin representation, SMART-DS loader, validation metrics, reference-feeder builder
backends/              OpenDSS proof-of-concept realisation adapter
agents/                rule_based · llm_semantic (E2) · crl (E-CRL) · sync_agent (E3)
tests/                 loader round-trip test (E1) and download-utility tests
run_crl.py             E-CRL
run_crl_ablation.py    ablation: pipeline vs blind retry vs loop
run_e2.py, run_e3.py   E2, E3
standalone/            single-file, import-free versions of E1, E-CRL, E2, E3
```

## Notes

- The LLM performs *interpretation only* (recovering structure from messy
  input); all numerical power flow is executed by the deterministic OpenDSS
  backend. The semantic-inference interface is provider-agnostic.
- OpenDSS is a proof-of-concept realisation backend; the twin is defined by the
  CIM-grounded representation, not by simulator-specific files.
