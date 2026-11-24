"""Instance & log analytics engine + cross-project RAG bridge.

Shares the exact JSON contract (`DoughLogEntry`, `StoredDoughProfile`,
`DoughPhaseLogEntry`, `DoughTaskOrderItem`, `DoughExperimentMeta`,
`KNOWN_INSTANCE_VARIABLES`) with `v0-yeast-development-app`, while bridging
empirical dough journeys directly into `cards.jsonl` / `candidates.jsonl`
and `validate.evaluate_truth`.
"""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ingest import (
    CANDIDATES_PATH,
    CARDS_PATH,
    commit_candidates,
    is_jsonl_note,
    load_card_ids,
    normalize_card,
    schema_enums,
    write_candidates,
)

PROFILES_PATH = "instance_profiles.jsonl"
LOGS_PATH = "instance_logs.jsonl"

YEAST_BAKER_PCT: Dict[str, Dict[str, float]] = {
    "instant": {"low": 0.25, "standard": 0.6, "high": 1.2},
    "active-dry": {"low": 0.35, "standard": 0.75, "high": 1.5},
    "fresh": {"low": 0.75, "standard": 1.8, "high": 3.0},
    "sourdough": {"low": 10.0, "standard": 20.0, "high": 30.0},
}

FLOUR_FERMENTABLE_FRAC: Dict[str, float] = {
    "00": 0.024,
    "bread": 0.028,
    "all-purpose": 0.026,
    "whole-wheat": 0.032,
    "rye": 0.036,
    "gluten-free": 0.022,
}

FLOUR_LABELS: Dict[str, str] = {
    "00": "Tipo 00 · 12.5%",
    "bread": "Bread Flour · 12.7%",
    "all-purpose": "All-Purpose · 11.7%",
    "whole-wheat": "Whole Wheat · 13.8%",
    "rye": "Dark Rye · 11.0%",
    "gluten-free": "Gluten-Free Blend",
}

YEAST_LABELS: Dict[str, str] = {
    "instant": "Instant Dry Yeast",
    "active-dry": "Active Dry Yeast",
    "fresh": "Fresh Cake Yeast",
    "sourdough": "Sourdough Levain",
}

KNOWN_INSTANCE_VARIABLES: Tuple[Dict[str, Any], ...] = (
    {
        "key": "hydrationPct",
        "label": "Hydration",
        "shortLabel": "Hydration %",
        "phase": "mixing",
        "kind": "numeric",
        "unit": "%",
        "tolerance": 0.4,
        "ragVariables": ["hydration", "extensibility", "tactility"],
        "ragPhase": "mixing",
    },
    {
        "key": "flourTypeKey",
        "label": "Flour base",
        "shortLabel": "Flour",
        "phase": "mixing",
        "kind": "categorical",
        "unit": "",
        "tolerance": 0,
        "ragVariables": ["flour_protein", "gluten", "kneading"],
        "ragPhase": "mixing",
    },
    {
        "key": "yeastKey",
        "label": "Leavening type",
        "shortLabel": "Yeast",
        "phase": "mixing",
        "kind": "categorical",
        "unit": "",
        "tolerance": 0,
        "ragVariables": ["leavening", "fermentation_time"],
        "ragPhase": "mixing",
    },
    {
        "key": "yeastAmountPct",
        "label": "Yeast / starter %",
        "shortLabel": "Yeast %",
        "phase": "mixing",
        "kind": "numeric",
        "unit": "%",
        "tolerance": 0.05,
        "ragVariables": ["leavening", "fermentation_time"],
        "ragPhase": "mixing",
    },
    {
        "key": "prefermentShare",
        "label": "Preferment flour",
        "shortLabel": "Preferment %",
        "phase": "mixing",
        "kind": "numeric",
        "unit": "%",
        "tolerance": 1.0,
        "ragVariables": ["leavening", "extensibility", "fermentation_time"],
        "ragPhase": "mixing",
    },
    {
        "key": "phaseTempFRest",
        "label": "Rest / autolyse temp",
        "shortLabel": "Rest °F",
        "phase": "resting",
        "kind": "numeric",
        "unit": "°F",
        "tolerance": 1.0,
        "ragVariables": ["temperature", "kneading", "gluten"],
        "ragPhase": "kneading",
    },
    {
        "key": "phaseTempFFerment",
        "label": "Bulk ferment temp",
        "shortLabel": "Ferment °F",
        "phase": "fermentation",
        "kind": "numeric",
        "unit": "°F",
        "tolerance": 1.0,
        "ragVariables": ["temperature", "fermentation_time", "cold_ferment"],
        "ragPhase": "bulk_fermentation",
    },
    {
        "key": "phaseTempFProof",
        "label": "Final proof temp",
        "shortLabel": "Proof °F",
        "phase": "fermentation",
        "kind": "numeric",
        "unit": "°F",
        "tolerance": 1.0,
        "ragVariables": ["temperature", "fermentation_time"],
        "ragPhase": "proofing",
    },
    {
        "key": "fridgePhase",
        "label": "Cold retard phase",
        "shortLabel": "Fridge",
        "phase": "fermentation",
        "kind": "categorical",
        "unit": "",
        "tolerance": 0,
        "ragVariables": ["cold_ferment", "fermentation_time", "extensibility"],
        "ragPhase": "bulk_fermentation",
    },
    {
        "key": "ambientTempF",
        "label": "Observed ambient",
        "shortLabel": "Ambient °F",
        "phase": "resting",
        "kind": "numeric",
        "unit": "°F",
        "tolerance": 1.0,
        "ragVariables": ["temperature", "fermentation_time"],
        "ragPhase": "bulk_fermentation",
    },
    {
        "key": "humidityPct",
        "label": "Observed humidity",
        "shortLabel": "Humidity %",
        "phase": "resting",
        "kind": "numeric",
        "unit": "%",
        "tolerance": 2.0,
        "ragVariables": ["humidity", "hydration", "extensibility"],
        "ragPhase": "proofing",
    },
    {
        "key": "timelineTimeMin",
        "label": "Elapsed dough clock",
        "shortLabel": "Clock min",
        "phase": "fermentation",
        "kind": "numeric",
        "unit": "m",
        "tolerance": 10.0,
        "ragVariables": ["fermentation_time", "extensibility"],
        "ragPhase": "bulk_fermentation",
    },
    {
        "key": "maxTimeMin",
        "label": "Planned horizon",
        "shortLabel": "Horizon min",
        "phase": "fermentation",
        "kind": "numeric",
        "unit": "m",
        "tolerance": 15.0,
        "ragVariables": ["fermentation_time"],
        "ragPhase": "bulk_fermentation",
    },
)

VARIABLE_BY_KEY: Dict[str, Dict[str, Any]] = {v["key"]: v for v in KNOWN_INSTANCE_VARIABLES}

FALLBACK_CONFIG: Dict[str, Any] = {
    "flourMode": "single",
    "selectedFlour": "00",
    "activeBlend": [],
    "blendName": None,
    "selectedYeast": "instant",
    "yeastAmount": "standard",
    "prefermentType": "none",
    "prefermentFlourPercent": 0,
    "prefermentHydrationPercent": 100,
    "prefermentMaturity": 0.6,
    "hydrationTarget": 64,
    "maxTime": 360,
    "timeStep": 15,
    "phaseTempF": {"rest": 70, "ferment": 68, "proof": 72},
    "fridgePhase": "none",
}


def _round1(val: float) -> float:
    return round(float(val) * 10.0) / 10.0


def _stable_short_code(seed: str, explicit: Optional[str] = None) -> str:
    if explicit and explicit.startswith("#"):
        return explicit
    # Deterministic FNV-1a 32-bit hash matching TypeScript `stableShortCode`.
    h = 2166136261
    for ch in seed:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    if h >= 0x80000000:
        h = abs(h - 0x100000000)
    return f"#{(h % 9000) + 1000}"


def _style_slug_for_name(name: str) -> str:
    low = (name or "").lower()
    if "neapolitan" in low:
        return "neapolitan"
    if "ny" in low or "slice" in low:
        return "ny_slice"
    if "sicilian" in low or "pan" in low:
        return "sicilian"
    if "taglio" in low:
        return "al_taglio"
    if "sourdough" in low:
        return "sourdough"
    return "general"


def _effective_phase_temp(phase: str, config: Dict[str, Any]) -> float:
    if config.get("fridgePhase") == phase:
        return 38.0
    temps = config.get("phaseTempF") or {}
    val = temps.get(phase)
    return float(val) if isinstance(val, (int, float)) else 70.0


def _yeast_baker_pct(config: Dict[str, Any]) -> float:
    yeast = str(config.get("selectedYeast") or "instant")
    amt = str(config.get("yeastAmount") or "standard")
    return YEAST_BAKER_PCT.get(yeast, YEAST_BAKER_PCT["instant"]).get(amt, 0.6)


def resolve_outcome_positive(log: Dict[str, Any]) -> Optional[bool]:
    rating = log.get("outcomeRating")
    if rating == "good":
        return True
    if rating == "bad":
        return False
    res = str(log.get("result") or "").strip()
    if not res or res == "—":
        return None
    return bool(re.match(r"^(great|good|excellent|success|perfect|love|amazing|blistered|airy|clean)", res, re.I))


