"""
LLM semantic-inference agent — the core of the "agentic" contribution (E2).

Where the rule-based baseline (agents/rule_based.py) collapses on heterogeneous
input — because it needs exact column names and complete structured records —
this agent recovers the missing structure by *interpreting* free-text
maintenance notes and renamed measurement columns, then emitting CIM-shaped
structured output.

Key design points that map onto the paper's claims:
  * LLM-enabled, not LLM-dependent: the provider is chosen at run time from the
    environment (ANTHROPIC_API_KEY or OPENAI_API_KEY); any capable model fits.
  * CIM grounding constrains the LLM: we force JSON output against an explicit
    schema and validate it, rather than accepting free prose. This is the
    concrete mechanism behind "CIM grounding reduces hallucination".
  * The LLM interprets, it does not compute: it recovers conductor class,
    length, ratings and column meanings; all numerical solving stays in OpenDSS.

The API key is read from the environment and never logged. Set one of:
    export ANTHROPIC_API_KEY=...      # uses Claude
    export OPENAI_API_KEY=...         # uses GPT
"""
import json
import os
import re
import urllib.request
import urllib.error

from core.twin import (
    TwinRepresentation, ConnectivityNode, PerLengthPhaseImpedance,
    ACLineSegment, PowerTransformer, EnergyConsumer, EnergySource,
    LinearShuntCompensator, PowerElectronicsConnection, LoadShape,
)

DEFAULT_MV_KV = 12.47


# --------------------------------------------------------------------------- #
# Provider abstraction — chosen from the environment.                         #
# --------------------------------------------------------------------------- #
class LLMUnavailable(RuntimeError):
    pass


def _detect_provider():
    """Return (provider, model, key) from the environment, or raise."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("anthropic",
                os.environ.get("ADTE_LLM_MODEL", "claude-sonnet-4-5"),
                os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("OPENAI_API_KEY"):
        return ("openai",
                os.environ.get("ADTE_LLM_MODEL", "gpt-4o"),
                os.environ["OPENAI_API_KEY"])
    raise LLMUnavailable(
        "No API key found. Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your "
        "environment (do not hard-code it). The rule-based baseline still runs "
        "without a key; only the LLM agent needs one.")


def _call_llm(system: str, user: str, max_tokens: int = 8000) -> str:
    """Single-turn completion. Returns raw text. Provider-agnostic."""
    provider, model, key = _detect_provider()

    if provider == "anthropic":
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({
                "model": model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            })
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []))

    else:  # openai
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({
                "model": model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {key}",
            })
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]


def _extract_json(text: str):
    """Pull the first JSON object out of a model response, tolerating fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in response")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


# --------------------------------------------------------------------------- #
# The CIM-constrained extraction prompt.                                      #
# --------------------------------------------------------------------------- #
_SYSTEM = """You are a power-distribution data engineer. You convert messy, \
heterogeneous utility records — free-text maintenance notes, renamed telemetry \
columns, partial asset rows — into a strict structured form for building a \
CIM-grounded network model.

You NEVER invent values that are not supported by the input. If a value is not \
recoverable, use null. You only INTERPRET what is present (e.g. read a \
conductor class or a span length out of a maintenance note, or recognise that \
a column named 'p_val' is real power in kW).

Return ONLY a JSON object, no prose, with this exact shape:
{
  "assets": [
    {"asset_id": str,
     "type": "overhead_line"|"substation_transformer"|"capacitor_bank"|"solar_pv"|"service_point",
     "from": str|null, "to": str|null, "at": str|null,
     "conductor_class": str|null, "length_km": number|null,
     "rating_mva": number|null, "hv_kv": number|null, "lv_kv": number|null,
     "kvar": number|null, "rating_kw": number|null}
  ],
  "measurement_columns": {"meter_id": str|null, "kW": str|null,
                          "kVAR": str|null, "hour": str|null},
  "notes_resolved": [ {"asset_id": str, "recovered": [str]} ]
}
The measurement_columns map your best guess of which ACTUAL column name in the \
data carries each quantity (e.g. {"kW": "p_val"})."""


def _build_user_prompt(raw: dict) -> str:
    assets = json.dumps(raw.get("assets", []), ensure_ascii=False)
    notes = json.dumps(raw.get("notes", []), ensure_ascii=False)
    cols = list(raw["measurements"][0].keys()) if raw.get("measurements") else []
    return (f"Asset rows (some fields may be missing):\n{assets}\n\n"
            f"Free-text maintenance notes:\n{notes}\n\n"
            f"Measurement columns present in the telemetry: {cols}\n\n"
            f"Recover every asset's missing fields from the notes, and map the "
            f"measurement columns. Return the JSON object only.")


