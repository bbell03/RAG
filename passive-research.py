"""passive-research.py — Claims controller for the dough research library.

This script intentionally reads top-to-bottom as a sequential research lifecycle:

  1. Selection: find weak / unverified claims.
  2. Evidence: retrieve sources and assess support / conflict / insufficient.
  3. Planning: build candidate library operations.
  4. Policy: enforce deterministic claim lifecycle rules.
  5. Verification: validate candidate operations before writes.
  6. Write path: stage or commit updates and archive removals.
  7. Repeat: revisit unresolved claims in bounded rounds.

The original behavior is retained. The refactor only adds clearer sectioning,
inline notes, and explicit lifecycle boundaries so the logic is easier to test.
"""

import argparse
import json
import time
from datetime import date
from typing import Callable, Dict, List, Optional, Set

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
from synthesis import load_env
from validate import evaluate_truth, is_url

# ---------------------------------------------------------------------------
# 0. Setup and constants
# ---------------------------------------------------------------------------
load_env()

ARCHIVE_PATH = "archive.jsonl"
DRAFT_CONFIDENCE_CAP = 0.45
DEDUP_SIMILARITY = 0.82
PROPAGATION_LIMIT = 3
ADD_LIMIT = 2


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
    return updated


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
    refines = [link_id] if link_field == "refines" else []
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
    if link_field == "expands":
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


def ops_from_plan(
    card: dict,
    truth: dict,
    plan: dict,
    library: List[dict],
    taken: List[str],
    allow_propagate: bool = True,
    propagate_unproven: bool = False,
    duplicate_fn: Optional[Callable[[str], bool]] = None,
) -> List[ClaimOp]:
    """Turn a planner payload into concrete add / modify / remove / propagate ops."""
    verdict = (truth or {}).get("verdict")
    if verdict not in ("support", "conflict"):
        return []

    action = _reconcile_action(verdict, plan)
    why = plan.get("why") or (truth or {}).get("rationale") or ""
    parent_id = card.get("id")
    ops: List[ClaimOp] = []

    if action == "modify" and plan.get("revised_claim"):
        rewritten = _apply_evidence(card, truth, plan["revised_claim"], why)
        if rewritten.get("claim") != card.get("claim"):
            ops.append(ClaimOp("modify", why, card=rewritten, parent_id=parent_id))
        else:
            action = "validate"
    if action == "validate":
        ops.append(ClaimOp("validate", why, card=_apply_evidence(card, truth, why=why), parent_id=parent_id))
    elif action == "remove":
        ops.append(
            ClaimOp(
                "remove",
                why or "Contradicted and not repairable by a narrower claim.",
                card_id=parent_id,
                parent_id=parent_id,
                fallback_card=_apply_evidence(card, truth, why=why),
            )
        )

    if verdict != "support" and not propagate_unproven:
        return ops
    if action == "remove":
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
        known.add(norm)
        return True

    if verdict == "support":
        for raw in (plan.get("adds") or [])[:ADD_LIMIT]:
            if not _accept_text(raw.get("claim") or ""):
                continue
            draft = _shape_draft(raw, taken, reference, "controller:add", parent_id, "refines")
            draft["refines"] = []
            ops.append(ClaimOp("add", why, card=draft, parent_id=parent_id))

    if allow_propagate:
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
            if direction == "specialize":
                key = _scope_key(draft)
                if key in scopes:
                    taken[:] = [item for item in taken if item != draft.get("id")]
                    continue
                scopes.add(key)
            ops.append(
                ClaimOp(
                    "propagate",
                    why,
                    card=draft,
                    parent_id=parent_id,
                    direction=direction,
                )
            )
            emitted += 1
    return ops


# ---------------------------------------------------------------------------
# 4. Evidence retrieval and truth checking
# ---------------------------------------------------------------------------