def resolve_log_config(log: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    snap = log.get("profileConfigSnapshot")
    base = dict(fallback)
    if isinstance(snap, dict):
        base.update(snap)
        base["phaseTempF"] = {
            **(fallback.get("phaseTempF") or {"rest": 70, "ferment": 68, "proof": 72}),
            **(snap.get("phaseTempF") or {}),
        }
    else:
        base["phaseTempF"] = dict(fallback.get("phaseTempF") or {"rest": 70, "ferment": 68, "proof": 72})
        if log.get("flour"):
            base["selectedFlour"] = log["flour"]
        if log.get("flourMode"):
            base["flourMode"] = log["flourMode"]
        if log.get("yeast"):
            base["selectedYeast"] = log["yeast"]
    if isinstance(log.get("hydration"), (int, float)):
        base["hydrationTarget"] = float(log["hydration"])
    return base


def extract_known_variables(config: Dict[str, Any], log: Dict[str, Any]) -> Dict[str, Any]:
    flour_mode = config.get("flourMode") or "single"
    flour_key = (
        f"blend:{config.get('blendName') or 'custom'}"
        if flour_mode == "blend"
        else str(config.get("selectedFlour") or log.get("flour") or "00")
    )
    pref_type = config.get("prefermentType") or "none"
    pref_pct = float(config.get("prefermentFlourPercent") or 0) if pref_type != "none" else 0.0
    hydr = log.get("hydration") if isinstance(log.get("hydration"), (int, float)) else config.get("hydrationTarget", 64)
    return {
        "hydrationPct": _round1(float(hydr)),
        "flourTypeKey": flour_key,
        "yeastKey": str(config.get("selectedYeast") or log.get("yeast") or "instant"),
        "yeastAmountPct": _round1(_yeast_baker_pct(config)),
        "prefermentShare": round(pref_pct),
        "phaseTempFRest": round(_effective_phase_temp("rest", config)),
        "phaseTempFFerment": round(_effective_phase_temp("ferment", config)),
        "phaseTempFProof": round(_effective_phase_temp("proof", config)),
        "fridgePhase": str(config.get("fridgePhase") or "none"),
        "ambientTempF": _round1(float(log["ambientTempF"])) if isinstance(log.get("ambientTempF"), (int, float)) else None,
        "humidityPct": _round1(float(log["humidityPct"])) if isinstance(log.get("humidityPct"), (int, float)) else None,
        "timelineTimeMin": round(float(log["timelineTimeMin"])) if isinstance(log.get("timelineTimeMin"), (int, float)) else None,
        "maxTimeMin": round(float(config.get("maxTime") or 360)),
    }


def format_variable_value(defn: Dict[str, Any], value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        if not math.isfinite(num):
            return "—"
        txt = str(int(num)) if num.is_integer() else f"{num:.1f}"
        return f"{txt}{defn['unit']}"
    if value == "none":
        return "None"
    return str(value).replace("blend:", "Blend · ")


def compute_stoichiometry(config: Dict[str, Any], rise: float, base_flour_g: int = 500) -> Tuple[Dict[str, float], Dict[str, float]]:
    hydr = float(config.get("hydrationTarget") or 64)
    water_g = round(base_flour_g * hydr / 100.0, 1)
    yeast_pct = _yeast_baker_pct(config)
    yeast_g = round(base_flour_g * yeast_pct / 100.0, 1)
    salt_g = round(base_flour_g * 0.025, 1)

    flour_id = str(config.get("selectedFlour") or "00")
    ferm_frac = FLOUR_FERMENTABLE_FRAC.get(flour_id, 0.026)
    if config.get("flourMode") == "blend" and config.get("activeBlend"):
        blend = config["activeBlend"]
        tot = sum(float(b.get("percentage") or 0) for b in blend) or 100.0
        ferm_frac = sum(
            FLOUR_FERMENTABLE_FRAC.get(str(b.get("flourId")), 0.026) * (float(b.get("percentage") or 0) / tot)
            for b in blend
        )

    pref_share = (
        min(1.0, max(0.0, float(config.get("prefermentFlourPercent") or 0) / 100.0))
        if (config.get("prefermentType") or "none") != "none"
        else 0.0
    )
    maturity = min(1.0, max(0.0, float(config.get("prefermentMaturity") or 0.6)))
    sugar_pool = base_flour_g * ferm_frac * (1.0 - pref_share * maturity)

    conv = min(1.0, max(0.0, float(rise) / 100.0))
    sugar_consumed = round(sugar_pool * conv, 2)
    co2_g = round(sugar_consumed * (88.02 / 180.16), 2)
    ethanol_g = round(sugar_consumed * (92.14 / 180.16), 2)
    co2_ppm = round(420 + (co2_g / 6.8) * 3200)

    batch = {
        "flourGrams": base_flour_g,
        "waterGrams": water_g,
        "yeastGrams": yeast_g,
        "saltGrams": salt_g,
        "fermentableSugarGrams": round(sugar_pool, 2),
    }
    sample = {
        "conversionFrac": round(conv, 3),
        "sugarConsumedG": sugar_consumed,
        "sugarRemainingG": round(max(0.0, sugar_pool - sugar_consumed), 2),
        "co2ProducedG": co2_g,
        "ethanolProducedG": ethanol_g,
        "co2HeadspacePpm": co2_ppm,
    }
    return batch, sample


def build_ball_composition(config: Dict[str, Any], latest_rise: float = 68.0) -> Dict[str, Any]:
    base_flour = 500
    batch, sample = compute_stoichiometry(config, latest_rise, base_flour)
    hydr = float(config.get("hydrationTarget") or 64)
    yeast_pct = _round1(_yeast_baker_pct(config))
    pref_type = str(config.get("prefermentType") or "none")
    pref_pct = round(float(config.get("prefermentFlourPercent") or 0)) if pref_type != "none" else 0

    flour_mode = config.get("flourMode") or "single"
    if flour_mode == "blend" and config.get("activeBlend"):
        flour_line = f"{config.get('blendName') or 'Custom Blend'} · mixed"
    else:
        flour_id = str(config.get("selectedFlour") or "00")
        flour_line = FLOUR_LABELS.get(flour_id, flour_id)

    yeast_id = str(config.get("selectedYeast") or "instant")
    yeast_line = YEAST_LABELS.get(yeast_id, yeast_id)

    ingredients: List[Dict[str, Any]] = []
    if flour_mode == "blend" and config.get("activeBlend"):
        for row in config["activeBlend"]:
            pct = float(row.get("percentage") or 0)
            fid = str(row.get("flourId") or "bread")
            ingredients.append(
                {
                    "name": FLOUR_LABELS.get(fid, fid).split(" · ")[0],
                    "amount": round(base_flour * pct / 100.0),
                    "unit": "g",
                    "bakerPercent": pct,
                }
            )
    else:
        fid = str(config.get("selectedFlour") or "00")
        ingredients.append(
            {
                "name": FLOUR_LABELS.get(fid, fid).split(" · ")[0],
                "amount": base_flour,
                "unit": "g",
                "bakerPercent": 100.0,
            }
        )

    ingredients.extend(
        [
            {"name": "Water", "amount": round(batch["waterGrams"]), "unit": "g", "bakerPercent": _round1(hydr)},
            {"name": yeast_line, "amount": round(batch["yeastGrams"], 1), "unit": "g", "bakerPercent": yeast_pct},
            {"name": "Fine Sea Salt", "amount": round(batch["saltGrams"], 1), "unit": "g", "bakerPercent": 2.5},
        ]
    )
    if pref_pct > 0:
        ingredients.append(
            {
                "name": f"Preferment ({pref_type})",
                "amount": round(base_flour * pref_pct / 100.0),
                "unit": "g flour share",
                "bakerPercent": pref_pct,
            }
        )

    total_g = round(base_flour + batch["waterGrams"] + batch["yeastGrams"] + batch["saltGrams"])
    return {
        "baseFlourGrams": base_flour,
        "totalDoughGrams": total_g,
        "ballCount250g": max(1, round(total_g / 250)),
        "hydrationPct": _round1(hydr),
        "yeastBakerPct": yeast_pct,
        "saltBakerPct": 2.5,
        "prefermentPct": pref_pct,
        "flourLine": flour_line,
        "yeastLine": yeast_line,
        "ingredients": ingredients,
        "batch": batch,
        "sampleAtLatestRise": sample,
    }


def compute_trial_convergence_score(log: Dict[str, Any], config: Dict[str, Any]) -> int:
    outcome = resolve_outcome_positive(log)
    outcome_pts = 52 if outcome is True else (14 if outcome is False else 32)
    rise = float(log.get("rise") if isinstance(log.get("rise"), (int, float)) else 55)
    rise_pts = max(0, round(33 - abs(rise - 75.0) * 0.75))
    target_h = float(config.get("hydrationTarget") or 64)
    logged_h = float(log.get("hydration") if isinstance(log.get("hydration"), (int, float)) else target_h)
    align_pts = max(0, round(15 - abs(logged_h - target_h) * 2.0))
    return int(max(5, min(100, outcome_pts + rise_pts + align_pts)))


def derive_trial_phases(
    log: Dict[str, Any],
    config: Dict[str, Any],
    short_code: str,
    trial_number: int,
) -> Dict[str, Dict[str, Any]]:
    max_time = float(config.get("maxTime") or 360)
    rest_min = max(15, round(max_time * 0.12))
    ferment_min = max(30, round((float(log.get("timelineTimeMin") or max_time)) - rest_min))
    default_ref = (
        ((log.get("experimentMeta") or {}).get("objectRef") or "").strip()
        or f"{short_code} · Ball T{trial_number}"
    )

    phase_logs = log.get("phaseLogs") or {}
    mix_saved = phase_logs.get("mixing") or {}
    rest_saved = phase_logs.get("resting") or {}
    ferm_saved = phase_logs.get("fermentation") or {}

    hydr = _round1(float(log.get("hydration") or config.get("hydrationTarget") or 64))
    rise = round(float(log.get("rise") or 55))
    rest_temp = round(float(rest_saved.get("tempF") or _effective_phase_temp("rest", config)))
    ferm_temp = round(float(ferm_saved.get("tempF") or _effective_phase_temp("ferment", config)))

    return {
        "mixing": {
            "phase": "mixing",
            "label": "Mixing",
            "shapeHint": "square",
            "completionPct": mix_saved.get("completionPct") if mix_saved.get("completionPct") is not None else hydr,
            "tempF": round(float(mix_saved.get("tempF") or log.get("ambientTempF") or rest_temp)),
            "durationMin": int(mix_saved.get("durationMin") or 15),
            "feedback": (mix_saved.get("feedback") or "").strip()
            or f"{hydr}% hydration incorporated cleanly; Mix = 0 baseline set.",
            "objectRef": (mix_saved.get("objectRef") or "").strip() or default_ref,
            "metricBadge": f"{hydr}% hydr",
        },
        "resting": {
            "phase": "resting",
            "label": "Resting",
            "shapeHint": "rectangle",
            "completionPct": rest_saved.get("completionPct") if rest_saved.get("completionPct") is not None else 72,
            "tempF": rest_temp,
            "durationMin": int(rest_saved.get("durationMin") or rest_min),
            "feedback": (rest_saved.get("feedback") or "").strip()
            or (log.get("method") if log.get("method") and log.get("method") != "—" else "Bench rest & fold checkpoint"),
            "objectRef": (rest_saved.get("objectRef") or "").strip() or default_ref,
            "metricBadge": f"{rest_temp}°F · {int(rest_saved.get('durationMin') or rest_min)}m",
        },
        "fermentation": {
            "phase": "fermentation",
            "label": "Fermentation",
            "shapeHint": "trapezoid",
            "completionPct": ferm_saved.get("completionPct") if ferm_saved.get("completionPct") is not None else rise,
            "tempF": ferm_temp,
            "durationMin": int(ferm_saved.get("durationMin") or ferment_min),
            "feedback": (ferm_saved.get("feedback") or "").strip()
            or " — ".join(p for p in [str(log.get("fermentation") or "").strip(), str(log.get("result") or "").strip()] if p)
            or f"{rise}% rise at {log.get('stage') or 'bulk'}",
            "objectRef": (ferm_saved.get("objectRef") or "").strip() or default_ref,
            "metricBadge": f"{rise}% rise · {ferm_temp}°F",
        },
    }


def derive_task_orders(config: Dict[str, Any], latest_log: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    custom = ((latest_log or {}).get("experimentMeta") or {}).get("taskOrders")
    if isinstance(custom, list) and custom:
        return sorted(custom, key=lambda item: int(item.get("order") or 0))

    max_time = int(config.get("maxTime") or 360)
    pref_type = str(config.get("prefermentType") or "none")
    fridge = str(config.get("fridgePhase") or "none")
    steps: List[Dict[str, Any]] = []
    order = 1

    if pref_type != "none" and float(config.get("prefermentFlourPercent") or 0) > 0:
        steps.append(
            {
                "id": f"ord-{order}",
                "order": order,
                "phase": "preferment",
                "label": f"Build {pref_type} ({int(config.get('prefermentFlourPercent') or 20)}% flour share)",
                "targetMin": -480,
                "completed": True,
            }
        )
        order += 1

    steps.extend(
        [
            {
                "id": f"ord-{order}",
                "order": order,
                "phase": "mixing",
                "label": f"Mix flour + {config.get('hydrationTarget', 64)}% water + salt (Mix = 0m)",
                "targetMin": 0,
                "completed": latest_log is not None,
            },
            {
                "id": f"ord-{order + 1}",
                "order": order + 1,
                "phase": "resting",
                "label": f"Autolyse / rest at {round(_effective_phase_temp('rest', config))}°F + windowpane check",
                "targetMin": 30,
                "completed": latest_log is not None,
            },
            {
                "id": f"ord-{order + 2}",
                "order": order + 2,
                "phase": "fermentation",
                "label": (
                    f"Cold retard ({fridge} at 38°F)"
                    if fridge != "none"
                    else f"Bulk ferment at {round(_effective_phase_temp('ferment', config))}°F with coil folds"
                ),
                "targetMin": round(max_time * 0.55),
                "completed": bool(latest_log and (latest_log.get("timelineTimeMin") or max_time) >= max_time * 0.5),
            },
            {
                "id": f"ord-{order + 3}",
                "order": order + 3,
                "phase": "shaping",
                "label": f"Ball & final proof at {round(_effective_phase_temp('proof', config))}°F (poke test)",
                "targetMin": round(max_time * 0.82),
                "completed": bool(latest_log and (latest_log.get("rise") or 0) >= 55),
            },
            {
                "id": f"ord-{order + 4}",
                "order": order + 4,
                "phase": "baking",
                "label": f"Bake at peak window (~{max_time}m) & record outcome",
                "targetMin": max_time,
                "completed": bool(latest_log and latest_log.get("outcomeRating") in ("good", "bad")),
            },
        ]
    )
    return steps


def load_rag_cards(path: str = CARDS_PATH) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if is_jsonl_note(line):
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(card, dict) and card.get("id") and card.get("claim"):
                out.append(card)
    return out


def match_cards_for_context(
    style_slug: str,
    rag_variables: Sequence[str],
    query_text: str = "",
    top_k: int = 4,
    cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Match verified/library RAG insight cards to an instance or variable shift.

    Uses deterministic variable + style + confidence overlap first (instant,
    offline-safe) so every instance and comparison renders relevant dough
    physics without requiring live API calls on page load.
    """
    pool = cards if cards is not None else load_rag_cards()
    var_set = {v.strip().lower() for v in rag_variables if v}
    tokens = {t for t in re.findall(r"[a-z0-9_]{4,}", (query_text or "").lower())}

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for card in pool:
        c_vars = {str(v).lower() for v in (card.get("variables") or [])}
        c_styles = {str(s).lower() for s in (card.get("style_scope") or [])}
        conf = float(card.get("confidence") or 0.4)
        if conf < 0.35:
            continue
        var_hits = len(var_set & c_vars)
        style_bonus = 0.35 if style_slug in c_styles else (0.15 if "general" in c_styles else 0.0)
        claim_words = {t for t in re.findall(r"[a-z0-9_]{4,}", str(card.get("claim") or "").lower())}
        lex_hits = min(3, len(tokens & claim_words)) * 0.12
        score = var_hits * 0.45 + style_bonus + lex_hits + conf * 0.25
        if var_hits > 0 or style_slug in c_styles or lex_hits > 0:
            scored.append((round(score, 3), card))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "id": card["id"],
            "claim": card["claim"],
            "variables": card.get("variables") or [],
            "style_scope": card.get("style_scope") or [],
            "phase": card.get("phase"),
            "confidence": card.get("confidence"),
            "status": card.get("status") or "reviewed",
            "tags": card.get("tags") or [],
            "source": card.get("source") or {},
            "matchScore": score,
        }
        for score, card in scored[:top_k]
    ]


def _changes_between(prev_vars: Dict[str, Any], curr_vars: Dict[str, Any]) -> List[str]:
    changes: List[str] = []
    for defn in KNOWN_INSTANCE_VARIABLES:
        key = defn["key"]
        a = prev_vars.get(key)
        b = curr_vars.get(key)
        if a is None and b is None:
            continue
        if defn["kind"] == "numeric":
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                diff = _round1(float(b) - float(a))
                if abs(diff) > float(defn["tolerance"]):
                    sign = f"+{diff}" if diff > 0 else str(diff)
                    changes.append(f"{defn['shortLabel']} {sign}{defn['unit']}")
            elif a is None and isinstance(b, (int, float)):
                changes.append(f"{defn['shortLabel']} logged ({format_variable_value(defn, b)})")
        elif a != b and b is not None:
            changes.append(f"{defn['label']} → {format_variable_value(defn, b)}")
    return changes


def build_dough_instances(
    profiles: Sequence[Dict[str, Any]],
    logs: Sequence[Dict[str, Any]],
    rag_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    cards = rag_cards if rag_cards is not None else load_rag_cards()
    profiles_by_id = {p["id"]: p for p in profiles if p.get("id")}
    logs_by_profile: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in profiles_by_id}
    logs_by_group: Dict[str, List[Dict[str, Any]]] = {}
    unlinked: List[Dict[str, Any]] = []

    for log in logs:
        pid = (log.get("profileId") or "").strip()
        gid = (log.get("trialGroupId") or "").strip()
        if pid and pid in profiles_by_id:
            logs_by_profile[pid].append(log)
        elif gid:
            logs_by_group.setdefault(gid, []).append(log)
        else:
            unlinked.append(log)

    groups_to_build: List[Dict[str, Any]] = []
    for pid, prof in profiles_by_id.items():
        groups_to_build.append(
            {
                "id": f"instance:profile:{pid}",
                "shortCode": prof.get("shortCode"),
                "name": prof.get("name") or pid,
                "subtitle": "Dough profile instance",
                "kind": "profile-line",
                "profileId": pid,
                "profile": prof,
                "trialGroupLabel": None,
                "logs": logs_by_profile.get(pid, []),
            }
        )

    for gid, glogs in logs_by_group.items():
        label = next((l.get("trialGroupLabel") for l in glogs if l.get("trialGroupLabel")), gid)
        groups_to_build.append(
            {
                "id": f"instance:group:{gid}",
                "shortCode": None,
                "name": label,
                "subtitle": "Controlled trial series",
                "kind": "trial-series",
                "profileId": None,
                "profile": None,
                "trialGroupLabel": label,
                "logs": glogs,
            }
        )

    if unlinked:
        groups_to_build.append(
            {
                "id": "instance:unlinked",
                "shortCode": None,
                "name": "Exploratory unlinked bakes",
                "subtitle": "Ad-hoc dough runs",
                "kind": "unlinked",
                "profileId": None,
                "profile": None,
                "trialGroupLabel": None,
                "logs": unlinked,
            }
        )

    instances: List[Dict[str, Any]] = []
    for grp in groups_to_build:
        short_code = _stable_short_code(grp["id"], grp.get("shortCode"))
        chron_logs = sorted(grp["logs"], key=lambda item: str(item.get("createdAt") or ""))
        prof = grp["profile"]
        baseline_cfg = (
            (prof or {}).get("config")
            or (chron_logs[-1].get("profileConfigSnapshot") if chron_logs else None)
            or dict(FALLBACK_CONFIG)
        )
        style_slug = _style_slug_for_name(grp["name"])

        desired_outcome = (
            next(
                (
                    ((l.get("experimentMeta") or {}).get("desiredOutcome") or "").strip()
                    for l in reversed(chron_logs)
                    if ((l.get("experimentMeta") or {}).get("desiredOutcome") or "").strip()
                ),
                None,
            )
            or ((prof or {}).get("notes") or "").strip()
            or "Airy, extensible open crumb with repeatable peak fermentation window"
        )

        trials: List[Dict[str, Any]] = []
        storybank: List[Dict[str, Any]] = []

        for idx, log in enumerate(chron_logs):
            t_num = idx + 1
            cfg = resolve_log_config(log, baseline_cfg)
            variables = extract_known_variables(cfg, log)
            prev_vars = trials[idx - 1]["variables"] if idx > 0 else None
            changed = _changes_between(prev_vars, variables) if prev_vars else []
            phases = derive_trial_phases(log, cfg, short_code, t_num)
            _, stoich = compute_stoichiometry(cfg, float(log.get("rise") or 55))
            outcome_pos = resolve_outcome_positive(log)
            conv = compute_trial_convergence_score(log, cfg)
            meta = log.get("experimentMeta") or {}
            obj_ref = (meta.get("objectRef") or "").strip() or f"{short_code} · Ball T{t_num}"

            shift_txt = (
                f"Baseline mix at {variables['hydrationPct']}% hydration ({variables['phaseTempFFerment']}°F bulk)."
                if t_num == 1
                else (
                    f"Isolated {', '.join(changed[:3])} vs T{t_num - 1}."
                    if changed
                    else f"Replicate of T{t_num - 1} holding known variables steady."
                )
            )
            story_note = (meta.get("storyNote") or "").strip()
            res_txt = (
                story_note
                or " · ".join(
                    p
                    for p in [
                        str(log.get("result") or "").strip(),
                        str(log.get("method") or "").strip() if log.get("method") != "—" else "",
                    ]
                    if p
                )
                or f"Reached {round(float(log.get('rise') or 55))}% rise."
            )
            narrative = f"{shift_txt} {res_txt}".strip()

            flour_type = str(log.get("flour") or cfg.get("selectedFlour") or "00")
            node = {
                "id": log["id"],
                "trialNumber": t_num,
                "instanceId": grp["id"],
                "instanceName": grp["name"],
                "shortCode": short_code,
                "styleSlug": style_slug,
                "log": log,
                "config": cfg,
                "variables": variables,
                "phases": phases,
                "stoichiometry": stoich,
                "outcomePositive": outcome_pos,
                "convergenceScore": conv,
                "flourType": flour_type,
                "objectRef": obj_ref,
                "storySummary": narrative,
                "changedFromPrevious": changed,
            }
            trials.append(node)
            storybank.append(
                {
                    "id": f"story:{log['id']}",
                    "logId": log["id"],
                    "trialNumber": t_num,
                    "createdAt": log.get("createdAt"),
                    "title": log.get("title"),
                    "outcomePositive": outcome_pos,
                    "convergenceScore": conv,
                    "headline": (
                        f"T1 Baseline · {log.get('title')}"
                        if t_num == 1
                        else f"T{t_num} · {changed[0] if changed else 'Repeatability check'}"
                    ),
                    "narrative": narrative,
                    "hypothesis": (meta.get("hypothesis") or "").strip() or None,
                    "isolatedLabels": meta.get("isolatedVariables") or changed,
                    "objectRef": obj_ref,
                }
            )

        latest = trials[-1] if trials else None
        conv_score = latest["convergenceScore"] if latest else None
        improvement = (
            trials[-1]["convergenceScore"] - trials[0]["convergenceScore"]
            if len(trials) >= 2
            else None
        )
        rated = [t["outcomePositive"] for t in trials if t["outcomePositive"] is not None]
        success_rate = round((sum(1 for r in rated if r) / len(rated)) * 100) if rated else None

        ball_comp = build_ball_composition(
            latest["config"] if latest else baseline_cfg,
            float(latest["log"].get("rise") or 65) if latest else 65.0,
        )
        task_orders = derive_task_orders(
            latest["config"] if latest else baseline_cfg,
            latest["log"] if latest else None,
        )

        active_rag_vars = ["hydration", "fermentation_time", "temperature", "kneading"]
        if (latest or {}).get("variables", {}).get("fridgePhase") != "none":
            active_rag_vars.append("cold_ferment")
        matched_cards = match_cards_for_context(
            style_slug=style_slug,
            rag_variables=active_rag_vars,
            query_text=f"{grp['name']} {desired_outcome}",
            top_k=4,
            cards=cards,
        )

        if not trials:
            headline = f"No trials yet — log T1 baseline to start {short_code}."
        elif len(trials) == 1:
            headline = f"T1 baseline recorded ({conv_score}/100). Isolate 1 variable on T2."
        elif improvement and improvement > 0:
            headline = f"Improved +{improvement} convergence pts across {len(trials)} trials toward target."
        elif improvement and improvement < 0:
            headline = f"Regressed {improvement} pts vs T1 — inspect comparison matrix to revert confounders."
        else:
            headline = f"Steady at {conv_score}/100 across {len(trials)} trials — isolate one knob next run."

        instances.append(
            {
                "id": grp["id"],
                "shortCode": short_code,
                "name": grp["name"],
                "subtitle": grp["subtitle"],
                "kind": grp["kind"],
                "styleSlug": style_slug,
                "profileId": grp["profileId"],
                "profile": prof,
                "trialGroupLabel": grp["trialGroupLabel"],
                "baselineConfig": baseline_cfg,
                "desiredOutcome": desired_outcome,
                "trials": trials,
                "latestTrial": latest,
                "convergenceScore": conv_score,
                "improvementDelta": improvement,
                "successRate": success_rate,
                "ballComposition": ball_comp,
                "taskOrders": task_orders,
                "storyHeadline": headline,
                "storybank": list(reversed(storybank)),
                "matchedCards": matched_cards,
                "variableEffects": analyze_variable_effects(trials),
            }
        )

    instances.sort(key=lambda inst: (-len(inst["trials"]), inst["name"]))
    return instances


def draft_claim_from_comparison(
    control: Dict[str, Any],
    variant: Dict[str, Any],
    isolated: List[Dict[str, Any]],
    outcome_shift: str,
    rise_delta: float,
    conv_delta: int,
) -> Optional[Dict[str, Any]]:
    """Synthesize a falsifiable atomic RAG claim draft when an experiment isolates <=2 variables."""
    if not isolated or len(isolated) > 2 or outcome_shift == "unrated":
        return None

    lead = isolated[0]
    defn = lead["definition"]
    style_slug = variant.get("styleSlug") or "general"
    style_human = style_slug.replace("_", " ").title() if style_slug != "general" else "pizza"

    c_fmt = lead["controlFormatted"]
    v_fmt = lead["variantFormatted"]
    held_summary = f"while holding {variant['variables']['phaseTempFFerment']}°F bulk temp and {variant['variables']['flourTypeKey']} flour constant"
    if defn["key"] == "phaseTempFFerment":
        held_summary = f"while holding {variant['variables']['hydrationPct']}% hydration and {variant['variables']['flourTypeKey']} flour constant"

    res_note = str(variant["log"].get("result") or "").strip().rstrip(".")
    if outcome_shift == "improved":
        effect = (
            f"improves bake convergence (+{conv_delta} pts, {rise_delta:+.0f}% rise; {res_note.lower()})"
            if res_note and res_note != "—"
            else f"improves bake convergence (+{conv_delta} pts, {rise_delta:+.0f}% rise)"
        )
    elif outcome_shift == "regressed":
        effect = (
            f"reduces bake quality ({conv_delta} convergence pts, {rise_delta:+.0f}% rise; {res_note.lower()})"
            if res_note and res_note != "—"
            else f"reduces bake convergence ({conv_delta} pts, {rise_delta:+.0f}% rise)"
        )
    else:
        effect = f"keeps rise and handling broadly steady ({rise_delta:+.0f}% rise)"

    second_clause = ""
    if len(isolated) == 2:
        sec = isolated[1]
        second_clause = f" (alongside {sec['definition']['label'].lower()} {sec['controlFormatted']} → {sec['variantFormatted']})"

    sentence = (
        f"For {style_human} dough, shifting {defn['label'].lower()} from {c_fmt} to {v_fmt}"
        f"{second_clause} {held_summary} {effect}."
    )

    rag_vars: List[str] = []
    for row in isolated:
        for rv in row["definition"].get("ragVariables") or []:
            if rv not in rag_vars:
                rag_vars.append(rv)

    return {
        "claim": sentence,
        "variables": rag_vars or ["tactility"],
        "style_scope": [style_slug],
        "phase": [defn.get("ragPhase") or "bulk_fermentation"],
        "kind": "mechanism" if len(isolated) == 1 else "tradeoff",
        "status": "draft",
        "confidence": 0.42 if len(isolated) == 1 else 0.35,
        "relevance_targets": ["fermentation_lab_dashboard"],
        "reference": f"experiment:{variant.get('shortCode', '#inst')}:T{control['trialNumber']}->T{variant['trialNumber']}",
    }


def compare_trial_instances(
    control: Dict[str, Any],
    variant: Dict[str, Any],
    rag_cards: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    explicit_iso = set((variant["log"].get("experimentMeta") or {}).get("isolatedVariables") or [])
    deltas: List[Dict[str, Any]] = []

    for defn in KNOWN_INSTANCE_VARIABLES:
        key = defn["key"]
        c_val = control["variables"].get(key)
        v_val = variant["variables"].get(key)
        c_fmt = format_variable_value(defn, c_val)
        v_fmt = format_variable_value(defn, v_val)
        num_delta: Optional[float] = None
        changed = False
        delta_fmt = "Held constant"

        if defn["kind"] == "numeric":
            if isinstance(c_val, (int, float)) and isinstance(v_val, (int, float)):
                num_delta = _round1(float(v_val) - float(c_val))
                changed = abs(num_delta) > float(defn["tolerance"])
                delta_fmt = (
                    f"{'+' if num_delta > 0 else ''}{num_delta}{defn['unit']}"
                    if changed
                    else "0 (control)"
                )
            else:
                changed = False
                delta_fmt = "Unlogged"
        elif c_val != v_val:
            changed = True
            delta_fmt = f"{c_fmt} → {v_fmt}"

        deltas.append(
            {
                "definition": defn,
                "controlValue": c_val,
                "variantValue": v_val,
                "controlFormatted": c_fmt,
                "variantFormatted": v_fmt,
                "numericDelta": num_delta,
                "deltaFormatted": delta_fmt,
                "changed": changed,
                "intentionallyIsolated": key in explicit_iso,
            }
        )

    isolated = [d for d in deltas if d["changed"]]
    held_constant = [
        d for d in deltas if not d["changed"] and d["controlValue"] is not None and d["variantValue"] is not None
    ]

    if len(isolated) == 0:
        rigor = "replicate"
        rigor_badge = "Pure replicate (0 Δ)"
        rigor_expl = "All known variables were held constant — measures pure baseline repeatability."
    elif len(isolated) == 1:
        rigor = "single-variable"
        rigor_badge = "Clean 1-variable isolation"
        rigor_expl = (
            f"Only {isolated[0]['definition']['label']} changed while {len(held_constant)} variables stayed "
            "locked — outcome delta is scientifically attributable."
        )
    elif len(isolated) == 2:
        rigor = "controlled-pair"
        rigor_badge = "2-variable shift"
        rigor_expl = (
            f"{isolated[0]['definition']['label']} and {isolated[1]['definition']['label']} shifted together. "
            "Lock one next trial to separate their individual contributions."
        )
    else:
        rigor = "confounded"
        rigor_badge = f"Confounded ({len(isolated)} variables)"
        names = ", ".join(d["definition"]["shortLabel"] for d in isolated[:3])
        rigor_expl = (
            f"{len(isolated)} variables changed at once ({names}…). Hold baseline controls fixed and isolate "
            "a single variable on the next trial."
        )

    conv_delta = int(variant["convergenceScore"] - control["convergenceScore"])
    rise_delta = _round1(float(variant["log"].get("rise") or 0) - float(control["log"].get("rise") or 0))
    co2_delta = int(variant["stoichiometry"]["co2HeadspacePpm"] - control["stoichiometry"]["co2HeadspacePpm"])

    if control["outcomePositive"] is None and variant["outcomePositive"] is None and abs(conv_delta) < 4:
        outcome_shift = "unrated"
    elif (control["outcomePositive"] is False and variant["outcomePositive"] is True) or conv_delta >= 5:
        outcome_shift = "improved"
    elif (control["outcomePositive"] is True and variant["outcomePositive"] is False) or conv_delta <= -5:
        outcome_shift = "regressed"
    else:
        outcome_shift = "steady"

    lead = isolated[0] if isolated else None
    if outcome_shift == "improved":
        verdict = (
            f"{lead['definition']['label']} ({lead['deltaFormatted']}) lifted convergence by +{conv_delta} pts ({rise_delta:+.1f}% rise)."
            if lead
            else f"Replicate run gained +{conv_delta} convergence pts."
        )
    elif outcome_shift == "regressed":
        verdict = (
            f"{lead['definition']['label']} ({lead['deltaFormatted']}) dropped convergence by {conv_delta} pts — revert or narrow next run."
            if lead
            else f"Outcome regressed ({conv_delta} pts) with steady inputs — check unlogged oven/shaping cues."
        )
    else:
        verdict = f"Convergence held within ±4 pts ({conv_delta:+d} pts); variable shift was neutral."

    rag_vars: List[str] = []
    for d in isolated:
        for rv in d["definition"].get("ragVariables") or []:
            if rv not in rag_vars:
                rag_vars.append(rv)
    if not rag_vars:
        rag_vars = ["hydration", "fermentation_time"]

    matched_cards = match_cards_for_context(
        style_slug=variant.get("styleSlug") or "general",
        rag_variables=rag_vars,
        query_text=f"{verdict} {variant['log'].get('result') or ''}",
        top_k=3,
        cards=rag_cards,
    )

    drafted_claim = draft_claim_from_comparison(
        control=control,
        variant=variant,
        isolated=isolated,
        outcome_shift=outcome_shift,
        rise_delta=rise_delta,
        conv_delta=conv_delta,
    )

    return {
        "controlId": control["id"],
        "variantId": variant["id"],
        "controlLabel": f"{control['shortCode']} T{control['trialNumber']} · {control['log'].get('title')}",
        "variantLabel": f"{variant['shortCode']} T{variant['trialNumber']} · {variant['log'].get('title')}",
        "deltas": deltas,
        "isolatedVariables": isolated,
        "heldConstant": held_constant,
        "scientificRigor": rigor,
        "rigorBadge": rigor_badge,
        "rigorExplanation": rigor_expl,
        "outcomeShift": outcome_shift,
        "riseDelta": rise_delta,
        "convergenceDelta": conv_delta,
        "co2PpmDelta": co2_delta,
        "verdictSummary": verdict,
        "nextRecommendedIsolation": isolated[0]["definition"]["key"] if len(isolated) >= 2 else "hydrationPct",
        "matchedCards": matched_cards,
        "draftedClaim": drafted_claim,
    }


def analyze_variable_effects(trials: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(trials) < 2:
        return []
    sorted_conv = sorted(t["convergenceScore"] for t in trials)
    median_conv = sorted_conv[len(sorted_conv) // 2]

    good_bucket = [
        t for t in trials if t["outcomePositive"] is True or (t["outcomePositive"] is None and t["convergenceScore"] >= median_conv)
    ]
    bad_bucket = [
        t for t in trials if t["outcomePositive"] is False or (t["outcomePositive"] is None and t["convergenceScore"] < median_conv)
    ]

    insights: List[Dict[str, Any]] = []
    for defn in KNOWN_INSTANCE_VARIABLES:
        key = defn["key"]
        present = [t for t in trials if t["variables"].get(key) is not None]
        if len(present) < 2:
            continue
        distinct = {str(t["variables"][key]) for t in present}
        if len(distinct) < 2:
            continue

        if defn["kind"] == "numeric":
            all_nums = [float(t["variables"][key]) for t in present if isinstance(t["variables"][key], (int, float))]
            if not all_nums:
                continue
            span = max(all_nums) - min(all_nums)
            if span <= float(defn["tolerance"]):
                continue
            good_nums = [float(t["variables"][key]) for t in good_bucket if isinstance(t["variables"].get(key), (int, float))]
            bad_nums = [float(t["variables"][key]) for t in bad_bucket if isinstance(t["variables"].get(key), (int, float))]
            if not good_nums or not bad_nums:
                continue
            good_avg = _round1(sum(good_nums) / len(good_nums))
            bad_avg = _round1(sum(bad_nums) / len(bad_nums))
            diff = _round1(good_avg - bad_avg)
            strength = min(100, round((abs(diff) / max(span, 1.0)) * 100))
            meaningful = abs(diff) > float(defn["tolerance"])
            hint = (
                f"Insensitive across {format_variable_value(defn, min(all_nums))}–{format_variable_value(defn, max(all_nums))}"
                if not meaningful
                else (
                    f"Winning trials center near {format_variable_value(defn, good_avg)} vs {format_variable_value(defn, bad_avg)} in weaker runs"
                )
            )
            insights.append(
                {
                    "key": key,
                    "label": defn["label"],
                    "shortLabel": defn["shortLabel"],
                    "phase": defn["phase"],
                    "sampleCount": len(present),
                    "goodMeanOrMode": format_variable_value(defn, good_avg),
                    "badMeanOrMode": format_variable_value(defn, bad_avg),
                    "directionHint": hint,
                    "signalStrength": strength,
                    "status": "works" if meaningful else "neutral",
                }
            )
        else:
            good_vals = [str(t["variables"][key]) for t in good_bucket if t["variables"].get(key) is not None]
            bad_vals = [str(t["variables"][key]) for t in bad_bucket if t["variables"].get(key) is not None]
            if not good_vals or not bad_vals:
                continue
            good_mode = max(set(good_vals), key=good_vals.count)
            bad_mode = max(set(bad_vals), key=bad_vals.count)
            differs = good_mode != bad_mode
            insights.append(
                {
                    "key": key,
                    "label": defn["label"],
                    "shortLabel": defn["shortLabel"],
                    "phase": defn["phase"],
                    "sampleCount": len(present),
                    "goodMeanOrMode": format_variable_value(defn, good_mode),
                    "badMeanOrMode": format_variable_value(defn, bad_mode),
                    "directionHint": (
                        f"{format_variable_value(defn, good_mode)} outperforms {format_variable_value(defn, bad_mode)}"
                        if differs
                        else f"{format_variable_value(defn, good_mode)} used across outcomes"
                    ),
                    "signalStrength": 68 if differs else 18,
                    "status": "works" if differs else "neutral",
                }
            )

    insights.sort(key=lambda item: item["signalStrength"], reverse=True)
    return insights


def stage_empirical_claim(draft_payload: Dict[str, Any], commit: bool = False) -> Dict[str, Any]:
    """Normalize an empirical comparison claim onto `schema.json` and stage to `candidates.jsonl`."""
    enums = schema_enums()
    taken = load_card_ids()
    raw = {
        "claim": draft_payload.get("claim"),
        "variables": draft_payload.get("variables") or ["tactility"],
        "style_scope": draft_payload.get("style_scope") or ["general"],
        "phase": draft_payload.get("phase") or ["bulk_fermentation"],
        "kind": draft_payload.get("kind") or "mechanism",
        "status": "draft",
        "confidence": min(0.45, float(draft_payload.get("confidence") or 0.42)),
        "relevance_targets": draft_payload.get("relevance_targets") or ["fermentation_lab_dashboard"],
    }
    doc = {
        "source_type": "experiment",
        "reference": draft_payload.get("reference") or "admin-instance-lab",
    }
    card = normalize_card(raw, doc, enums, taken)
    card["origin"] = "instance_lab:experiment"
    write_candidates([card], CANDIDATES_PATH)
    committed = False
    if commit:
        commit_candidates([card])
        committed = True
    return {"card": card, "stagedPath": CANDIDATES_PATH, "committed": committed}


def verify_claim_bridge(claim_text: str, evidence_mode: str = "internal") -> Dict[str, Any]:
    """Run RAG internal or external truth check from `/admin`."""
    from validate import evaluate_truth

    use_web = evidence_mode == "external"
    hits = []
    try:
        from embed_pipeline import query as voyage_query

        hits = voyage_query(claim_text, top_k=5)
    except Exception:
        # Offline fallback: score against local cards.jsonl so internal check still works offline.
        matched = match_cards_for_context("general", ["hydration", "fermentation_time", "temperature"], claim_text, top_k=5)
        cards_by_id = {c["id"]: c for c in load_rag_cards()}
        hits = [(float(m["matchScore"]), cards_by_id[m["id"]]) for m in matched if m["id"] in cards_by_id]

    res = evaluate_truth(claim_text, hits, use_web=use_web)
    res["evidence_mode"] = evidence_mode
    return res


def _seed_profiles_and_logs() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    profiles = [
        {
            "id": "prof-neapolitan-5678",
            "shortCode": "#5678",
            "name": "Neapolitan — A journey",
            "notes": "Blistered airy cornicione with dry, extensible hand-stretch and zero gum line",
            "createdAt": "2026-11-01T10:00:00Z",
            "config": {
                **FALLBACK_CONFIG,
                "selectedFlour": "00",
                "selectedYeast": "instant",
                "yeastAmount": "low",
                "hydrationTarget": 64,
                "maxTime": 420,
                "phaseTempF": {"rest": 70, "ferment": 68, "proof": 72},
                "fridgePhase": "none",
            },
        },
        {
            "id": "prof-ny-cold",
            "shortCode": "#4102",
            "name": "NY Slice — Cold Retard Lab",
            "notes": "Crisp foldable undercarriage that opens thin straight after cold retard without snap-back",
            "createdAt": "2026-11-04T12:00:00Z",
            "config": {
                **FALLBACK_CONFIG,
                "flourMode": "blend",
                "selectedFlour": "bread",
                "blendName": "NY High-Gluten Blend",
                "activeBlend": [
                    {"flourId": "bread", "percentage": 85},
                    {"flourId": "whole-wheat", "percentage": 15},
                ],
                "selectedYeast": "instant",
                "yeastAmount": "low",
                "hydrationTarget": 63,
                "maxTime": 1440,
                "phaseTempF": {"rest": 72, "ferment": 38, "proof": 70},
                "fridgePhase": "ferment",
            },
        },
        {
            "id": "prof-sicilian-pan",
            "shortCode": "#7390",
            "name": "Sicilian Steel-Pan Focaccia",
            "notes": "Tall honeycomb pan crumb with fried olive-oil bottom and fully set center",
            "createdAt": "2026-11-08T09:00:00Z",
            "config": {
                **FALLBACK_CONFIG,
                "selectedFlour": "bread",
                "selectedYeast": "instant",
                "yeastAmount": "standard",
                "prefermentType": "poolish",
                "prefermentFlourPercent": 30,
                "hydrationTarget": 74,
                "maxTime": 480,
                "phaseTempF": {"rest": 72, "ferment": 72, "proof": 75},
                "fridgePhase": "none",
            },
        },
    ]

    neap_cfg = profiles[0]["config"]
    ny_cfg = profiles[1]["config"]
    sic_cfg = profiles[2]["config"]

    logs = [
        {
            "id": "log-5678-t1",
            "title": "T1 · Tight 60% baseline",
            "profileId": "prof-neapolitan-5678",
            "profileName": "Neapolitan — A journey",
            "flourMode": "single",
            "flour": "00",
            "yeast": "instant",
            "blend": [],
            "hydration": 60,
            "fermentation": "7h direct @ 68°F",
            "method": "20m autolyse, 2 slap-and-folds",
            "result": "Tight extensibility; resisted opening and tore near center",
            "outcomeRating": "bad",
            "stage": "Baking",
            "rise": 54,
            "ambientTempF": 69,
            "humidityPct": 48,
            "timelineTimeMin": 420,
            "createdAt": "2026-11-10T18:00:00Z",
            "comments": [],
            "phaseLogs": {
                "mixing": {"phase": "mixing", "completionPct": 60, "tempF": 69, "durationMin": 14, "feedback": "Stiff ball, zero tackiness", "objectRef": "#5678 · Ball A1"},
                "resting": {"phase": "resting", "completionPct": 65, "tempF": 70, "durationMin": 30, "feedback": "Passed windowpane early but high snap-back", "objectRef": "#5678 · Ball A1"},
                "fermentation": {"phase": "fermentation", "completionPct": 54, "tempF": 68, "durationMin": 376, "feedback": "Tight dome, modest gas dispersion", "objectRef": "#5678 · Ball A1"},
            },
            "experimentMeta": {
                "desiredOutcome": "Blistered airy cornicione with dry, extensible hand-stretch and zero gum line",
                "hypothesis": "Establish 60% hydration direct-dough baseline at 68°F bulk.",
                "objectRef": "#5678 · Ball A1",
                "storyNote": "Dough fought back on the bench; crust baked dense despite clean flavor.",
            },
            "profileConfigSnapshot": {**neap_cfg, "hydrationTarget": 60},
        },
        {
            "id": "log-5678-t2",
            "title": "T2 · Isolate Hydration → 64%",
            "profileId": "prof-neapolitan-5678",
            "profileName": "Neapolitan — A journey",
            "flourMode": "single",
            "flour": "00",
            "yeast": "instant",
            "blend": [],
            "hydration": 64,
            "fermentation": "7h direct @ 68°F",
            "method": "30m rest + 3 periodic coil folds during bulk",
            "result": "Good extensibility and airy rim, slight under-ferment in cool corner",
            "outcomeRating": "good",
            "stage": "Baking",
            "rise": 71,
            "ambientTempF": 69,
            "humidityPct": 50,
            "timelineTimeMin": 420,
            "createdAt": "2026-11-13T19:00:00Z",
            "comments": [],
            "phaseLogs": {
                "mixing": {"phase": "mixing", "completionPct": 64, "tempF": 69, "durationMin": 15, "feedback": "Supple pumpkin shape, smooth bowl release", "objectRef": "#5678 · Ball B1"},
                "resting": {"phase": "resting", "completionPct": 78, "tempF": 70, "durationMin": 35, "feedback": "Periodic coil folds built silkiness without mechanical over-tightening", "objectRef": "#5678 · Ball B1"},
                "fermentation": {"phase": "fermentation", "completionPct": 71, "tempF": 68, "durationMin": 370, "feedback": "Even micro-bubbles + slow poke spring-back", "objectRef": "#5678 · Ball B1"},
            },
            "experimentMeta": {
                "desiredOutcome": "Blistered airy cornicione with dry, extensible hand-stretch and zero gum line",
                "hypothesis": "Raising hydration +4% while holding 68°F bulk & 00 flour constant unlocks extensibility.",
                "isolatedVariables": ["hydrationPct"],
                "controlLogId": "log-5678-t1",
                "objectRef": "#5678 · Ball B1",
                "storyNote": "Clean single-variable jump: +4% water eliminated center tearing immediately.",
            },
            "profileConfigSnapshot": {**neap_cfg, "hydrationTarget": 64},
        },
        {
            "id": "log-5678-t3",
            "title": "T3 · Confounded 68% + Warm 76°F",
            "profileId": "prof-neapolitan-5678",
            "profileName": "Neapolitan — A journey",
            "flourMode": "single",
            "flour": "00",
            "yeast": "instant",
            "blend": [],
            "hydration": 68,
            "fermentation": "7h warm @ 76°F",
            "method": "Coil folds every 45m",
            "result": "Overproofed slack puddle; tore on peel transfer",
            "outcomeRating": "bad",
            "stage": "Baking",
            "rise": 96,
            "ambientTempF": 76,
            "humidityPct": 64,
            "timelineTimeMin": 420,
            "createdAt": "2026-11-16T20:00:00Z",
            "comments": [],
            "phaseLogs": {
                "mixing": {"phase": "mixing", "completionPct": 68, "tempF": 75, "durationMin": 18, "feedback": "Sticky slurry; needed extra slap folds", "objectRef": "#5678 · Ball C1"},
                "resting": {"phase": "resting", "completionPct": 60, "tempF": 76, "durationMin": 30, "feedback": "Spread flat rapidly", "objectRef": "#5678 · Ball C1"},
                "fermentation": {"phase": "fermentation", "completionPct": 96, "tempF": 76, "durationMin": 372, "feedback": "Collapsed bubbles; poke left permanent dent", "objectRef": "#5678 · Ball C1"},
            },
            "experimentMeta": {
                "desiredOutcome": "Blistered airy cornicione with dry, extensible hand-stretch and zero gum line",
                "hypothesis": "Push both hydration and temperature higher for faster open crumb.",
                "isolatedVariables": ["hydrationPct", "phaseTempFFerment", "ambientTempF"],
                "controlLogId": "log-5678-t2",
                "objectRef": "#5678 · Ball C1",
                "storyNote": "Classic confounded failure — changing hydration, bulk temp, and humidity at once blew past peak.",
            },
            "profileConfigSnapshot": {
                **neap_cfg,
                "hydrationTarget": 68,
                "phaseTempF": {"rest": 76, "ferment": 76, "proof": 76},
            },
        },
        {
            "id": "log-5678-t4",
            "title": "T4 · Lock 64% + Isolate 20% Poolish",
            "profileId": "prof-neapolitan-5678",
            "profileName": "Neapolitan — A journey",
            "flourMode": "single",
            "flour": "00",
            "yeast": "instant",
            "blend": [],
            "hydration": 64,
            "fermentation": "12h 20% poolish + 6.5h @ 68°F",
            "method": "Gentle coil folds at 45m & 90m",
            "result": "Excellent blistered cornicione, featherweight extensibility, zero gum line",
            "outcomeRating": "good",
            "stage": "Baking",
            "rise": 77,
            "ambientTempF": 69,
            "humidityPct": 51,
            "timelineTimeMin": 410,
            "createdAt": "2026-11-20T19:30:00Z",
            "comments": [],
            "phaseLogs": {
                "mixing": {"phase": "mixing", "completionPct": 64, "tempF": 69, "durationMin": 14, "feedback": "Ripe poolish Perfume; satin ball feel at Mix = 0", "objectRef": "#5678 · Ball D1"},
                "resting": {"phase": "resting", "completionPct": 88, "tempF": 70, "durationMin": 30, "feedback": "Translucent windowpane after second fold", "objectRef": "#5678 · Ball D1"},
                "fermentation": {"phase": "fermentation", "completionPct": 77, "tempF": 68, "durationMin": 366, "feedback": "Finely dispersed micro-bubbles + springy poke test", "objectRef": "#5678 · Ball D1"},
            },
            "experimentMeta": {
                "desiredOutcome": "Blistered airy cornicione with dry, extensible hand-stretch and zero gum line",
                "hypothesis": "Reverting to T2 controls (64% hydr, 68°F) and isolating 20% poolish maximizes extensibility safely.",
                "isolatedVariables": ["prefermentShare"],
                "controlLogId": "log-5678-t2",
                "objectRef": "#5678 · Ball D1",
                "storyNote": "Sweet spot nailed: poolish protease relaxation gave 68%-like stretch with 64% dry handling.",
            },
            "profileConfigSnapshot": {
                **neap_cfg,
                "hydrationTarget": 64,
                "prefermentType": "poolish",
                "prefermentFlourPercent": 20,
            },
        },
        {
            "id": "log-4102-t1",
            "title": "T1 · 6h Same-Day NY Slice",
            "profileId": "prof-ny-cold",
            "profileName": "NY Slice — Cold Retard Lab",
            "flourMode": "blend",
            "flour": None,
            "blendName": "NY High-Gluten Blend",
            "blend": [{"flourId": "bread", "percentage": 85}, {"flourId": "whole-wheat", "percentage": 15}],
            "yeast": "instant",
            "hydration": 63,
            "fermentation": "6h room temp @ 72°F",
            "method": "8m spiral knead to hard windowpane checkpoint",
            "result": "Bready flavor, fought back during 16-inch stretch",
            "outcomeRating": "bad",
            "stage": "Baking",
            "rise": 62,
            "ambientTempF": 72,
            "humidityPct": 45,
            "timelineTimeMin": 360,
            "createdAt": "2026-11-12T17:00:00Z",
            "comments": [],
            "experimentMeta": {
                "desiredOutcome": "Crisp foldable undercarriage that opens thin straight after cold retard without snap-back",
                "hypothesis": "Test same-day room-temp NY dough at 63% hydration.",
                "objectRef": "#4102 · Tray 1",
                "storyNote": "Passed kneading windowpane, but without cold retard the 63% dough snapped back.",
            },
            "profileConfigSnapshot": {
                **ny_cfg,
                "maxTime": 360,
                "fridgePhase": "none",
                "phaseTempF": {"rest": 72, "ferment": 72, "proof": 72},
            },
        },
        {
            "id": "log-4102-t2",
            "title": "T2 · Isolate 24h Cold Ferment (38°F)",
            "profileId": "prof-ny-cold",
            "profileName": "NY Slice — Cold Retard Lab",
            "flourMode": "blend",
            "flour": None,
            "blendName": "NY High-Gluten Blend",
            "blend": [{"flourId": "bread", "percentage": 85}, {"flourId": "whole-wheat", "percentage": 15}],
            "yeast": "instant",
            "hydration": 63,
            "fermentation": "24h cold bulk @ 38°F + 2h temper",
            "method": "8m knead to windowpane, cold tub retard",
            "result": "Opened effortlessly to 16 inches; crisp blistered foldable slice",
            "outcomeRating": "good",
            "stage": "Baking",
            "rise": 76,
            "ambientTempF": 71,
            "humidityPct": 46,
            "timelineTimeMin": 1440,
            "createdAt": "2026-11-18T18:30:00Z",
            "comments": [],
            "experimentMeta": {
                "desiredOutcome": "Crisp foldable undercarriage that opens thin straight after cold retard without snap-back",
                "hypothesis": "Isolating 24h 38°F cold bulk relaxes gluten so 63% dough opens easily.",
                "isolatedVariables": ["fridgePhase", "phaseTempFFerment", "timelineTimeMin"],
                "controlLogId": "log-4102-t1",
                "objectRef": "#4102 · Tray 2",
                "storyNote": "Directly confirms `cold-ferment-openability` card: time substituted for hydration.",
            },
            "profileConfigSnapshot": ny_cfg,
        },
        {
            "id": "log-7390-t1",
            "title": "T1 · High-Heat Pan Bake (525°F)",
            "profileId": "prof-sicilian-pan",
            "profileName": "Sicilian Steel-Pan Focaccia",
            "flourMode": "single",
            "flour": "bread",
            "yeast": "instant",
            "blend": [],
            "hydration": 74,
            "fermentation": "30% poolish + 4h pan proof",
            "method": "Coil folds + oiled steel pan dimple",
            "result": "Top scorched before thick center set; slight gum line",
            "outcomeRating": "bad",
            "stage": "Baking",
            "rise": 84,
            "ambientTempF": 73,
            "humidityPct": 52,
            "timelineTimeMin": 480,
            "createdAt": "2026-11-19T16:00:00Z",
            "comments": [],
            "experimentMeta": {
                "objectRef": "#7390 · Pan 1",
                "storyNote": "74% hydration rose beautifully in the pan, but baking too hot scorched top before crumb set.",
            },
            "profileConfigSnapshot": sic_cfg,
        },
    ]
    return profiles, logs


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if is_jsonl_note(line):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _write_jsonl(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def ensure_store(
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    profiles = _read_jsonl(profiles_path)
    logs = _read_jsonl(logs_path)
    if not profiles and not logs:
        profiles, logs = _seed_profiles_and_logs()
        _write_jsonl(profiles_path, profiles)
        _write_jsonl(logs_path, logs)
    return profiles, logs


def append_trial_log(
    draft: Dict[str, Any],
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Dict[str, Any]:
    profiles, logs = ensure_store(profiles_path, logs_path)
    profiles_by_id = {p["id"]: p for p in profiles if p.get("id")}
    pid = (draft.get("profileId") or "").strip() or None
    prof = profiles_by_id.get(pid) if pid else None
    base_cfg = dict((prof or {}).get("config") or FALLBACK_CONFIG)

    phase_temps = dict(base_cfg.get("phaseTempF") or {"rest": 70, "ferment": 68, "proof": 72})
    if isinstance(draft.get("phaseTempF"), dict):
        for k in ("rest", "ferment", "proof"):
            if draft["phaseTempF"].get(k) is not None:
                phase_temps[k] = float(draft["phaseTempF"][k])

    hydr = float(draft.get("hydration") if draft.get("hydration") is not None else base_cfg.get("hydrationTarget", 64))
    snap_cfg = {
        **base_cfg,
        "selectedFlour": draft.get("flour") or base_cfg.get("selectedFlour", "00"),
        "selectedYeast": draft.get("yeast") or base_cfg.get("selectedYeast", "instant"),
        "yeastAmount": draft.get("yeastAmount") or base_cfg.get("yeastAmount", "standard"),
        "prefermentType": draft.get("prefermentType") or base_cfg.get("prefermentType", "none"),
        "prefermentFlourPercent": float(
            draft.get("prefermentFlourPercent")
            if draft.get("prefermentFlourPercent") is not None
            else base_cfg.get("prefermentFlourPercent", 0)
        ),
        "hydrationTarget": hydr,
        "maxTime": int(draft.get("maxTime") if draft.get("maxTime") is not None else base_cfg.get("maxTime", 360)),
        "fridgePhase": draft.get("fridgePhase") or base_cfg.get("fridgePhase", "none"),
        "phaseTempF": phase_temps,
    }

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry: Dict[str, Any] = {
        "id": draft.get("id") or f"log-{uuid.uuid4().hex[:8]}",
        "title": (draft.get("title") or "").strip() or f"Controlled trial · {now_iso[:10]}",
        "profileId": pid,
        "profileName": (prof or {}).get("name") or draft.get("profileName"),
        "flourMode": snap_cfg.get("flourMode", "single"),
        "flour": snap_cfg.get("selectedFlour") if snap_cfg.get("flourMode") == "single" else None,
        "blendName": snap_cfg.get("blendName"),
        "blend": snap_cfg.get("activeBlend") or [],
        "yeast": snap_cfg.get("selectedYeast", "instant"),
        "hydration": _round1(hydr),
        "fermentation": (draft.get("fermentation") or "").strip()
        or f"{snap_cfg['maxTime']}m @ {round(_effective_phase_temp('ferment', snap_cfg))}°F",
        "method": (draft.get("notes") or draft.get("method") or "—").strip(),
        "result": (draft.get("result") or "Logged in Admin Lab").strip(),
        "outcomeRating": draft.get("outcomeRating") if draft.get("outcomeRating") in ("good", "bad") else None,
        "stage": (draft.get("stage") or "Baking").strip(),
        "rise": float(draft.get("rise") if draft.get("rise") is not None else 68),
        "ambientTempF": float(draft["ambientTempF"]) if draft.get("ambientTempF") not in (None, "") else None,
        "humidityPct": float(draft["humidityPct"]) if draft.get("humidityPct") not in (None, "") else None,
        "timelineTimeMin": int(draft["timelineTimeMin"]) if draft.get("timelineTimeMin") not in (None, "") else snap_cfg["maxTime"],
        "trialGroupId": draft.get("trialGroupId"),
        "trialGroupLabel": draft.get("trialGroupLabel"),
        "createdAt": draft.get("createdAt") or now_iso,
        "comments": [],
        "phaseLogs": draft.get("phaseLogs") or {},
        "experimentMeta": draft.get("experimentMeta") or {},
        "profileConfigSnapshot": snap_cfg,
    }
    logs.append(entry)
    _write_jsonl(logs_path, logs)
    return entry


def update_trial_log(
    log_id: str,
    patch: Dict[str, Any],
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Optional[Dict[str, Any]]:
    _, logs = ensure_store(profiles_path, logs_path)
    updated = None
    for idx, existing in enumerate(logs):
        if existing.get("id") != log_id:
            continue
        merged = dict(existing)
        for key in ("title", "result", "method", "outcomeRating", "rise", "hydration"):
            if key in patch and patch[key] is not None:
                merged[key] = patch[key]
        if "phaseLogs" in patch and isinstance(patch["phaseLogs"], dict):
            merged["phaseLogs"] = {**(merged.get("phaseLogs") or {}), **patch["phaseLogs"]}
        if "experimentMeta" in patch and isinstance(patch["experimentMeta"], dict):
            merged["experimentMeta"] = {**(merged.get("experimentMeta") or {}), **patch["experimentMeta"]}
        logs[idx] = merged
        updated = merged
        break
    if updated is not None:
        _write_jsonl(logs_path, logs)
    return updated


def import_app_bundle(
    payload: Dict[str, Any],
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Dict[str, int]:
    """Merge `StoredDoughProfile[]` and `DoughLogEntry[]` from `v0-yeast-development-app`."""
    profiles, logs = ensure_store(profiles_path, logs_path)
    prof_by_id = {p["id"]: p for p in profiles if p.get("id")}
    log_by_id = {l["id"]: l for l in logs if l.get("id")}

    in_profiles = payload.get("profiles") or []
    in_logs = payload.get("logs") or []

    added_p = 0
    for p in in_profiles:
        if isinstance(p, dict) and p.get("id"):
            if p["id"] not in prof_by_id:
                added_p += 1
            prof_by_id[p["id"]] = p

    added_l = 0
    for l in in_logs:
        if isinstance(l, dict) and l.get("id"):
            if l["id"] not in log_by_id:
                added_l += 1
            log_by_id[l["id"]] = l

    _write_jsonl(profiles_path, list(prof_by_id.values()))
    _write_jsonl(logs_path, list(log_by_id.values()))
    return {"profilesMerged": len(in_profiles), "profilesAdded": added_p, "logsMerged": len(in_logs), "logsAdded": added_l}


def export_app_bundle(
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Dict[str, Any]:
    """Export wire-compatible bundle for `v0-yeast-development-app` + matched RAG cards."""
    profiles, logs = ensure_store(profiles_path, logs_path)
    instances = build_dough_instances(profiles, logs)
    return {
        "schema": "dough-instance-bridge/v1",
        "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profiles": profiles,
        "logs": logs,
        "instances": instances,
        "knownVariables": list(KNOWN_INSTANCE_VARIABLES),
    }


def build_dashboard_payload(
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Dict[str, Any]:
    profiles, logs = ensure_store(profiles_path, logs_path)
    cards = load_rag_cards()
    instances = build_dough_instances(profiles, logs, rag_cards=cards)
    all_trials = [t for inst in instances for t in inst["trials"]]

    default_comparison = None
    if instances and len(instances[0]["trials"]) >= 2:
        t_list = instances[0]["trials"]
        default_comparison = compare_trial_instances(t_list[-2], t_list[-1], rag_cards=cards)
    elif len(all_trials) >= 2:
        default_comparison = compare_trial_instances(all_trials[0], all_trials[1], rag_cards=cards)

    return {
        "instances": instances,
        "profiles": profiles,
        "knownVariables": list(KNOWN_INSTANCE_VARIABLES),
        "globalVariableEffects": analyze_variable_effects(all_trials),
        "defaultComparison": default_comparison,
        "ragCardCount": len(cards),
    }
