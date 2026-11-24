"""passive-research.py — Claims controller for the dough research library.

Research sessions run in three phases so the loop does not die once every
card looks "done":

  1. Substantiate  weak / draft / unverified claims
  2. Expand        supported claims that still lack workflow children
  3. Seed          gap-filling drafts when the library has coverage holes

Within each card pass:

  select → retrieve/discover sources → assess truth → plan ops →
  enforce ClaimLifecyclePolicy → confirm → stage/commit → rebuild index
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set, Tuple

from embed_pipeline import build_index, query
from ingest import (
    CANDIDATES_PATH,
    CARDS_PATH,
    card_from_claim,
    commit_candidates,
    is_jsonl_note,
    load_card_ids,
    normalize_card,
    schema_enums,
)
from research_features import ResearchFeatures, add_feature_arguments, features_from_args
from research_policy import ClaimLifecyclePolicy
from synthesis import load_env
from validate import apply_sourcewise_tag, evaluate_truth, is_url

# ---------------------------------------------------------------------------
# 0. Setup and constants
# ---------------------------------------------------------------------------
load_env()

ARCHIVE_PATH = "archive.jsonl"
REPORT_DIR = "research-reports"
REPORT_LATEST = os.path.join(REPORT_DIR, "latest.md")
DRAFT_CONFIDENCE_CAP = 0.45
DEDUP_SIMILARITY = 0.82
PROPAGATION_LIMIT = 3
ADD_LIMIT = 2
POLICY = ClaimLifecyclePolicy(draft_confidence_cap=DRAFT_CONFIDENCE_CAP)
# Escalating search/verify depths: thin → medium → thorough.
RESEARCH_DEPTHS = (
    {"max_pages": 3, "max_candidates": 8, "search_limit": 4},
    {"max_pages": 5, "max_candidates": 12, "search_limit": 6},
    {"max_pages": 8, "max_candidates": 16, "search_limit": 8},
)
WORKFLOW_TARGETS = (
    "fermentation_lab_dashboard",
    "pizza_cart_app",
    "mise_en_place",
)
PREFERRED_STYLES = ("ny_slice", "neapolitan", "sicilian", "al_taglio", "sourdough")
PREFERRED_VARIABLES = (
    "salt",
    "flour_protein",
    "bake_temp",
    "bake_time",
    "cold_ferment",
    "humidity",
    "leavening",
    "extensibility",
)


def _log(message: str = "") -> None:
    """Print and flush so tee'd long runs show progress immediately."""
    print(message, flush=True)


class ClaimOp:
    """One controller decision. card is set for every op except a bare remove."""

    def __init__(
        self,
        op: str,
        reason: str,
        card: Optional[dict] = None,
        card_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        direction: Optional[str] = None,
        fallback_card: Optional[dict] = None,
    ):
        self.op = op
        self.reason = reason
        self.card = card
        self.card_id = card_id or (card or {}).get("id")
        self.parent_id = parent_id
        self.direction = direction
        self.fallback_card = fallback_card


# ---------------------------------------------------------------------------
# 1. Library IO and selection helpers
# ---------------------------------------------------------------------------

def is_needing_substantiation(card: dict, confidence_bar: float = 0.65) -> bool:
    """True if the card is not yet firmly verified with independent corroboration."""
    corroboration = card.get("corroboration") or {}
    verdict = corroboration.get("verdict")
    if verdict in ("support", "conflict") and card.get("status") in ("reviewed", "published"):
        return False
    if card.get("confidence", 0.0) < confidence_bar:
        return True
    if card.get("status") == "draft":
        return True
    if verdict in (None, "insufficient", "skipped"):
        return True
    return False


def _child_ids(parent_id: str, library: List[dict]) -> List[str]:
    kids = []
    for card in library:
        cid = card.get("id")
        if not cid:
            continue
        if parent_id in _labels(card.get("refines")) or parent_id in _labels(card.get("expands")):
            kids.append(cid)
    return kids


def is_ripe_for_expansion(card: dict, library: List[dict], confidence_bar: float = 0.65) -> bool:
    """Supported reviewed card that still has room for specialize/expand children."""
    if not card.get("id"):
        return False
    corroboration = card.get("corroboration") or {}
    if corroboration.get("verdict") != "support":
        return False
    if card.get("status") not in ("reviewed", "published"):
        return False
    if float(card.get("confidence") or 0.0) < confidence_bar:
        return False
    return len(_child_ids(card["id"], library)) < PROPAGATION_LIMIT


def library_coverage_gaps(library: List[dict]) -> Tuple[List[str], List[str]]:
    """Styles and variables the preferred vocab still lacks in the library."""
    styles: Set[str] = set()
    variables: Set[str] = set()
    for card in library:
        styles.update(_labels(card.get("style_scope")))
        variables.update(_labels(card.get("variables")))
    missing_styles = [s for s in PREFERRED_STYLES if s not in styles]
    missing_vars = [v for v in PREFERRED_VARIABLES if v not in variables]
    return missing_styles, missing_vars


def select_batch(
    library: List[dict],
    unresolved: Set[str],
    confidence_bar: float,
    batch_size: int,
    allow_propagate: bool,
    features: Optional[ResearchFeatures] = None,
) -> Tuple[str, List[dict]]:
    """Pick the next work phase and a batch of cards.

    substantiate → expand → seed|done. Seed returns an empty card list; the
    session drafts gap claims instead of researching an existing id. When seed
    and expand are disabled, returns phase "done".
    """
    feats = features or ResearchFeatures()
    if feats.substantiate:
        weak = [
            card
            for card in library
            if card.get("id") not in unresolved and is_needing_substantiation(card, confidence_bar)
        ]
        if weak:
            weak.sort(
                key=lambda card: (
                    0 if card.get("status") == "draft" else 1,
                    float(card.get("confidence") or 0.0),
                )
            )
            return "substantiate", weak[:batch_size]

    if feats.expand and allow_propagate:
        ripe = [
            card
            for card in library
            if card.get("id") not in unresolved and is_ripe_for_expansion(card, library, confidence_bar)
        ]
        if ripe:
            ripe.sort(key=lambda card: -float(card.get("confidence") or 0.0))
            return "expand", ripe[:batch_size]

    if feats.seed:
        return "seed", []
    return "done", []


def load_library_cards(path: str = CARDS_PATH) -> List[dict]:
    """Load all valid card objects from a jsonl file."""
    cards = []
    try:
        handle = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        return cards
    with handle:
        for line in handle:
            if is_jsonl_note(line):
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return cards