def research_claim(card: dict, use_web: bool = True) -> tuple:
    """Search evidence for one claim. Returns (verdict, summary, truth)."""
    claim = card["claim"]
    try:
        hits = query(claim, top_k=3)
    except Exception as exc:
        truth = {"verdict": "error", "score": 0.0, "rationale": f"Retrieval failed: {exc}", "citations": []}
        return "error", truth["rationale"], truth

    try:
        truth = evaluate_truth(claim, hits, use_web=use_web)
    except Exception as exc:
        truth = {"verdict": "error", "score": 0.0, "rationale": f"Truth evaluation failed: {exc}", "citations": []}
        return "error", truth["rationale"], truth

    verdict = truth.get("verdict") or "insufficient"
    score = float(truth.get("score") or 0.0)
    rationale = truth.get("rationale") or "No rationale provided."
    citations = truth.get("citations") or []
    first_url = next((c.get("url") for c in citations if isinstance(c, dict) and c.get("url")), None)
    if verdict == "support":
        summary = f"Supported (score: {score:.2f}) via {first_url or 'evidence'} - {rationale}"
    elif verdict == "conflict":
        summary = f"Contradicted (score: {score:.2f}) - {rationale}"
    elif verdict in ("error", "skipped"):
        summary = rationale
    else:
        summary = f"Insufficient evidence - {rationale}"
        verdict = "insufficient" if verdict not in ("error", "skipped") else verdict
    return verdict, summary, truth


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
    ):
        self.library = list(library)
        self.by_id = {card["id"]: card for card in library if card.get("id")}
        self.allow_propagate = allow_propagate
        self.planner = planner
        self.taken = [card["id"] for card in library if card.get("id")]

    def propose(
        self,
        card: dict,
        truth: dict,
        propagate_unproven: bool = False,
        check_similarity: bool = True,
    ) -> List[ClaimOp]:
        """Plan library edits for one card. Does not write."""
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
        )
        if propagate_unproven:
            ops = [op for op in ops if op.op == "propagate"]
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
            print(f"  Planner unavailable ({exc}). Using the critic verdict only.")
            return _fallback_plan(truth or {})

    def remember(self, op: ClaimOp) -> None:
        """Keep later proposals from duplicating a card accepted earlier in the session."""
        if op.card and op.op in ("add", "propagate"):
            self.library.append(op.card)
            if op.card.get("id") and op.card["id"] not in self.taken:
                self.taken.append(op.card["id"])
        elif op.card and op.op in ("validate", "modify"):
            self.by_id[op.card["id"]] = op.card


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
) -> None:
    """Scan weak claims and run the controller on each piece of evidence."""
    actions = 0
    unresolved: Set[str] = set()
    round_n = 0
    print("\n" + "=" * 60)
    print("  CLAIMS CONTROLLER")
    print(
        f"  Confidence {confidence_bar} | batch {batch_size} | "
        f"max actions {max_actions} | propagate {allow_propagate}"
    )
    print("=" * 60)

    while True:
        round_n += 1
        library = load_library_cards()
        controller = ClaimsController(library, allow_propagate=allow_propagate)
        if only_id:
            needy = [card for card in library if card.get("id") == only_id]
            if not needy:
                print(f"No card with id {only_id}.")
                return
        else:
            needy = [
                card
                for card in library
                if card.get("id") not in unresolved and is_needing_substantiation(card, confidence_bar)
            ]
        if not needy:
            print("\nNo claims left below the confidence bar.")
            break

        print(f"\nRound {round_n}: {len(needy)} claim(s) still open. Scanning {min(batch_size, len(needy))}.")
        progressed = False
        quit_session = False
        accepted_round: List[ClaimOp] = []

        for card in needy[:batch_size]:
            if actions >= max_actions:
                print(f"\n[Limit] Max actions ({max_actions}) reached.")
                quit_session = True
                break
            cid = card.get("id")
            print("\n" + "-" * 60)
            print(f"Target [{cid}]")
            print(f"  \"{card.get('claim')}\"")
            print(
                f"  Confidence {float(card.get('confidence') or 0):.2f} | "
                f"status {card.get('status', 'draft')} | "
                f"style {card.get('style_scope')} | target {card.get('relevance_targets')}"
            )
            verdict, summary, truth = research_claim(card, use_web=True)
            print(f"  Evidence: {summary}")
            if verdict in ("insufficient", "error", "skipped"):
                print("  >>> No library edit. Evidence does not support a change.")
                unresolved.add(cid)
                time.sleep(delay)
                continue

            remaining = max_actions - actions
            ops = controller.propose(card, truth)[:remaining]
            if not ops:
                print("  >>> Controller proposed no edits.")
                unresolved.add(cid)
                time.sleep(delay)
                continue

            accepted, quit_session = confirm_ops(ops, interactive=interactive)
            if not any(op.op in ("validate", "modify", "remove") for op in accepted):
                unresolved.add(cid)
            for op in accepted:
                controller.remember(op)
            accepted_round.extend(accepted)
            actions += len(accepted)
            progressed = progressed or bool(accepted)
            time.sleep(delay)
            if quit_session:
                break

        if accepted_round:
            print("\n" + "=" * 60)
            print("  APPLY")
            print("=" * 60)
            apply_ops(accepted_round, auto_commit=auto_commit, interactive=interactive)

        if quit_session or not until_done or only_id:
            break
        if not progressed:
            print("\nNo further corroborated edits. Stopping.")
            break
        if round_n >= 20:
            print("\nRound cap reached.")
            break

    if unresolved:
        print(f"\nUnsettled this session ({len(unresolved)}): {', '.join(sorted(unresolved))}")


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

    verdict, summary, truth = research_claim(card, use_web=not args.no_web)
    print(f"Evidence: {summary}")
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
    if not args.no_web and truth.get("verdict") not in ("support", "conflict"):
        verdict, summary, truth = research_claim(card, use_web=True)
        print(f"Evidence: {summary} ({verdict})")
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
    parser.add_argument("--until-done", action="store_true", help="Keep scanning until claims settle or evidence runs out.")
    parser.add_argument("--max-actions", type=int, default=10, help="Cap on applied operations per session.")
    parser.add_argument("--batch-size", type=int, default=5, help="Weak claims to research per round.")
    parser.add_argument("--confidence-bar", type=float, default=0.65, help="Claims below this are researched.")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between web lookups.")
    parser.add_argument("--commit", action="store_true", help="Write new drafts into cards.jsonl and rebuild the index.")
    parser.add_argument("--no-propagate", action="store_true", help="Do not fan claims out into workflow drafts.")
    parser.add_argument("--no-web", action="store_true", help="Skip web lookup on modify/propagate.")
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
    research_session(
        interactive=args.interactive,
        auto_commit=args.commit,
        confidence_bar=args.confidence_bar,
        max_actions=args.max_actions,
        batch_size=args.batch_size,
        delay=args.delay,
        until_done=args.until_done,
        allow_propagate=not args.no_propagate,
        only_id=args.id,
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

    conflict = {"verdict": "conflict", "score": 0.2, "rationale": "sources disagree", "citations": []}
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
    print("claims controller self-check ok")


if __name__ == "__main__":
    main()
