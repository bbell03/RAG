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