def update_cards_in_library(updated_cards: List[dict], path: str = CARDS_PATH) -> int:
    """Update existing cards by id in cards.jsonl, preserving notes."""
    by_id = {c["id"]: c for c in updated_cards}
    new_lines = []
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if is_jsonl_note(line):
                new_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            try:
                card = json.loads(line)
                cid = card.get("id")
                if cid in by_id:
                    new_lines.append(json.dumps(by_id[cid], ensure_ascii=False) + "\n")
                    count += 1
                else:
                    new_lines.append(line if line.endswith("\n") else line + "\n")
            except json.JSONDecodeError:
                new_lines.append(line if line.endswith("\n") else line + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return count


def archive_cards_from_library(
    card_ids: List[str],
    path: str = CARDS_PATH,
    archive_path: str = ARCHIVE_PATH,
    reason: str = "archived",
) -> int:
    """Move cards from cards.jsonl into archive.jsonl. Never hard-deletes."""
    to_remove = set(card_ids)
    kept_lines = []
    archived_cards = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if is_jsonl_note(line):
                kept_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            try:
                card = json.loads(line)
                if card.get("id") in to_remove:
                    card["archived_at"] = date.today().isoformat()
                    card["archive_reason"] = reason
                    archived_cards.append(card)
                else:
                    kept_lines.append(line if line.endswith("\n") else line + "\n")
            except json.JSONDecodeError:
                kept_lines.append(line if line.endswith("\n") else line + "\n")

    if archived_cards:
        with open(archive_path, "a", encoding="utf-8") as f:
            for card in archived_cards:
                f.write(json.dumps(card, ensure_ascii=False) + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)
    return len(archived_cards)


def stage_candidates(cards: List[dict], path: str = CANDIDATES_PATH) -> int:
    """Append new drafts to candidates.jsonl. Skips ids already staged."""
    if not cards:
        return 0
    existing = set(load_card_ids(path))
    fresh = [c for c in cards if c.get("id") and c["id"] not in existing]
    if not fresh:
        return 0
    with open(path, "a", encoding="utf-8") as f:
        for card in fresh:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")
    return len(fresh)


# ---------------------------------------------------------------------------
# 2. Claim-shaping utilities
# ---------------------------------------------------------------------------

def _labels(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in value if str(item).strip()]


def _norm_claim(text: str) -> str:
    return " ".join((text or "").lower().split())


def _context_shift(parent: dict, child: dict) -> bool:
    """True when the child is scoped to a different style, phase, or workflow."""
    parent_styles = set(_labels(parent.get("style_scope")))
    child_styles = set(_labels(child.get("style_scope")))
    parent_phase = set(_labels(parent.get("phase")))
    child_phase = set(_labels(child.get("phase")))
    parent_targets = set(_labels(parent.get("relevance_targets"))) or {"general_reference"}
    child_targets = set(_labels(child.get("relevance_targets")))
    style_shift = bool(child_styles - parent_styles) and child_styles != {"general"}
    phase_shift = bool(child_phase - parent_phase)
    target_shift = bool(child_targets - parent_targets) and child_targets != {"general_reference"}
    return style_shift or phase_shift or target_shift


def _broadens(parent: dict, child: dict) -> bool:
    """True when the child generalizes style or adds a variable the parent lacks."""
    parent_styles = set(_labels(parent.get("style_scope")))
    child_styles = set(_labels(child.get("style_scope")))
    generalizes = "general" in child_styles and bool(parent_styles - {"general"}) and not (child_styles - {"general"})
    more_styles = bool(child_styles - parent_styles) and not (parent_styles - child_styles)
    new_variable = bool(set(_labels(child.get("variables"))) - set(_labels(parent.get("variables"))))
    same_style = child_styles == parent_styles
    return generalizes or more_styles or (same_style and new_variable)


def _classify_propagation(parent: dict, child: dict, direction: str) -> Optional[str]:
    """specialize, expand, or None when the draft does not actually move the claim."""
    shift = _context_shift(parent, child)
    broad = _broadens(parent, child)
    if direction == "expand" and broad:
        return "expand"
    if direction == "specialize" and shift:
        return "specialize"
    if shift:
        return "specialize"
    if broad:
        return "expand"
    return None


def _scope_key(card: dict):
    return (
        tuple(sorted(_labels(card.get("style_scope")))),
        tuple(sorted(_labels(card.get("phase")))),
        tuple(sorted(_labels(card.get("relevance_targets")))),
    )


def _known_claims(library: List[dict]) -> Set[str]:
    return {_norm_claim(card.get("claim") or "") for card in library}


def _specialization_keys(parent_id: str, library: List[dict]) -> Set[tuple]:
    keys = set()
    for card in library:
        if parent_id in _labels(card.get("refines")):
            keys.add(_scope_key(card))
    return keys


def _evidence_reference(truth: dict, parent: dict) -> str:
    for cite in truth.get("citations") or []:
        if isinstance(cite, dict) and cite.get("url"):
            return cite["url"]
    return ((parent.get("source") or {}).get("reference")) or "passive-research"


# ---------------------------------------------------------------------------
# 3. Evidence-to-operation conversion
# ---------------------------------------------------------------------------

def _apply_evidence(card: dict, truth: dict, claim_text: Optional[str] = None, why: str = "") -> dict:
    """Copy a card and record evidence. A changed sentence keeps the same id."""
    updated = dict(card)
    changed = bool(claim_text) and _norm_claim(claim_text) != _norm_claim(card.get("claim") or "")
    if changed:
        updated["prior_claim"] = card.get("claim")
        updated["claim"] = " ".join(claim_text.split())
        updated["revised_at"] = date.today().isoformat()
    if why:
        updated["revision_note"] = why[:300]
    updated["corroboration"] = truth
    verdict = truth.get("verdict")
    score = float(truth.get("score") or 0.0)
    citations = truth.get("citations") or []
    first_url = next((c.get("url") for c in citations if isinstance(c, dict) and c.get("url")), None)
    if truth.get("contradicts"):
        existing = list(updated.get("contradicts") or [])
        updated["contradicts"] = list(dict.fromkeys(existing + list(truth["contradicts"])))
    if verdict == "support":
        updated["status"] = "reviewed"
        updated["confidence"] = max(0.75, min(1.0, score))
        if first_url:
            updated["source"] = {"type": "article", "reference": first_url}
    elif verdict == "conflict":
        updated["status"] = "draft" if changed else "reviewed"
        cap = 0.35 if changed else 0.20
        updated["confidence"] = min(cap, score if score else cap)
    return apply_sourcewise_tag(updated, truth)


def _shape_draft(
    raw: dict,
    taken: List[str],
    reference: str,
    origin: str,
    link_id: str,
    link_field: str,
) -> dict:
    """Normalize a new learning card and cap it as an unreviewed draft."""
    source_type = "article" if is_url(reference) else "conversation"
    refines = [link_id] if link_field == "refines" and link_id else []
    draft = normalize_card(
        {
            "claim": raw.get("claim"),
            "variables": raw.get("variables"),
            "style_scope": raw.get("style_scope"),
            "phase": raw.get("phase"),
            "kind": raw.get("kind"),
            "confidence": raw.get("confidence", 0.4),
            "relevance_targets": raw.get("relevance_targets"),
            "status": "draft",
            "refines": refines,
        },
        {"source_type": source_type, "reference": reference},
        schema_enums(),
        taken,
    )
    draft["status"] = "draft"
    draft["confidence"] = min(DRAFT_CONFIDENCE_CAP, float(draft.get("confidence") or 0.4))
    draft["origin"] = origin
    if link_field == "expands" and link_id:
        draft["expands"] = [link_id]
        draft["refines"] = [item for item in (draft.get("refines") or []) if item != link_id]
    return draft


def _fallback_plan(truth: dict) -> dict:
    """Used when the planner model is unavailable. No invented drafts."""
    verdict = (truth or {}).get("verdict")
    if verdict == "support":
        action = "validate"
    elif verdict == "conflict":
        action = "remove"
    else:
        action = "skip"
    return {
        "action": action,
        "revised_claim": None,
        "why": (truth or {}).get("rationale") or "Planner unavailable; followed the critic verdict.",
        "adds": [],
        "propagations": [],
    }


def _reconcile_action(verdict: str, plan: dict) -> str:
    """The critic bounds the planner. Support is not deleted. Conflict is not upgraded."""
    action = plan.get("action") or "skip"
    if verdict == "conflict" and action == "validate":
        return "remove"
    if verdict == "support" and action == "remove":
        return "modify" if plan.get("revised_claim") else "validate"
    if verdict == "support" and action == "skip":
        return "validate"
    return action


def _verified(op: ClaimOp) -> Optional[ClaimOp]:
    """Drop ops that fail structural lifecycle verification."""
    decision = POLICY.verify_operation(
        {
            "op": op.op,
            "card": op.card or {},
            "card_id": op.card_id,
            "parent_id": op.parent_id,
        }
    )
    if decision.allowed:
        return op
    _log(f"  Policy rejected {op.op}: {decision.reason}")
    return None


def ops_from_plan(
    card: dict,
    truth: dict,
    plan: dict,
    library: List[dict],
    taken: List[str],
    allow_propagate: bool = True,
    propagate_unproven: bool = False,
    duplicate_fn: Optional[Callable[[str], bool]] = None,
    features: Optional[ResearchFeatures] = None,
) -> List[ClaimOp]:
    """Turn a planner payload into concrete add / modify / remove / propagate ops.

    ClaimLifecyclePolicy gates create/modify/propagate/archive. Structural
    verification runs before an op is returned. ResearchFeatures modulates
    evidence_adds and propagate independently of parent validate/modify/remove.
    """
    feats = features or ResearchFeatures()
    verdict = (truth or {}).get("verdict")
    if verdict not in ("support", "conflict"):
        return []

    action = _reconcile_action(verdict, plan)
    why = plan.get("why") or (truth or {}).get("rationale") or ""
    parent_id = card.get("id")
    ops: List[ClaimOp] = []

    if action == "modify" and plan.get("revised_claim"):
        modify_gate = POLICY.allow_modify(card.get("claim") or "", plan["revised_claim"], truth)
        if modify_gate.allowed:
            rewritten = _apply_evidence(card, truth, plan["revised_claim"], why)
            if rewritten.get("claim") != card.get("claim"):
                kept = _verified(ClaimOp("modify", why, card=rewritten, parent_id=parent_id))
                if kept:
                    ops.append(kept)
            else:
                action = "validate"
        else:
            _log(f"  Policy blocked modify: {modify_gate.reason}")
            action = "validate" if verdict == "support" else action
    if action == "validate":
        kept = _verified(
            ClaimOp("validate", why, card=_apply_evidence(card, truth, why=why), parent_id=parent_id)
        )
        if kept:
            ops.append(kept)
    elif action == "remove":
        repairable = bool(plan.get("revised_claim"))
        archive_gate = POLICY.allow_archive(truth, repairable=repairable)
        if archive_gate.allowed:
            kept = _verified(
                ClaimOp(
                    "remove",
                    why or "Contradicted and not repairable by a narrower claim.",
                    card_id=parent_id,
                    parent_id=parent_id,
                    fallback_card=_apply_evidence(card, truth, why=why),
                )
            )
            if kept:
                ops.append(kept)
        else:
            _log(f"  Policy blocked archive: {archive_gate.reason}")
            kept = _verified(
                ClaimOp(
                    "modify",
                    archive_gate.reason,
                    card=_apply_evidence(card, truth, why=why),
                    parent_id=parent_id,
                )
            )
            if kept:
                ops.append(kept)

    if verdict != "support" and not propagate_unproven:
        return ops
    if action == "remove" and any(op.op == "remove" for op in ops):
        return ops

    growth_ok = feats.allow_growth(truth) or propagate_unproven
    if not growth_ok:
        _log("  Growth skipped: require_sourcewise_for_growth and claim is not sourcewise_supported.")
        return ops

    reference = _evidence_reference(truth or {}, card)
    known = _known_claims(library)
    known.add(_norm_claim(card.get("claim") or ""))
    scopes = _specialization_keys(parent_id, library)

    def _accept_text(claim_text: str) -> bool:
        norm = _norm_claim(claim_text)
        if not norm or norm in known:
            return False
        if duplicate_fn and duplicate_fn(claim_text):
            return False
        return True

    if verdict == "support" and feats.evidence_adds:
        for raw in (plan.get("adds") or [])[:ADD_LIMIT]:
            if not _accept_text(raw.get("claim") or ""):
                continue
            draft = _shape_draft(raw, taken, reference, "controller:add", parent_id, "refines")
            draft["refines"] = []
            create_gate = POLICY.allow_create(draft, truth, known, duplicate_fn)
            if not create_gate.allowed:
                taken[:] = [item for item in taken if item != draft.get("id")]
                continue
            kept = _verified(ClaimOp("add", why, card=draft, parent_id=parent_id))
            if kept:
                ops.append(kept)
                known.add(_norm_claim(draft.get("claim") or ""))

    if allow_propagate and feats.propagate:
        emitted = 0
        for raw in plan.get("propagations") or []:
            if emitted >= PROPAGATION_LIMIT:
                break
            if not _accept_text(raw.get("claim") or ""):
                continue
            direction = _classify_propagation(card, raw, raw.get("direction") or "")
            if direction is None:
                continue
            link_field = "refines" if direction == "specialize" else "expands"
            draft = _shape_draft(raw, taken, reference, f"controller:{direction}", parent_id, link_field)
            prop_gate = POLICY.allow_propagate(
                card,
                draft,
                direction,
                truth,
                known,
                explicit_hypothesis=propagate_unproven,
            )
            if not prop_gate.allowed:
                taken[:] = [item for item in taken if item != draft.get("id")]
                continue
            if direction == "specialize":
                key = _scope_key(draft)
                if key in scopes:
                    taken[:] = [item for item in taken if item != draft.get("id")]
                    continue
                scopes.add(key)
            kept = _verified(
                ClaimOp(
                    "propagate",
                    why,
                    card=draft,
                    parent_id=parent_id,
                    direction=direction,
                )
            )
            if kept:
                ops.append(kept)
                known.add(_norm_claim(draft.get("claim") or ""))
                emitted += 1
    return ops


# ---------------------------------------------------------------------------
# 4. Evidence retrieval and truth checking
# ---------------------------------------------------------------------------

def _search_queries(card: dict) -> List[str]:
    """Primary claim plus style/variable reformulations aimed at resolving doubts."""
    claim = (card.get("claim") or "").strip()
    styles = " ".join(_labels(card.get("style_scope")))
    variables = " ".join(_labels(card.get("variables")))
    queries = [claim] if claim else []

    def _add(text: str) -> None:
        cleaned = " ".join((text or "").split()).strip()
        if cleaned and _norm_claim(cleaned) not in {_norm_claim(q) for q in queries}:
            queries.append(cleaned)

    _add(" ".join(part for part in (claim, styles, variables, "pizza dough") if part))
    if styles and styles != "general":
        _add(f"{styles} pizza dough {variables} {claim}")
        _add(f"{styles} pizza bake temperature time oven")
    if "bake_temp" in variables or "bake_time" in variables or "bake" in claim.lower():
        _add(f"{styles or 'pizza'} pan dough oven temperature bake time vs neapolitan")
        _add(f"{styles or 'sicilian'} steel pan bake temperature crumb set")
    if "ferment" in claim.lower() or "cold_ferment" in variables:
        _add(f"{styles or 'pizza'} cold fermentation protease extensibility evidence")
    _add(f"{claim} food science source")
    return queries[:6]


def _summarize_truth(truth: dict) -> Tuple[str, str]:
    verdict = truth.get("verdict") or "insufficient"
    score = float(truth.get("score") or 0.0)
    rationale = truth.get("rationale") or "No rationale provided."
    citations = truth.get("citations") or []
    first_url = next((c.get("url") for c in citations if isinstance(c, dict) and c.get("url")), None)
    if verdict == "support":
        return verdict, f"Supported (score: {score:.2f}) via {first_url or 'evidence'} - {rationale}"
    if verdict == "conflict":
        return verdict, f"Contradicted (score: {score:.2f}) - {rationale}"
    if verdict in ("error", "skipped"):
        return verdict, rationale
    return "insufficient", f"Insufficient evidence - {rationale}"


def _merge_truth_attempts(base: Optional[dict], nxt: dict) -> dict:
    """Keep the strongest verdict while accumulating found/not_found/pages across depths."""
    if not base:
        return dict(nxt)
    merged = dict(base)
    rank = {"support": 3, "conflict": 3, "insufficient": 1, "skipped": 0, "error": 0}
    if rank.get(nxt.get("verdict"), 0) >= rank.get(merged.get("verdict"), 0):
        for key in ("verdict", "score", "rationale", "citations", "contradicts", "checked_at"):
            if key in nxt:
                merged[key] = nxt[key]
    for key in ("found", "not_found", "open_doubts", "pages_checked", "pages_ok", "pages_failed", "urls_considered"):
        seen = list(merged.get(key) or [])
        for item in nxt.get(key) or []:
            if item not in seen:
                seen.append(item)
        merged[key] = seen
    merged["sources"] = _merge_source_lists(merged.get("sources") or [], nxt.get("sources") or [])
    if "evidence_mode" in nxt:
        merged["evidence_mode"] = nxt["evidence_mode"]
    from validate import is_sourcewise_supported

    merged["sourcewise_supported"] = is_sourcewise_supported(merged)
    depths = list(merged.get("depths_tried") or [])
    depths.extend(nxt.get("depths_tried") or [])
    merged["depths_tried"] = depths
    return merged


def _merge_source_lists(base: List[dict], nxt: List[dict]) -> List[dict]:
    """Union source inventories across research depths; prefer richer fetch/cite state."""
    rank_status = {
        "fetched_ok": 4,
        "fetched_failed": 3,
        "cited_only": 2,
        "not_fetched": 1,
        "skipped_internal": 1,
        "not_applicable": 0,
    }
    by_key: dict = {}
    order: List[str] = []

    def _key(item: dict) -> str:
        if item.get("kind") == "library" and item.get("id"):
            return f"library:{item['id']}"
        return f"url:{item.get('url') or ''}"

    for item in list(base) + list(nxt):
        key = _key(item)
        if not key or key in ("url:", "library:"):
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(item)
            order.append(key)
            continue
        combined = dict(existing)
        if rank_status.get(item.get("fetch_status"), 0) > rank_status.get(combined.get("fetch_status"), 0):
            combined["fetch_status"] = item.get("fetch_status")
        if item.get("cited"):
            combined["cited"] = True
        for field in ("rank", "reason", "similarity", "claim", "discovered_via", "kind", "url", "id"):
            if combined.get(field) in (None, "") and item.get(field) not in (None, ""):
                combined[field] = item.get(field)
        by_key[key] = combined
    return [by_key[k] for k in order]


def research_claim(card: dict, use_web: bool = True) -> tuple:
    """Search evidence for one claim with escalating source depth.

    Retries alternate queries and raises page/search budgets until support,
    conflict, or depth is exhausted. Returns (verdict, summary, truth).
    """
    claim = card["claim"]
    queries = _search_queries(card) or [claim]
    best: Optional[Tuple[str, str, dict]] = None
    accumulated: Optional[dict] = None

    for depth_index, depth in enumerate(RESEARCH_DEPTHS):
        search_text = queries[min(depth_index, len(queries) - 1)]
        depth_queries = queries[: max(2, depth_index + 2)]
        _log(
            f"  Research depth {depth_index + 1}/{len(RESEARCH_DEPTHS)}: "
            f"pages≤{depth['max_pages']}, search≤{depth['search_limit']}"
        )
        try:
            hits = query(search_text, top_k=5)
        except Exception as exc:
            truth = {
                "verdict": "error",
                "score": 0.0,
                "rationale": f"Retrieval failed: {exc}",
                "citations": [],
                "found": [],
                "not_found": ["library retrieval failed"],
                "open_doubts": [str(exc)],
                "depths_tried": [depth],
            }
            accumulated = _merge_truth_attempts(accumulated, truth)
            best = ("error", truth["rationale"], accumulated)
            continue

        extra_urls: List[str] = []
        try:
            from research_sources import discover_sources

            discovered = discover_sources(
                search_text,
                hits,
                use_web=use_web,
                max_candidates=depth["max_candidates"],
                search_limit=depth["search_limit"],
                search_queries=depth_queries,
            )
            extra_urls = [item["url"] for item in discovered if item.get("url")]
        except Exception:
            extra_urls = []

        try:
            truth = evaluate_truth(
                claim,
                hits,
                extra_urls=extra_urls,
                use_web=use_web,
                max_pages=depth["max_pages"],
                search_limit=depth["search_limit"],
                search_queries=depth_queries,
            )
        except Exception as exc:
            truth = {
                "verdict": "error",
                "score": 0.0,
                "rationale": f"Truth evaluation failed: {exc}",
                "citations": [],
                "found": [],
                "not_found": [],
                "open_doubts": [str(exc)],
            }
        truth["depths_tried"] = [depth]
        truth["queries_used"] = depth_queries
        accumulated = _merge_truth_attempts(accumulated, truth)
        verdict, summary = _summarize_truth(accumulated)
        pages_ok = accumulated.get("pages_ok") or []
        sources = accumulated.get("sources") or []
        cited = sum(1 for s in sources if s.get("cited"))
        _log(
            f"  Depth result: {verdict} | pages ok {len(pages_ok)} | "
            f"sources {len(sources)} (cited {cited}) | "
            f"urls considered {len(accumulated.get('urls_considered') or [])}"
        )
        if depth_index and search_text != claim:
            summary = f"[depth {depth_index + 1}] {summary}"
        best = (verdict, summary, accumulated)
        if verdict in ("support", "conflict"):
            return verdict, summary, accumulated
        if depth_index < len(RESEARCH_DEPTHS) - 1 and verdict == "insufficient":
            _log("  Thin evidence; escalating search/verify depth...")

    return best or (
        "insufficient",
        "Insufficient evidence - no retrieval completed.",
        {
            "verdict": "insufficient",
            "score": 0.0,
            "rationale": "no retrieval",
            "citations": [],
            "found": [],
            "not_found": ["no sources retrieved"],
            "open_doubts": ["research loop never obtained evidence"],
        },
    )


def _claim_note(
    card: dict,
    phase: str,
    verdict: str,
    summary: str,
    truth: dict,
    actions: Optional[List[str]] = None,
) -> dict:
    """Basis-level note for one claim pass: what was found, missing, and concluded."""
    conclusion = {
        "support": "Claim resolved as supported.",
        "conflict": "Claim resolved as contradicted.",
        "insufficient": "Claim unresolved; sources were inadequate after escalation.",
        "error": "Claim unresolved due to retrieval/critic error.",
        "skipped": "Claim skipped; critic unavailable.",
    }.get(verdict, "Claim unresolved.")
    return {
        "id": card.get("id"),
        "claim": card.get("claim"),
        "phase": phase,
        "verdict": verdict,
        "summary": summary,
        "conclusion": conclusion,
        "found": list(truth.get("found") or []),
        "not_found": list(truth.get("not_found") or []),
        "open_doubts": list(truth.get("open_doubts") or []),
        "pages_ok": list(truth.get("pages_ok") or truth.get("pages_checked") or []),
        "pages_failed": list(truth.get("pages_failed") or []),
        "urls_considered": list(truth.get("urls_considered") or []),
        "sources": list(truth.get("sources") or []),
        "sourcewise_supported": bool(truth.get("sourcewise_supported")),
        "evidence_mode": truth.get("evidence_mode"),
        "actions": list(actions or []),
        "checked_at": truth.get("checked_at") or date.today().isoformat(),
    }


def format_research_report(notes: List[dict], session_meta: Optional[dict] = None) -> str:
    """Render a coherent basis-level session report."""
    meta = session_meta or {}
    lines = [
        "# Research loop report",
        "",
        f"- Generated: {date.today().isoformat()}",
        f"- Actions applied: {meta.get('actions', 0)}",
        f"- Claims examined: {len(notes)}",
    ]
    if meta.get("evidence_mode"):
        lines.append(f"- Evidence mode: {meta.get('evidence_mode')}")
    feats = meta.get("features") or {}
    if feats:
        on = [k for k, v in feats.items() if v]
        off = [k for k, v in feats.items() if not v]
        lines.append(f"- Features on: {', '.join(on) or '(none)'}")
        lines.append(f"- Features off: {', '.join(off) or '(none)'}")
    lines.append("")
    if not notes:
        lines.append("No claims were researched in this session.")
        lines.append("")
        return "\n".join(lines)

    resolved = [n for n in notes if n.get("verdict") in ("support", "conflict")]
    unresolved = [n for n in notes if n.get("verdict") not in ("support", "conflict")]
    lines.append("## Session conclusion")
    lines.append("")
    lines.append(
        f"{len(resolved)} claim(s) reached a support/conflict conclusion; "
        f"{len(unresolved)} remain open after escalated sourcing."
    )
    lines.append("")

    for note in notes:
        lines.append(f"## [{note.get('id')}]")
        lines.append("")
        lines.append(f"**Claim:** {note.get('claim')}")
        lines.append(f"**Phase:** {note.get('phase')}")
        lines.append(f"**Verdict:** {note.get('verdict')}")
        lines.append(f"**Conclusion:** {note.get('conclusion')}")
        lines.append(f"**Evidence summary:** {note.get('summary')}")
        lines.append("")
        lines.append("### Found")
        for item in note.get("found") or ["(nothing concrete extracted from sources)"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("### Not found")
        for item in note.get("not_found") or ["(critic did not list missing evidence)"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("### Open doubts")
        for item in note.get("open_doubts") or ["(none recorded)"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("### Full source list")
        sources = note.get("sources") or []
        if sources:
            for src in sources:
                kind = src.get("kind") or "?"
                status = src.get("fetch_status") or "?"
                cited = "cited" if src.get("cited") else "not-cited"
                label = src.get("url") or src.get("id") or "(unnamed)"
                extra = ""
                if src.get("kind") == "library" and src.get("id"):
                    extra = f" id={src.get('id')}"
                    if src.get("similarity") is not None:
                        extra += f" sim={src.get('similarity')}"
                elif src.get("discovered_via"):
                    extra = f" via={src.get('discovered_via')}"
                lines.append(f"- [{kind}/{status}/{cited}] {label}{extra}")
        else:
            pages = note.get("pages_ok") or []
            if pages:
                for url in pages:
                    lines.append(f"- ok: {url}")
            else:
                lines.append("- (no sources recorded)")
            for url in note.get("pages_failed") or []:
                lines.append(f"- failed: {url}")
        if note.get("sourcewise_supported"):
            lines.append("- tag: sourcewise_supported")
        lines.append("")
        lines.append("### Actions")
        for item in note.get("actions") or ["(no library edits)"]:
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines)


def _timestamped_report_path(when: Optional[datetime] = None) -> str:
    """research-reports/YYYY-MM-DDTHHMMSS.md (local time)."""
    stamp = (when or datetime.now()).strftime("%Y-%m-%dT%H%M%S")
    return os.path.join(REPORT_DIR, f"{stamp}.md")


def write_research_report(
    notes: List[dict],
    session_meta: Optional[dict] = None,
    path: Optional[str] = None,
) -> str:
    """Write a timestamped session report under research-reports/ and refresh latest.md."""
    text = format_research_report(notes, session_meta=session_meta)
    os.makedirs(REPORT_DIR, exist_ok=True)
    target = path or _timestamped_report_path()
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(REPORT_LATEST, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target


def record_research_attempt(card: dict, truth: dict) -> None:
    """Persist corroboration attempt on an unresolved card without raising confidence."""
    library = load_library_cards()
    cid = card.get("id")
    updated = False
    for index, existing in enumerate(library):
        if existing.get("id") != cid:
            continue
        next_card = apply_sourcewise_tag(dict(existing), truth)
        next_card["corroboration"] = truth
        next_card["research_attempted_at"] = date.today().isoformat()
        library[index] = next_card
        updated = True
        break
    if not updated:
        return
    with open(CARDS_PATH, "w", encoding="utf-8") as handle:
        for item in library:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def _library_duplicate(claim: str, parent_id: Optional[str]) -> bool:
    """True when Voyage already has a near-copy that is not the parent card."""
    try:
        hits = query(claim, top_k=3)
    except Exception:
        return False
    for sim, neighbor in hits:
        if neighbor.get("id") == parent_id:
            continue
        if sim >= DEDUP_SIMILARITY:
            return True
    return False


# ---------------------------------------------------------------------------
# 5. Controller: planning and operation proposal
# ---------------------------------------------------------------------------

class ClaimsController:
    """Propose and apply add, modify, remove, and propagate from evidence."""

    def __init__(
        self,
        library: List[dict],
        allow_propagate: bool = True,
        planner=None,
        features: Optional[ResearchFeatures] = None,
    ):
        self.library = list(library)
        self.by_id = {card["id"]: card for card in library if card.get("id")}
        self.features = features or ResearchFeatures(propagate=allow_propagate)
        if not allow_propagate:
            self.features.propagate = False
            self.features.focused_propagate = False
        self.allow_propagate = self.features.propagate
        self.planner = planner
        self.taken = [card["id"] for card in library if card.get("id")]

    def propose(
        self,
        card: dict,
        truth: dict,
        propagate_unproven: bool = False,
        check_similarity: bool = True,
        force_propagate: bool = False,
        skip_parent_edit: bool = False,
    ) -> List[ClaimOp]:
        """Plan library edits for one card. Does not write.

        If support yields no propagate ops, request a focused propagation plan
        once so validated claims still fan into workflows (when enabled).
        """
        verdict = (truth or {}).get("verdict")
        if verdict not in ("support", "conflict") and not propagate_unproven:
            return []
        plan = self._plan(card, truth)
        duplicate_fn = None
        if check_similarity:
            duplicate_fn = lambda text, parent=card.get("id"): _library_duplicate(text, parent)
        working_truth = truth or {"verdict": "insufficient", "score": 0.0, "rationale": "", "citations": []}
        if propagate_unproven and working_truth.get("verdict") not in ("support", "conflict"):
            working_truth = dict(working_truth)
            working_truth["verdict"] = "support"
            working_truth.setdefault("score", 0.4)
            working_truth["rationale"] = (
                working_truth.get("rationale")
                or "Explicit propagate. Drafts are hypotheses, not corroborated facts."
            )
        ops = ops_from_plan(
            card,
            working_truth,
            plan,
            self.library,
            self.taken,
            allow_propagate=self.allow_propagate,
            propagate_unproven=propagate_unproven,
            duplicate_fn=duplicate_fn,
            features=self.features,
        )
        has_propagate = any(op.op == "propagate" for op in ops)
        ripe = is_ripe_for_expansion(
            {
                **card,
                "status": "reviewed",
                "confidence": max(float(card.get("confidence") or 0), 0.75),
                "corroboration": working_truth,
            },
            self.library,
        )
        needs_propagate = (
            self.allow_propagate
            and self.features.focused_propagate
            and working_truth.get("verdict") == "support"
            and not has_propagate
            and (force_propagate or ripe)
        )
        if needs_propagate and not propagate_unproven:
            _log("  Planner returned no workflow drafts; requesting focused propagations...")
            prop_plan = self._plan_propagations(card, working_truth)
            more = ops_from_plan(
                card,
                working_truth,
                {
                    "action": "validate",
                    "revised_claim": None,
                    "why": prop_plan.get("why") or working_truth.get("rationale") or why_from(plan),
                    "adds": [],
                    "propagations": prop_plan.get("propagations") or [],
                },
                self.library,
                self.taken,
                allow_propagate=True,
                propagate_unproven=False,
                duplicate_fn=duplicate_fn,
                features=self.features,
            )
            ops.extend(op for op in more if op.op == "propagate")
        if propagate_unproven or skip_parent_edit:
            ops = [op for op in ops if op.op in ("propagate", "add")]
        return ops

    def _plan(self, card: dict, truth: dict) -> dict:
        if self.planner is not None:
            return self.planner(card, truth, self.library)
        try:
            from synthesis import make_bot
            from validate import resolve_critic

            name = resolve_critic(None)
            if not name:
                raise RuntimeError("no GEMINI_API_KEY, ANTHROPIC_API_KEY, or local model")
            evidence = {
                "verdict": (truth or {}).get("verdict"),
                "score": (truth or {}).get("score"),
                "rationale": (truth or {}).get("rationale"),
                "citations": (truth or {}).get("citations") or [],
            }
            return make_bot(name).plan_claim(card, evidence, self.library)
        except Exception as exc:
            _log(f"  Planner unavailable ({exc}). Using the critic verdict only.")
            return _fallback_plan(truth or {})

    def _plan_propagations(self, card: dict, truth: dict) -> dict:
        try:
            from synthesis import make_bot
            from validate import resolve_critic

            name = resolve_critic(None)
            if not name:
                raise RuntimeError("no chat model configured")
            evidence = {
                "verdict": (truth or {}).get("verdict"),
                "score": (truth or {}).get("score"),
                "rationale": (truth or {}).get("rationale"),
                "citations": (truth or {}).get("citations") or [],
            }
            return make_bot(name).plan_propagations(card, evidence, self.library)
        except Exception as exc:
            _log(f"  Propagation planner unavailable ({exc}).")
            return {"action": "validate", "revised_claim": None, "why": str(exc), "adds": [], "propagations": []}

    def remember(self, op: ClaimOp) -> None:
        """Keep later proposals from duplicating a card accepted earlier in the session."""
        if op.card and op.op in ("add", "propagate"):
            self.library.append(op.card)
            if op.card.get("id") and op.card["id"] not in self.taken:
                self.taken.append(op.card["id"])
        elif op.card and op.op in ("validate", "modify"):
            self.by_id[op.card["id"]] = op.card


def why_from(plan: dict) -> str:
    return (plan or {}).get("why") or ""


def seed_gap_ops(library: List[dict], taken: List[str], limit: int = 2) -> List[ClaimOp]:
    """Draft gap-filling claims from library coverage holes. Confidence capped."""
    missing_styles, missing_vars = library_coverage_gaps(library)
    if missing_styles:
        question = (
            f"What important pizza-dough mechanisms are missing for {missing_styles[0]} style? "
            "Draft atomic falsifiable claims."
        )
    elif missing_vars:
        question = (
            f"How does {missing_vars[0]} affect pizza dough fermentation or handling? "
            "Draft atomic falsifiable claims the library lacks."
        )
    else:
        question = (
            "Draft novel pizza-dough research claims about bake temperature, cold ferment, "
            "or dough diagnostics that are not already in the library."
        )
    _log(f"  Seeding gaps from: {question}")
    try:
        from synthesis import make_bot
        from validate import resolve_critic

        name = resolve_critic(None)
        if not name:
            raise RuntimeError("no chat model configured")
        slim = [
            {
                "id": card.get("id"),
                "claim": (card.get("claim") or "")[:200],
                "variables": card.get("variables"),
                "style_scope": card.get("style_scope"),
            }
            for card in library[:30]
        ]
        payload = make_bot(name).synthesize(question, slim, retrieval=0.2, threshold=0.7)
    except Exception as exc:
        _log(f"  Gap seeder unavailable ({exc}).")
        return []

    known = _known_claims(library)
    ops: List[ClaimOp] = []
    for raw in (payload.get("cards") or [])[:limit]:
        claim = " ".join(str(raw.get("claim") or "").split()).strip()
        if not claim or _norm_claim(claim) in known:
            continue
        if _library_duplicate(claim, None):
            continue
        draft = _shape_draft(
            raw,
            taken,
            "controller:seed",
            "controller:seed",
            "",
            "refines",
        )
        draft["refines"] = []
        draft["origin"] = "controller:seed"
        fake_truth = {"verdict": "support", "score": 0.7, "rationale": "gap seed", "citations": []}
        gate = POLICY.allow_create(draft, fake_truth, known)
        if not gate.allowed:
            taken[:] = [item for item in taken if item != draft.get("id")]
            continue
        kept = _verified(ClaimOp("add", f"Seeded library gap: {question}", card=draft))
        if kept:
            ops.append(kept)
            known.add(_norm_claim(claim))
    return ops


def format_op(op: ClaimOp) -> str:
    """One block of text describing a proposed operation."""
    if op.op == "validate" and op.card:
        return (
            f"VALIDATE [{op.card_id}] confidence -> {float(op.card.get('confidence') or 0):.2f}. "
            f"{op.reason}"
        )
    if op.op == "modify" and op.card:
        if op.card.get("prior_claim"):
            return (
                f"MODIFY [{op.card_id}] ({op.reason})\n"
                f"      was: {op.card.get('prior_claim')}\n"
                f"      now: {op.card.get('claim')}"
            )
        return (
            f"MODIFY [{op.card_id}] confidence -> {float(op.card.get('confidence') or 0):.2f}. "
            f"{op.reason}"
        )
    if op.op == "remove":
        return f"REMOVE [{op.card_id}] archive to {ARCHIVE_PATH}. {op.reason}"
    if op.op == "add" and op.card:
        return f"ADD [{op.card['id']}] {op.card.get('claim')}"
    if op.op == "propagate" and op.card:
        return (
            f"PROPAGATE {op.direction} [{op.card['id']}] "
            f"style={op.card.get('style_scope')} phase={op.card.get('phase')} "
            f"target={op.card.get('relevance_targets')}\n"
            f"      {op.card.get('claim')}"
        )
    return f"{op.op} [{op.card_id}] {op.reason}"


def prompt_user_choice(prompt: str, options: List[str], default: str) -> str:
    """Prompt for a single-character choice. Blank accepts the default."""
    rendered = "/".join(
        f"[{item.upper()}]" if item.lower() == default.lower() else item.lower()
        for item in options
    )
    while True:
        try:
            choice = input(f"{prompt} ({rendered}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return "q"
        if not choice:
            return default.lower()
        if choice in [item.lower() for item in options]:
            return choice
        print(f"Invalid option. Choose from: {rendered}")


# ---------------------------------------------------------------------------
# 6. Confirm and write path
# ---------------------------------------------------------------------------

def _auto_resolve(op: ClaimOp, force_remove: bool) -> Optional[ClaimOp]:
    """Without a person in the loop, archive is withheld and the conflict is demoted."""
    if op.op != "remove" or force_remove:
        return op
    if not op.fallback_card:
        return None
    return ClaimOp(
        "modify",
        "Conflict recorded as a demotion. Archive withheld unless you confirm remove.",
        card=op.fallback_card,
        parent_id=op.parent_id,
    )


def confirm_ops(
    ops: List[ClaimOp],
    interactive: bool,
    force_remove: bool = False,
) -> tuple:
    """Return (accepted, quit). quit is True when the operator stops the session."""
    accepted: List[ClaimOp] = []
    for op in ops:
        print(f"  >>> {format_op(op)}")
        if not interactive:
            resolved = _auto_resolve(op, force_remove=force_remove)
            if resolved is None:
                print("  Auto: withheld.")
                continue
            if resolved is not op:
                print(f"  Auto: {format_op(resolved)}")
            else:
                print("  Auto: applying.")
            accepted.append(resolved)
            continue
        default = "n" if op.op == "remove" else "y"
        choice = prompt_user_choice("  Apply?", ["y", "n", "q"], default=default)
        if choice == "q":
            return accepted, True
        if choice == "y":
            accepted.append(op)
    return accepted, False


def apply_ops(
    ops: List[ClaimOp],
    auto_commit: bool,
    interactive: bool,
    force_commit_drafts: bool = False,
) -> Dict[str, int]:
    """Write accepted operations. New cards always land in candidates.jsonl."""
    updates = [op.card for op in ops if op.op in ("validate", "modify") and op.card]
    removals = [op.card_id for op in ops if op.op == "remove" and op.card_id]
    drafts = [op.card for op in ops if op.op in ("add", "propagate") and op.card]
    counts = {"validate": 0, "modify": 0, "remove": 0, "add": 0, "propagate": 0, "staged": 0}
    for op in ops:
        if op.op in counts:
            counts[op.op] += 1

    if removals:
        n_arch = archive_cards_from_library(removals, reason="removed_by_claims_controller")
        print(f"  -> Archived {n_arch} card(s) to {ARCHIVE_PATH}.")

    commit_updates = bool(updates) and (auto_commit or not interactive)
    commit_drafts = bool(drafts) and (auto_commit or force_commit_drafts)
    if interactive and not auto_commit:
        if updates:
            choice = prompt_user_choice(
                "Commit claim updates (validate / modify) to cards.jsonl?",
                ["y", "n"],
                default="y",
            )
            commit_updates = choice == "y"
        if drafts and not force_commit_drafts:
            choice = prompt_user_choice(
                "Also commit new add/propagate drafts into cards.jsonl?",
                ["y", "n"],
                default="n",
            )
            commit_drafts = choice == "y"

    if updates and not commit_updates:
        held = stage_candidates(updates)
        print(f"  -> Staged {held} update(s) in {CANDIDATES_PATH} instead of editing the library.")
    if commit_updates and updates:
        n_up = update_cards_in_library(updates)
        print(f"  -> Updated {n_up} existing card(s) in {CARDS_PATH}.")
    if drafts:
        n_staged = stage_candidates(drafts)
        counts["staged"] = n_staged
        print(f"  -> Staged {n_staged} new card(s) in {CANDIDATES_PATH}.")
    if commit_drafts and drafts:
        written = commit_candidates(drafts)
        print(f"  -> Committed {len(written)} new card(s) to {CARDS_PATH}.")

    library_changed = bool(removals) or (commit_updates and updates) or (commit_drafts and drafts)
    if library_changed:
        print("  -> Rebuilding vector index cards.db...")
        build_index(provider="voyage", cards_path=CARDS_PATH)
    return counts


# ---------------------------------------------------------------------------
# 7. Batch session loop
# ---------------------------------------------------------------------------

def research_session(
    interactive: bool = False,
    auto_commit: bool = False,
    confidence_bar: float = 0.65,
    max_actions: int = 10,
    batch_size: int = 5,
    delay: float = 3.0,
    until_done: bool = False,
    allow_propagate: bool = True,
    only_id: Optional[str] = None,
    explore: bool = False,
    use_web: bool = True,
    features: Optional[ResearchFeatures] = None,
) -> None:
    """Run substantiate → expand → seed until bounds are hit.

    Default one-shot still researches weak claims. `--until-done` or `--explore`
    continues into expansion of supported cards and gap seeding so the loop
    keeps producing new drafts after the library looks settled.

    use_web=True is external evidence (library + web). use_web=False is
    internal evidence (library neighbors only, no HTTP).

    ResearchFeatures modulates expand/seed/propagate/evidence_adds independently.
    """
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    feats = features or ResearchFeatures(propagate=allow_propagate)
    if not allow_propagate:
        feats.propagate = False
        feats.focused_propagate = False
    actions = 0
    unresolved: Set[str] = set()
    expanded_ids: Set[str] = set()
    exhausted_ids: Set[str] = set()
    seed_rounds = 0
    round_n = 0
    continuous = until_done or explore
    notes: List[dict] = []
    evidence_mode = "external" if use_web else "internal"

    _log("\n" + "=" * 60)
    _log("  CLAIMS CONTROLLER")
    _log(
        f"  Confidence {confidence_bar} | batch {batch_size} | "
        f"max actions {max_actions} | explore {continuous} | evidence {evidence_mode}"
    )
    _log(f"  Features: {feats.summary()}")
    _log("=" * 60)

    while True:
        round_n += 1
        library = load_library_cards()
        controller = ClaimsController(library, allow_propagate=feats.propagate, features=feats)
        progressed = False
        researched_this_round = False
        quit_session = False
        accepted_round: List[ClaimOp] = []

        if only_id:
            phase, batch = "substantiate", [card for card in library if card.get("id") == only_id]
            if not batch:
                _log(f"No card with id {only_id}.")
                return
        else:
            phase, batch = select_batch(
                library,
                unresolved | expanded_ids | exhausted_ids,
                confidence_bar,
                batch_size,
                allow_propagate=feats.propagate and feats.expand and continuous,
                features=feats,
            )

        if phase == "done":
            _log("\nNo claims left below the confidence bar (expand/seed disabled or empty).")
            break

        if phase == "seed":
            if not continuous:
                _log("\nNo claims left below the confidence bar.")
                _log("Pass --until-done or --explore to expand supported claims and seed gaps.")
                break
            if not feats.seed:
                _log("\nSeed phase disabled (--no-seed or profile). Stopping.")
                break
            if seed_rounds >= 3:
                _log("\nSeed round cap reached.")
                break
            if actions >= max_actions:
                _log(f"\n[Limit] Max actions ({max_actions}) reached.")
                break
            _log(f"\nRound {round_n}: phase=seed (coverage gaps)")
            missing_styles, missing_vars = library_coverage_gaps(library)
            _log(f"  Missing styles: {missing_styles or '—'} | Missing variables: {missing_vars or '—'}")
            remaining = max_actions - actions
            ops = seed_gap_ops(library, controller.taken, limit=min(2, remaining))
            seed_rounds += 1
            if not ops:
                _log("  No new gap drafts produced. Stopping.")
                break
            accepted, quit_session = confirm_ops(ops, interactive=interactive)
            for op in accepted:
                controller.remember(op)
            accepted_round.extend(accepted)
            actions += len(accepted)
            progressed = bool(accepted)
        else:
            _log(
                f"\nRound {round_n}: phase={phase} | "
                f"{len(batch)} card(s) queued (showing {min(batch_size, len(batch))})."
            )
            for card in batch:
                if actions >= max_actions:
                    _log(f"\n[Limit] Max actions ({max_actions}) reached.")
                    quit_session = True
                    break
                cid = card.get("id")
                _log("\n" + "-" * 60)
                _log(f"Target [{cid}] ({phase})")
                _log(f"  \"{card.get('claim')}\"")
                _log(
                    f"  Confidence {float(card.get('confidence') or 0):.2f} | "
                    f"status {card.get('status', 'draft')} | "
                    f"style {card.get('style_scope')} | target {card.get('relevance_targets')}"
                )

                note_actions: List[str] = []
                if phase == "expand":
                    truth = dict(card.get("corroboration") or {})
                    if truth.get("verdict") != "support":
                        researched_this_round = True
                        verdict, summary, truth = research_claim(card, use_web=use_web)
                        _log(f"  Evidence ({evidence_mode}): {summary}")
                        if verdict != "support":
                            record_research_attempt(card, truth)
                            notes.append(
                                _claim_note(card, phase, verdict, summary, truth, ["recorded unresolved attempt"])
                            )
                            unresolved.add(cid)
                            exhausted_ids.add(cid)
                            expanded_ids.add(cid)
                            time.sleep(delay)
                            continue
                    else:
                        _log(
                            f"  Using prior support (score: {float(truth.get('score') or 0):.2f}); "
                            "expanding into workflow drafts."
                        )
                        summary = "Prior support reused for expansion."
                        verdict = "support"
                    remaining = max_actions - actions
                    ops = controller.propose(
                        card,
                        truth,
                        force_propagate=True,
                        skip_parent_edit=True,
                    )[:remaining]
                    expanded_ids.add(cid)
                else:
                    researched_this_round = True
                    verdict, summary, truth = research_claim(card, use_web=use_web)
                    _log(f"  Evidence ({evidence_mode}): {summary}")
                    if truth.get("found"):
                        _log("  Found:")
                        for item in truth["found"][:5]:
                            _log(f"    - {item}")
                    if truth.get("not_found"):
                        _log("  Not found:")
                        for item in truth["not_found"][:5]:
                            _log(f"    - {item}")
                    if truth.get("open_doubts"):
                        _log("  Open doubts:")
                        for item in truth["open_doubts"][:5]:
                            _log(f"    - {item}")
                    if verdict in ("insufficient", "error", "skipped"):
                        _log("  >>> No library edit. Evidence does not support a change after escalation.")
                        record_research_attempt(card, truth)
                        notes.append(
                            _claim_note(
                                card,
                                phase,
                                verdict,
                                summary,
                                truth,
                                ["recorded unresolved attempt; depths exhausted"],
                            )
                        )
                        unresolved.add(cid)
                        exhausted_ids.add(cid)
                        time.sleep(delay)
                        continue
                    remaining = max_actions - actions
                    ops = controller.propose(card, truth)[:remaining]

                if not ops:
                    _log("  >>> Controller proposed no edits.")
                    notes.append(
                        _claim_note(card, phase, verdict, summary, truth, ["critic decisive but no ops proposed"])
                    )
                    unresolved.add(cid)
                    exhausted_ids.add(cid)
                    time.sleep(delay)
                    continue

                accepted, quit_session = confirm_ops(ops, interactive=interactive)
                if phase == "substantiate" and not any(
                    op.op in ("validate", "modify", "remove") for op in accepted
                ):
                    unresolved.add(cid)
                for op in accepted:
                    controller.remember(op)
                    note_actions.append(f"{op.op}: {op.reason[:160]}")
                notes.append(_claim_note(card, phase, verdict, summary, truth, note_actions or ["(none accepted)"]))
                accepted_round.extend(accepted)
                actions += len(accepted)
                progressed = progressed or bool(accepted)
                time.sleep(delay)
                if quit_session:
                    break

        if accepted_round:
            _log("\n" + "=" * 60)
            _log("  APPLY")
            _log("=" * 60)
            apply_ops(accepted_round, auto_commit=auto_commit, interactive=interactive)

        if quit_session or only_id:
            break
        if not continuous:
            break
        # Exhausted depth on a claim still counts as research progress for the
        # medium; continue into expand/seed. Stop only when a round neither
        # applied edits nor ran any search/verify work.
        if not progressed and not researched_this_round:
            _log("\nNo further corroborated edits or researchable claims. Stopping.")
            break
        if round_n >= 30:
            _log("\nRound cap reached.")
            break

    report_path = write_research_report(
        notes,
        session_meta={"actions": actions, "features": feats.to_dict(), "evidence_mode": evidence_mode},
    )
    _log("\n" + "=" * 60)
    _log("  RESEARCH REPORT")
    _log("=" * 60)
    _log(
        format_research_report(
            notes,
            session_meta={"actions": actions, "features": feats.to_dict(), "evidence_mode": evidence_mode},
        )
    )
    _log(f"Wrote basis report → {report_path}")

    if unresolved:
        _log(f"\nUnsettled this session ({len(unresolved)}): {', '.join(sorted(unresolved))}")


# ---------------------------------------------------------------------------
# 8. One-card commands
# ---------------------------------------------------------------------------

def _card_or_die(card_id: str) -> dict:
    library = load_library_cards()
    for card in library:
        if card.get("id") == card_id:
            return card
    raise SystemExit(f"No card with id {card_id!r} in {CARDS_PATH}.")


def command_add(args) -> None:
    """Stage or commit one claim typed by the operator."""
    card = card_from_claim(
        args.claim,
        style=args.style,
        target=args.target,
        variable=args.variable,
        phase=args.phase,
        confidence=min(DRAFT_CONFIDENCE_CAP, args.confidence),
        reference=args.reference or "claims-controller",
        refines=args.refines,
    )
    card["status"] = "draft"
    card["origin"] = "controller:add"
    op = ClaimOp("add", "Manual add.", card=card)
    print(format_op(op))
    accepted, quit_session = confirm_ops([op], interactive=args.interactive, force_remove=True)
    if quit_session or not accepted:
        print("Nothing applied.")
        return
    apply_ops(
        accepted,
        auto_commit=args.commit,
        interactive=args.interactive,
        force_commit_drafts=args.commit,
    )


def _manual_rewrite(card: dict, claim_text: str, why: str) -> dict:
    """Replace the sentence and keep every other field, including corroboration."""
    updated = dict(card)
    revised = " ".join((claim_text or "").split())
    if not revised or _norm_claim(revised) == _norm_claim(card.get("claim") or ""):
        raise SystemExit("modify --claim must be a different sentence.")
    updated["prior_claim"] = card.get("claim")
    updated["claim"] = revised
    updated["revised_at"] = date.today().isoformat()
    updated["revision_note"] = (why or "Manual edit.")[:300]
    return updated


def command_modify(args) -> None:
    """Rewrite one card, either from --claim or from a fresh evidence pass."""
    card = _card_or_die(args.id)
    if args.claim:
        updated = _manual_rewrite(card, args.claim, args.reason or "Manual edit.")
        op = ClaimOp("modify", args.reason or "Manual edit.", card=updated)
        accepted, quit_session = confirm_ops([op], interactive=args.interactive, force_remove=True)
        if quit_session or not accepted:
            print("Nothing applied.")
            return
        apply_ops(accepted, auto_commit=args.commit or not args.interactive, interactive=args.interactive)
        return

    use_web = _use_web_from_args(args)
    verdict, summary, truth = research_claim(card, use_web=use_web)
    print(f"Evidence ({'external' if use_web else 'internal'}): {summary}")
    controller = ClaimsController(load_library_cards(), allow_propagate=not args.no_propagate)
    ops = controller.propose(card, truth)
    if not ops:
        print("Evidence did not justify a modification.")
        return
    accepted, quit_session = confirm_ops(ops, interactive=args.interactive)
    if quit_session or not accepted:
        print("Nothing applied.")
        return
    apply_ops(accepted, auto_commit=args.commit or not args.interactive, interactive=args.interactive)


def command_remove(args) -> None:
    """Archive one card. Confirms when --interactive is set."""
    card = _card_or_die(args.id)
    op = ClaimOp("remove", args.reason or "Removed by claims controller.", card_id=card["id"])
    print(format_op(op))
    print(f"      {card.get('claim')}")
    accepted, quit_session = confirm_ops([op], interactive=args.interactive, force_remove=True)
    if quit_session or not accepted:
        print("Nothing applied.")
        return
    apply_ops(accepted, auto_commit=True, interactive=False)


def command_propagate(args) -> None:
    """Draft specialize / expand cards for one existing claim."""
    card = _card_or_die(args.id)
    truth = card.get("corroboration") or {}
    use_web = _use_web_from_args(args)
    if use_web and truth.get("verdict") not in ("support", "conflict"):
        verdict, summary, truth = research_claim(card, use_web=True)
        print(f"Evidence (external): {summary} ({verdict})")
    elif not use_web and truth.get("verdict") not in ("support", "conflict"):
        verdict, summary, truth = research_claim(card, use_web=False)
        print(f"Evidence (internal): {summary} ({verdict})")
    controller = ClaimsController(load_library_cards(), allow_propagate=True)
    ops = controller.propose(card, truth, propagate_unproven=True)
    if not ops:
        print("No context-specific or expansive drafts survived dedup.")
        return
    accepted, quit_session = confirm_ops(ops, interactive=args.interactive, force_remove=True)
    if quit_session or not accepted:
        print("Nothing applied.")
        return
    apply_ops(
        accepted,
        auto_commit=args.commit,
        interactive=args.interactive,
        force_commit_drafts=args.commit,
    )


# ---------------------------------------------------------------------------
# 9. CLI parser and main entry
# ---------------------------------------------------------------------------

def _use_web_from_args(args) -> bool:
    """True for external evidence. --evidence wins; --no-web means internal."""
    if getattr(args, "evidence", None) == "internal":
        return False
    if getattr(args, "evidence", None) == "external":
        return True
    return not bool(getattr(args, "no_web", False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Claims controller: add, modify, remove, and propagate dough-research claims from evidence."
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="research",
        choices=["research", "add", "modify", "remove", "propagate"],
        help="research scans weak claims. The other commands act on one card.",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Confirm each operation.")
    parser.add_argument(
        "--until-done",
        action="store_true",
        help="Continue through substantiate → expand → seed until bounds are hit.",
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="Same continuous phases as --until-done: expand supported claims and seed coverage gaps.",
    )
    parser.add_argument("--max-actions", type=int, default=10, help="Cap on applied operations per session.")
    parser.add_argument("--batch-size", type=int, default=5, help="Cards to research per round.")
    parser.add_argument("--confidence-bar", type=float, default=0.65, help="Claims below this are researched first.")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between web lookups.")
    parser.add_argument("--commit", action="store_true", help="Write new drafts into cards.jsonl and rebuild the index.")
    add_feature_arguments(parser, include_recommend=False)
    parser.add_argument(
        "--evidence",
        choices=["internal", "external"],
        default=None,
        help="internal = library neighbors only. external = library + web (default).",
    )
    parser.add_argument("--no-web", action="store_true", help="Alias for --evidence internal.")
    parser.add_argument("--id", default=None, help="Card id for modify, remove, propagate, or a single research target.")
    parser.add_argument("--claim", default=None, help="Claim text for add, or the replacement sentence for modify.")
    parser.add_argument("--style", default=None, help="Comma-separated style_scope for add.")
    parser.add_argument("--target", default=None, help="Comma-separated relevance_targets for add.")
    parser.add_argument("--variable", default=None, help="Comma-separated variables for add.")
    parser.add_argument("--phase", default=None, help="Comma-separated phase for add.")
    parser.add_argument("--refines", default=None, help="Comma-separated card ids the new claim sharpens.")
    parser.add_argument("--reference", default=None, help="Source URL or note for a manual add.")
    parser.add_argument("--confidence", type=float, default=0.4, help="Starting confidence for a manual add (capped at 0.45).")
    parser.add_argument("--reason", default=None, help="Note stored on a manual modify or remove.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "add":
        if not args.claim:
            raise SystemExit("add requires --claim")
        command_add(args)
        return
    if args.command == "modify":
        if not args.id:
            raise SystemExit("modify requires --id")
        command_modify(args)
        return
    if args.command == "remove":
        if not args.id:
            raise SystemExit("remove requires --id")
        command_remove(args)
        return
    if args.command == "propagate":
        if not args.id:
            raise SystemExit("propagate requires --id")
        command_propagate(args)
        return
    feats = features_from_args(args)
    research_session(
        interactive=args.interactive,
        auto_commit=args.commit,
        confidence_bar=args.confidence_bar,
        max_actions=args.max_actions,
        batch_size=args.batch_size,
        delay=args.delay,
        until_done=args.until_done,
        allow_propagate=feats.propagate,
        only_id=args.id,
        explore=args.explore,
        use_web=_use_web_from_args(args),
        features=feats,
    )


# ---------------------------------------------------------------------------
# 10. Offline self-checks
# ---------------------------------------------------------------------------

def _self_check() -> None:
    """Offline checks for edit selection, caps, and propagation filters."""
    parent = {
        "id": "cold-open",
        "claim": "Extended cold ferment makes lower-hydration dough easier to open.",
        "variables": ["hydration", "fermentation_time"],
        "style_scope": ["ny_slice"],
        "phase": "bulk_fermentation",
        "relevance_targets": ["pizza_cart_app"],
        "confidence": 0.5,
        "refines": [],
    }
    existing = {
        "id": "cold-open-lab",
        "claim": "In the lab, cold-fermented NY dough is checked by how easily it opens after the retard.",
        "variables": ["hydration", "fermentation_time"],
        "style_scope": ["ny_slice"],
        "phase": ["bulk_fermentation"],
        "relevance_targets": ["fermentation_lab_dashboard"],
        "refines": ["cold-open"],
        "confidence": 0.4,
    }
    library = [parent, existing]
    taken = ["cold-open", "cold-open-lab"]
    truth = {
        "verdict": "support",
        "score": 0.8,
        "rationale": "sources describe gluten relaxation",
        "citations": [{"title": "Cold ferment", "url": "https://example.com/cold"}],
    }
    plan = {
        "action": "modify",
        "revised_claim": "Extended cold fermentation relaxes lower-hydration NY dough so it opens with less tearing.",
        "why": "evidence names relaxation",
        "adds": [
            {
                "claim": "Protease activity during cold fermentation increases dough extensibility.",
                "variables": ["fermentation_time", "extensibility"],
                "style_scope": ["general"],
                "phase": ["bulk_fermentation"],
                "relevance_targets": ["general_reference"],
                "confidence": 0.9,
            }
        ],
        "propagations": [
            {
                "claim": "On the lab dashboard, record whether cold-retarded NY dough opens cleanly after the retard.",
                "direction": "specialize",
                "variables": ["hydration", "fermentation_time"],
                "style_scope": ["ny_slice"],
                "phase": ["bulk_fermentation"],
                "relevance_targets": ["fermentation_lab_dashboard"],
            },
            {
                "claim": "At the pizza cart, open cold-retarded NY dough while it is still cool, before it tightens.",
                "direction": "specialize",
                "variables": ["fermentation_time", "temperature"],
                "style_scope": ["ny_slice"],
                "phase": ["shaping"],
                "relevance_targets": ["pizza_cart_app"],
            },
            {
                "claim": "Extended cold fermentation increases extensibility across dough styles.",
                "direction": "expand",
                "variables": ["fermentation_time", "extensibility"],
                "style_scope": ["general"],
                "phase": ["bulk_fermentation"],
                "relevance_targets": ["general_reference"],
            },
            {
                "claim": "Extended cold ferment makes lower-hydration dough easier to open.",
                "direction": "specialize",
                "style_scope": ["ny_slice"],
                "phase": ["bulk_fermentation"],
                "relevance_targets": ["pizza_cart_app"],
                "variables": ["hydration", "fermentation_time"],
            },
        ],
    }
    ops = ops_from_plan(parent, truth, plan, library, taken, allow_propagate=True)
    kinds = [op.op for op in ops]
    assert kinds[0] == "modify", kinds
    assert ops[0].card["id"] == "cold-open"
    assert ops[0].card["prior_claim"] == parent["claim"]
    assert ops[0].card["confidence"] >= 0.75
    assert "sourcewise_supported" in (ops[0].card.get("tags") or [])
    assert "add" in kinds
    directions = [op.direction for op in ops if op.op == "propagate"]
    assert directions == ["specialize", "expand"], directions
    for op in ops:
        if op.op in ("add", "propagate"):
            assert op.card["confidence"] <= DRAFT_CONFIDENCE_CAP
            assert op.card["status"] == "draft"
    spec = next(op for op in ops if op.direction == "specialize")
    assert spec.card["refines"] == ["cold-open"]
    assert spec.card["phase"] == ["shaping"]
    expanded = next(op for op in ops if op.direction == "expand")
    assert expanded.card["expands"] == ["cold-open"]
    assert "cold-open" not in (expanded.card.get("refines") or [])

    quiet = ops_from_plan(parent, truth, plan, library, list(taken), allow_propagate=False)
    assert all(op.op != "propagate" for op in quiet)
    assert any(op.op == "add" for op in quiet)

    weak_conflict = {"verdict": "conflict", "score": 0.2, "rationale": "weak conflict", "citations": []}
    demoted = ops_from_plan(
        parent,
        weak_conflict,
        {"action": "remove", "revised_claim": None, "why": "not repairable", "adds": [], "propagations": []},
        library,
        list(taken),
    )
    assert [op.op for op in demoted] == ["modify"], demoted
    assert demoted[0].card["confidence"] <= 0.20

    conflict = {"verdict": "conflict", "score": 0.8, "rationale": "sources disagree", "citations": []}
    removed = ops_from_plan(
        parent,
        conflict,
        {"action": "remove", "revised_claim": None, "why": "not repairable", "adds": plan["adds"], "propagations": plan["propagations"]},
        library,
        list(taken),
    )
    assert [op.op for op in removed] == ["remove"]
    assert removed[0].fallback_card["status"] == "reviewed"
    assert removed[0].fallback_card["confidence"] <= 0.20

    upheld = ops_from_plan(
        parent,
        truth,
        {"action": "remove", "revised_claim": None, "why": "planner disagreed", "adds": [], "propagations": []},
        library,
        list(taken),
    )
    assert upheld[0].op == "validate"

    assert ops_from_plan(
        parent,
        {"verdict": "insufficient", "score": 0.0, "rationale": "thin", "citations": []},
        plan,
        library,
        list(taken),
    ) == []

    draft = {
        "id": "seed-draft",
        "claim": "Sicilian pan dough needs a longer bake than Neapolitan.",
        "confidence": 0.4,
        "status": "draft",
        "style_scope": ["sicilian"],
        "variables": ["bake_time"],
    }
    supported = {
        "id": "supported-parent",
        "claim": "Cold fermentation improves extensibility.",
        "confidence": 0.9,
        "status": "reviewed",
        "style_scope": ["ny_slice"],
        "variables": ["fermentation_time"],
        "corroboration": {"verdict": "support", "score": 0.9},
    }
    phase, batch = select_batch([supported, draft], set(), 0.65, 5, allow_propagate=True)
    assert phase == "substantiate" and batch[0]["id"] == "seed-draft"
    phase, batch = select_batch([supported], set(), 0.65, 5, allow_propagate=True)
    assert phase == "expand" and batch[0]["id"] == "supported-parent"
    phase, batch = select_batch([supported], set(), 0.65, 5, allow_propagate=False)
    assert phase == "seed" and batch == []
    phase, batch = select_batch(
        [supported],
        set(),
        0.65,
        5,
        allow_propagate=False,
        features=ResearchFeatures(expand=False, seed=False),
    )
    assert phase == "done" and batch == []
    verify_feats = ResearchFeatures(
        expand=False, seed=False, propagate=False, evidence_adds=False, focused_propagate=False
    )
    quiet_ops = ops_from_plan(parent, truth, plan, library, list(taken), features=verify_feats)
    assert all(op.op in ("validate", "modify", "remove") for op in quiet_ops)
    assert not any(op.op in ("add", "propagate") for op in quiet_ops)
    queries = _search_queries(draft)
    assert queries[0] == draft["claim"]
    assert any("bake" in q.lower() for q in queries)
    report = format_research_report(
        [
            {
                "id": "seed-draft",
                "claim": draft["claim"],
                "phase": "substantiate",
                "verdict": "insufficient",
                "summary": "thin",
                "conclusion": "Claim unresolved; sources were inadequate after escalation.",
                "found": [],
                "not_found": ["direct bake-temp comparison"],
                "open_doubts": ["steel pan vs stone bake profile"],
                "pages_ok": [],
                "pages_failed": [],
                "urls_considered": [],
                "actions": ["recorded unresolved attempt"],
            }
        ],
        session_meta={"actions": 0},
    )
    assert "Research loop report" in report
    assert "Not found" in report
    assert "direct bake-temp comparison" in report
    print("claims controller self-check ok")


if __name__ == "__main__":
    main()
