"""
passive-research.py — Claims controller for the dough research library.

Research lifecycle use cases:

  1. select_work
     Identify weak, unverified, or contradicted claims that need the next
     research pass.

  2. retrieve_evidence
     Fetch nearby library cards and web evidence for one card or a small batch.

  3. assess_truth
     Evaluate support / conflict / insufficient evidence using the configured
     critic model and/or local fallback path.

  4. synthesize_actions
     Convert evidence into add / modify / validate / remove / propagate actions.

  5. enforce_policy
     Apply deterministic claim quality gates: novelty, scope, confidence cap,
     repairability, and archival safety.

  6. verify_proposals
     Deduplicate, validate relationships, and ensure the generated card set is
     structurally coherent before writing.

  7. commit
     Stage or commit accepted cards / edits, rebuild the vector index, and log
     what changed.

  8. schedule_next_cycle
     Revisit unresolved or newly generated claims in the next loop pass.

The loop is intentionally phase-aware so the same model can be debugged and
verified at each step instead of letting the whole workflow hide behind a single
monolithic action.

The controller turns fresh evidence into four library operations:

  add         A new atomic claim the evidence states. Staged as a draft.
  modify      Rewrite an existing claim when evidence corrects the wording.
              The card id stays. The previous sentence is kept on prior_claim.
  remove      Archive a contradicted card to archive.jsonl. Nothing is hard-deleted.
  propagate   Fan a claim out into learning cards:
              - specialize: same mechanism, scoped to a style, phase, or workflow
                (fermentation lab, pizza cart, mise en place)
              - expand: a broader style or an adjacent variable

validate is the no-edit case: evidence agrees, so confidence rises and the
sentence stays. research (the default command) scans weak claims and proposes
these operations. New cards stay in candidates.jsonl unless --commit is set.
Existing-card edits are written to cards.jsonl when you are not in --interactive
mode, or after you confirm.

Safety:
  - A claim is researched at most once per session.
  - --max-actions caps how many operations are applied.
  - Remove is not applied automatically. Without --interactive it is recorded
    as a confidence demotion so the conflict is not lost and the loop can stop.
  - Propagated and added cards are drafts capped at confidence 0.45.
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

load_env()

ARCHIVE_PATH = "archive.jsonl"
DRAFT_CONFIDENCE_CAP = 0.45
DEDUP_SIMILARITY = 0.82
PROPAGATION_LIMIT = 3
ADD_LIMIT = 2

RESEARCH_LIFECYCLE = {
    "select_work": {
        "purpose": "choose the next weak, stale, or contradicted claim(s) for review",
        "tooling": "deterministic queue selection; no model call required",
    },
    "retrieve_evidence": {
        "purpose": "collect nearby cards and external evidence for one claim",
        "tooling": "Voyage retrieval + optional DuckDuckGo / page fetch; model decides nothing yet",
    },
    "assess_truth": {
        "purpose": "resolve support / conflict / insufficient and gather citations",
        "tooling": "critic model (gemini / claude / local) with explicit fallback behavior",
    },
    "synthesize_actions": {
        "purpose": "translate evidence into validate, modify, add, remove, or propagate proposals",
        "tooling": "planner model with deterministic fallback when the model is unavailable",
    },
    "enforce_policy": {
        "purpose": "apply novelty, scope, confidence, and archival safety rules",
        "tooling": "deterministic policy layer and set-based deduplication",
    },
    "verify_proposals": {
        "purpose": "check that drafted operations are coherent, non-duplicative, and structurally valid",
        "tooling": "schema validation, dedup checks, and optional second-pass model review",
    },
    "commit": {
        "purpose": "write accepted updates, archive removals, and rebuild the vector index",
        "tooling": "library IO + index rebuild",
    },
    "schedule_next_cycle": {
        "purpose": "revisit unsettled claims or newly produced drafts in the next round",
        "tooling": "bounded session loop with max-actions and claim deduplication",
    },
}


class ClaimLifecyclePolicy:
    """Explicit policy layer for creation, revision, propagation, and archival.

    The values here are intentionally conservative: a claim must be novel,
    evidence-backed, and scope-coherent before it becomes part of the library or a
    propagated branch of the learning graph.
    """

    def __init__(
        self,
        support_threshold: float = 0.60,
        draft_cap: float = DRAFT_CONFIDENCE_CAP,
        propagation_threshold: float = 0.55,
        archive_conflict_threshold: float = 0.35,
    ):
        self.support_threshold = float(support_threshold)
        self.draft_cap = float(draft_cap)
        self.propagation_threshold = float(propagation_threshold)
        self.archive_conflict_threshold = float(archive_conflict_threshold)

    def describe(self) -> str:
        """Return a compact summary of the policy contract."""
        return (
            "claim-lifecycle-policy: "
            "support_threshold={support} draft_cap={draft} propagation_threshold={prop} "
            "archive_conflict_threshold={archive}".format(
                support=self.support_threshold,
                draft=self.draft_cap,
                prop=self.propagation_threshold,
                archive=self.archive_conflict_threshold,
            )
        )

    def should_create_claim(self, card: Optional[dict], truth: Optional[dict], duplicate_fn: Optional[Callable[[str], bool]] = None) -> bool:
        """True when a new card is eligible for creation as a draft.

        Safety gates:
          - verdict must support enough evidence to justify a draft
          - novel sentence (deduped against existing cards)
          - must have a meaningful scope or target, not a vague restatement
        """
        if not card:
            return False
        verdict = (truth or {}).get("verdict")
        score = float((truth or {}).get("score") or 0.0)
        claim = (card.get("claim") or "").strip()
        if verdict not in ("support",):
            return False
        if score < self.support_threshold:
            return False
        if not claim:
            return False
        if duplicate_fn and duplicate_fn(claim):
            return False
        if not card.get("style_scope") and not card.get("relevance_targets") and not card.get("phase"):
            return False
        return True

    def should_modify_claim(self, card: Optional[dict], truth: Optional[dict], revised_claim: Optional[str] = None) -> bool:
        """True when the existing claim should be rewritten, not revalidated."""
        if not card:
            return False
        verdict = (truth or {}).get("verdict")
        if verdict != "support":
            return False
        if not revised_claim:
            return False
        if _norm_claim(revised_claim) == _norm_claim(card.get("claim") or ""):
            return False
        return True

    def should_archive_claim(self, card: Optional[dict], truth: Optional[dict], repairable: bool = False) -> bool:
        """True when a contradiction is strong enough to archive the claim.

        Conflict alone is not enough; it must remain unresolved and not repairable
        by narrowing the claim.
        """
        if not card:
            return False
        verdict = (truth or {}).get("verdict")
        score = float((truth or {}).get("score") or 0.0)
        if verdict != "conflict":
            return False
        if repairable:
            return False
        if score < self.archive_conflict_threshold:
            return False
        return True

    def should_propagate_claim(self, parent: Optional[dict], child: Optional[dict], direction: str) -> bool:
        """True when a child draft is meaningfully different and worth branching.

        This is intentionally stricter than "direction is present". A child must
        change scope or generality in a way that helps the learning system become
        more useful, not just restate the same claim under a different label.
        """
        if not parent or not child:
            return False
        direction = str(direction or "").lower()
        if direction not in ("specialize", "expand"):
            return False
        if direction == "specialize":
            allowed = _context_shift(parent, child)
        else:
            allowed = _broadens(parent, child)
        if not allowed:
            return False
        score = float((child.get("confidence") or 0.0))
        if score > self.draft_cap:
            return False
        if not (child.get("claim") or "").strip():
            return False
        return True

    def phase_gate(self, phase: str, card: Optional[dict] = None, truth: Optional[dict] = None) -> bool:
        """A single entry point for phase-driven safety and debug visibility."""
        phase = str(phase or "").strip()
        if phase == "select_work":
            return True
        if phase == "retrieve_evidence":
            return bool(card and card.get("claim"))
        if phase == "assess_truth":
            return bool(card and card.get("claim"))
        if phase == "synthesize_actions":
            return bool(card and (truth or {}).get("verdict") in ("support", "conflict"))
        if phase == "enforce_policy":
            return True
        if phase == "verify_proposals":
            return True
        if phase == "commit":
            return True
        if phase == "schedule_next_cycle":
            return True
        return True


def lifecycle_phase_summary() -> str:
    """Human-readable ordering of the research lifecycle use cases."""
    return " -> ".join(RESEARCH_LIFECYCLE.keys())


def lifecycle_phase_note(phase: str) -> str:
    """Return a short note for a phase so debugging output stays traceable."""
    info = RESEARCH_LIFECYCLE.get(phase, {})
    purpose = info.get("purpose") or "unnamed phase"
    tooling = info.get("tooling") or "generic tooling"
    return f"{phase}: {purpose} | tooling={tooling}"


# ===========================================================================
# Library I/O
# ===========================================================================
