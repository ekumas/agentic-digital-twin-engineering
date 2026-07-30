# Agentic Digital Twin Engineering — Reproducibility Package

Reference implementation for the paper *"Agentic Digital Twin Engineering: A
CIM-Grounded Multi-Agent Framework for Autonomous Construction, Validation, and
Continuous Synchronisation of Power Distribution Digital Twins."*

**License:** MIT · **Language:** Python 3.10+ · **Backend:** OpenDSS (proof-of-concept)

> **Citation.** If you use this code, please cite the paper. A BibTeX entry will
> be added here on publication. Until then, cite the preprint / the accompanying
> article in *Advanced Engineering Informatics*.

The framework autonomously reconstructs an executable, CIM-grounded digital
twin of a power distribution feeder from heterogeneous engineering artefacts,
validates it against a withheld ground-truth model, repairs construction faults
through a collaborative reasoning loop, and re-synchronises the twin when the
physical system changes.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (only `OpenDSSDirect.py`; the LLM agent uses
  the standard library `urllib`, no extra SDK)
- For the E2 experiment only: an API key in the environment,
  `export ANTHROPIC_API_KEY=...` **or** `export OPENAI_API_KEY=...`
  (never hard-code the key; it is read from the environment and never logged).

## What maps to what in the paper

Each experiment is provided in two forms. The **standalone/** scripts bundle
every module into a single file (no package imports, no folder structure) and
are the easiest way to reproduce a result. The **module tree** (core/, backends/,
agents/, run_*.py) is the same code organised for reading and extension.

| Paper section | Result | Standalone script | Module runner |
|---|---|---|---|
| §5.1 (E1) | Construction fidelity — loader round-trip on a SMART-DS feeder | `standalone/E1_loader_roundtrip.py` | `python -m tests.test_smartds_loader` |
| §5.2 (E-CRL) | Collaborative reasoning loop vs. pipeline on an injected fault | `standalone/E_CRL_agentic_loop.py` | `python run_crl.py` |
| §5.3 (E2) | LLM necessity — rule-based vs. LLM, structured vs. heterogeneous | `standalone/E2_llm_necessity.py` | `python run_e2.py` |
| §5.4 (E3) | Synchronisation across a base→solar physical change | `standalone/E3_synchronisation.py` | `python run_e3.py` |

## Quick start (standalone — recommended)

```bash
pip install -r requirements.txt

# §5.2 — agentic loop repairs a fault a pipeline ships (no API key needed)
python standalone/E_CRL_agentic_loop.py

# §5.4 — twin detects a physical change and re-grounds (no API key needed)
python standalone/E3_synchronisation.py

# §5.3 — LLM necessity (needs an API key for the LLM rows; rule-based rows run without)
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY
python standalone/E2_llm_necessity.py

# §5.1 — loader round-trip.
#   With a real SMART-DS feeder downloaded locally:
python standalone/E1_loader_roundtrip.py --master ./smartds_data
#   Without one, it runs on a bundled SMART-DS-shaped synthetic feeder.
python standalone/E1_loader_roundtrip.py
```

## Expected results

- **E-CRL** — pipeline ships a broken twin (edge F1 0.88, 2 unresolved); the
  collaborative reasoning loop repairs it in 2 iterations (edge F1 1.00, MAE 0.000).
- **E3** — a static twin drifts (tracking MAE 0.0056 pu) after PV is added; the
  synchronised twin detects the change and re-grounds to 0.000 pu.
- **E2** — rule-based and LLM are both exact on structured input (edge F1 1.00);
  on heterogeneous input the rule-based baseline collapses (edge F1 0.71, MAE 0.88)
  while the LLM agent recovers full structure (edge F1 1.00, MAE 0.000).
- **E1** — on the real SFO feeder `p27uhs0_1247--p27udt1338` the twin is
  reconstructed with exact topology (1654/1654 buses) and converges; MV-level
  numerical calibration is the Calibration agent's scope (see paper §5.1).

## Dataset (for E1 on real data)

NREL SMART-DS, publicly available via the Open Energy Data Initiative. The paper
uses `SMART-DS/v1.0/2017/SFO/P27U/scenarios/base_timeseries/opendss/p27uhs0_1247`
(feeder `p27uhs0_1247--p27udt1338`). E-CRL, E2 and E3 run on a bundled
SMART-DS-shaped reference feeder and need no download.

## Layout

```
core/            CIM twin representation, SMART-DS loader, validation metrics, ground-truth builder
backends/        OpenDSS proof-of-concept realisation adapter
agents/          rule_based · llm_semantic (E2) · crl (E-CRL) · sync_agent (E3)
tests/           loader round-trip test (E1) and download-utility tests
run_e2/crl/e3.py module-tree experiment runners
standalone/      single-file, import-free versions of each experiment
```

## Notes

- The LLM performs *interpretation only* (recovering structure from messy
  input); all numerical power flow is executed by the deterministic OpenDSS
  backend. The framework is LLM-enabled, not LLM-dependent — the semantic-
  inference interface is provider-agnostic.
- OpenDSS is a proof-of-concept realisation backend; the contribution is the
  autonomous engineering of an executable twin, not simulator-specific files.