# --------------------------------------------------------------------------- #
# Public API — mirrors agents.rule_based.reconstruct                          #
# --------------------------------------------------------------------------- #
def reconstruct(raw: dict) -> tuple:
    """
    Return (twin, issues), same contract as the rule-based baseline.

    The LLM recovers structure from notes/renamed columns; we then build the
    twin deterministically from the LLM's *structured* output and validate it.
    """
    t = TwinRepresentation(name="llm_recon")
    issues = []

    # conductor catalogue is a clean lookup in both conditions
    for name, z in raw.get("conductor_catalogue", {}).items():
        t.add(PerLengthPhaseImpedance(name, r1_ohm_per_km=z["r1"],
                                      x1_ohm_per_km=z["x1"], r0_ohm_per_km=z["r0"],
                                      x0_ohm_per_km=z["x0"]), "llm_semantic")

    src = raw.get("source_hint")
    if src:
        t.add(ConnectivityNode(src["node"], src["kv"]), "llm_semantic")
        t.add(EnergySource("src", src["node"], src["kv"]), "llm_semantic")
    else:
        issues.append("no source identified")

    # --- the semantic-inference step -------------------------------------- #
    try:
        raw_text = _call_llm(_SYSTEM, _build_user_prompt(raw))
        if os.environ.get("ADTE_DEBUG"):
            
            print("\n----- LLM RAW RESPONSE (first 1200 chars) -----",
                  file=sys.stderr)
            print(raw_text[:1200], file=sys.stderr)
            print("----- END -----\n", file=sys.stderr)
        parsed = _extract_json(raw_text)
        if os.environ.get("ADTE_DEBUG"):
            
            n_assets = len(parsed.get("assets", []))
            types = {}
            for a in parsed.get("assets", []):
                types[a.get("type")] = types.get(a.get("type"), 0) + 1
            print(f"[debug] parsed {n_assets} assets: {types}", file=sys.stderr)
            print(f"[debug] measurement_columns: "
                  f"{parsed.get('measurement_columns')}", file=sys.stderr)
    except LLMUnavailable as e:
        raise
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as e:
        issues.append(f"LLM inference failed: {e}")
        return t, issues

    def ensure_node(n, kv=DEFAULT_MV_KV):
        if n and n not in t.nodes:
            t.add(ConnectivityNode(n, kv), "llm_semantic")

    for a in parsed.get("assets", []):
        typ = a.get("type")
        try:
            if typ == "substation_transformer":
                if a.get("rating_mva") is None or a.get("hv_kv") is None:
                    issues.append(f"{a['asset_id']}: transformer rating unrecovered")
                    continue
                ensure_node(a.get("from"), a["hv_kv"])
                ensure_node(a.get("to"), a.get("lv_kv") or DEFAULT_MV_KV)
                t.add(PowerTransformer(a["asset_id"], a["from"], a["to"],
                                       a["rating_mva"], a["hv_kv"],
                                       a.get("lv_kv") or DEFAULT_MV_KV), "llm_semantic")
            elif typ == "overhead_line":
                if not a.get("conductor_class") or a.get("length_km") is None:
                    issues.append(f"{a['asset_id']}: impedance/length unrecovered")
                    continue
                ensure_node(a.get("from")); ensure_node(a.get("to"))
                t.add(ACLineSegment(a["asset_id"], a["from"], a["to"],
                                    a["length_km"], a["conductor_class"]), "llm_semantic")
            elif typ == "capacitor_bank":
                ensure_node(a.get("at"))
                t.add(LinearShuntCompensator(a["asset_id"], a["at"], a.get("kvar", 0)),
                      "llm_semantic")
            elif typ == "solar_pv":
                ensure_node(a.get("at"))
                t.add(PowerElectronicsConnection(a["asset_id"], a["at"],
                                                 a.get("rating_kw", 0), "pv_daily"),
                      "llm_semantic")
        except (KeyError, TypeError) as e:
            issues.append(f"{a.get('asset_id', '?')}: malformed asset ({e})")

    # --- measurement columns recovered by the LLM ------------------------- #
    cmap = parsed.get("measurement_columns", {})
    meas = raw.get("measurements", [])
    id_c, p_c, q_c, h_c = (cmap.get("meter_id"), cmap.get("kW"),
                           cmap.get("kVAR"), cmap.get("hour"))
    if meas and all([id_c, p_c, q_c, h_c]) and id_c in meas[0]:
        series = {}
        for m in meas:
            series.setdefault(m[id_c], {})[m[h_c]] = (m[p_c], m[q_c])
        sp_node = {a["asset_id"]: a.get("at") for a in parsed.get("assets", [])
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
            vals = [byh[h][0] / peak_p if peak_p else 0.0 for h in sorted(byh)]
            t.add(LoadShape(shape_id, 1.0, [round(v, 4) for v in vals]), "llm_semantic")
            t.add(EnergyConsumer(mid, node, peak_p, peak_q, shape_id), "llm_semantic")
    else:
        issues.append("measurement columns not recovered")

    if any(pv for pv in t.pv.values()):
        t.add(LoadShape("pv_daily", 1.0,
                        [0.0]*6 + [0.2,0.45,0.7,0.9,1.0,1.0,0.95,0.8,0.6,0.35,0.15,0.05] + [0.0]*6),
              "llm_semantic")

    return t, issues


def provider_status() -> str:
    """Human-readable note on which provider will be used (no key leakage)."""
    try:
        provider, model, _ = _detect_provider()
        return f"LLM provider: {provider} (model {model})"
    except LLMUnavailable as e:
        return f"LLM unavailable: {e}"
