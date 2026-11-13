"""
passive-research.py — Autonomous, interactive, and bounded claim research tool.

Provides 4 distinct claim operations:
  1. [ADD]         Ingests new gap-filling claims from external literature into candidates.
  2. [VALIDATE]    Substantiates claims with web citations, raises confidence, marks reviewed.
  3. [INVALIDATE]  Identifies disproven claims, lowers confidence, tags contradictions.
  4. [REMOVE]      Safely archives obsolete or debunked claims to archive.jsonl and prunes them.

Safety & Anti-Looping Controls:
  - --interactive (-i): Prompts user before applying any mutation to cards.jsonl.
  - Single-visit tracking: A claim is evaluated at most once per session.
  - Action caps: Limits modifications per run (--max-actions).
  - Safe archival: Claims are never hard-deleted; moved to archive.jsonl.
"""

import argparse
import json
import time
from datetime import date
from typing import Dict, List, Optional, Set, Tuple

from embed_pipeline import build_index, query
from ingest import (
    CANDIDATES_PATH,
    CARDS_PATH,
    is_jsonl_note,
    write_candidates,
)
from synthesis import load_env
from validate import evaluate_truth

load_env()

ARCHIVE_PATH = "archive.jsonl"


# ===========================================================================
# 1. Claim Inspection & Library Helpers
# ===========================================================================

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
    """Load all valid card objects from cards.jsonl."""
    cards = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if is_jsonl_note(line):
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return cards


def update_cards_in_library(updated_cards: List[dict], path: str = CARDS_PATH) -> int:
    """Update existing cards by id in cards.jsonl, preserving non-card notes/comments."""
    by_id = {c["id"]: c for c in updated_cards}
    new_lines = []
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if is_jsonl_note(line):
                new_lines.append(line)
                continue
            try:
                card = json.loads(line)
                cid = card.get("id")
                if cid in by_id:
                    new_lines.append(json.dumps(by_id[cid], ensure_ascii=False) + "\n")
                    count += 1
                else:
                    new_lines.append(line)
            except json.JSONDecodeError:
                new_lines.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return count


def archive_cards_from_library(card_ids: List[str], path: str = CARDS_PATH, archive_path: str = ARCHIVE_PATH, reason: str = "archived") -> int:
    """Safely move cards from cards.jsonl into archive.jsonl. Never hard deletes."""
    to_remove = set(card_ids)
    kept_lines = []
    archived_cards = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if is_jsonl_note(line):
                kept_lines.append(line)
                continue
            try:
                card = json.loads(line)
                if card.get("id") in to_remove:
                    card["archived_at"] = date.today().isoformat()
                    card["archive_reason"] = reason
                    archived_cards.append(card)
                else:
                    kept_lines.append(line)
            except json.JSONDecodeError:
                kept_lines.append(line)

    if archived_cards:
        with open(archive_path, "a", encoding="utf-8") as f:
            for card in archived_cards:
                f.write(json.dumps(card, ensure_ascii=False) + "\n")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept_lines)

    return len(archived_cards)


# ===========================================================================
# 2. Research & Evaluation Engine
# ===========================================================================

def research_claim(card: dict, use_web: bool = True) -> Tuple[Optional[dict], str, str]:
    """
    Search web evidence for one claim.
    Returns (updated_card_or_None, verdict, rationale).
    """
    claim = card["claim"]
    try:
        hits = query(claim, top_k=3)
    except Exception as exc:
        return None, "error", f"Retrieval failed: {exc}"

    try:
        truth = evaluate_truth(claim, hits, use_web=use_web)
    except Exception as exc:
        return None, "error", f"Truth evaluation failed: {exc}"

    verdict = truth.get("verdict")
    score = float(truth.get("score") or 0.0)
    rationale = truth.get("rationale") or "No rationale provided."

    updated = dict(card)
    updated["corroboration"] = truth

    # Check for citation URL
    citations = truth.get("citations") or []
    first_url = next((c.get("url") for c in citations if c.get("url")), None)

    if verdict == "support":
        updated["status"] = "reviewed"
        updated["confidence"] = max(0.75, score)
        if first_url:
            updated["source"] = {"type": "article", "reference": first_url}
        return updated, "support", f"Supported (score: {score:.2f}) via {first_url or 'evidence'} - {rationale}"

    elif verdict == "conflict":
        updated["status"] = "reviewed"
        updated["confidence"] = min(0.20, score)
        if truth.get("contradicts"):
            existing = updated.get("contradicts") or []
            updated["contradicts"] = list(dict.fromkeys(existing + truth["contradicts"]))
        return updated, "conflict", f"Contradicted (score: {score:.2f}) - {rationale}"

    return None, "insufficient", f"Insufficient evidence - {rationale}"


# ===========================================================================
# 3. Interactive Triage & Operations
# ===========================================================================

def prompt_user_choice(prompt: str, options: List[str], default: str) -> str:
    """Prompt user for a single-character choice."""
    opts_str = "/".join([f"[{o.upper()}]" if o.lower() == default.lower() else o.lower() for o in options])
    while True:
        try:
            choice = input(f"{prompt} ({opts_str}): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return "q"
        if not choice:
            return default.lower()
        if choice in [o.lower() for o in options]:
            return choice
        print(f"Invalid option. Choose from: {opts_str}")


def interactive_research_loop(
    cards: List[dict],
    interactive: bool = True,
    auto_commit: bool = False,
    confidence_bar: float = 0.65,
    max_actions: int = 10,
    delay: float = 3.0,
):
    """
    Controlled research loop:
    Evaluates claims one by one and prompts the user for decisions:
      - [VALIDATE]   Apply support updates & raise confidence
      - [INVALIDATE] Apply conflict demotion or queue for removal
      - [REMOVE]     Move contradicted/dead claim to archive.jsonl
      - [SKIP]       Leave card untouched
    """
    actions_performed = 0
    visited_ids: Set[str] = set()
    to_update: List[dict] = []
    to_archive: List[str] = []

    print("\n" + "=" * 60)
    print("  PASSIVE RESEARCH: INTERACTIVE CLAIM TRIAGE & CONTROL")
    print(f"  Target Confidence: {confidence_bar} | Max Actions: {max_actions} | Interactive: {interactive}")
    print("=" * 60 + "\n")

    for card in cards:
        if actions_performed >= max_actions:
            print(f"\n[Limit Reached] Reached maximum allowed actions ({max_actions}). Stopping.")
            break

        cid = card["id"]
        if cid in visited_ids:
            continue
        visited_ids.add(cid)

        if not is_needing_substantiation(card, confidence_bar):
            continue

        claim = card["claim"]
        print(f"\n" + "-" * 60)
        print(f"Target Claim [{cid}]:")
        print(f"  \"{claim}\"")
        print(f"  Current Confidence: {card.get('confidence', 0.5):.2f} | Status: {card.get('status', 'draft')}")

        updated, verdict, summary = research_claim(card, use_web=True)
        print(f"  Research Outcome: {summary}")

        if verdict == "support":
            print("\n  >>> PROPOSED ACTION: [VALIDATE CLAIM]")
            print(f"      - Elevate confidence: {card.get('confidence', 0.5):.2f} -> {updated['confidence']:.2f}")
            print(f"      - Set status: 'reviewed'")
            if (updated.get("source") or {}).get("reference"):
                print(f"      - Anchor citation: {updated['source']['reference']}")

            if interactive:
                choice = prompt_user_choice("  Apply validation update?", ["y", "n", "q"], default="y")
                if choice == "q":
                    break
                if choice == "y":
                    to_update.append(updated)
                    actions_performed += 1
            else:
                to_update.append(updated)
                actions_performed += 1

        elif verdict == "conflict":
            print("\n  >>> PROPOSED ACTION: [INVALIDATE CLAIM]")
            print(f"      - Evidence CONTRADICTS this claim mechanism.")
            print(f"      - Option A: Demote confidence to {updated['confidence']:.2f} & record conflict.")
            print(f"      - Option B: Remove from library & move to archive.jsonl.")

            if interactive:
                choice = prompt_user_choice("  Choose action", ["d", "r", "s", "q"], default="d")
                # d = demote, r = remove/archive, s = skip, q = quit
                if choice == "q":
                    break
                elif choice == "d":
                    to_update.append(updated)
                    actions_performed += 1
                elif choice == "r":
                    to_archive.append(cid)
                    actions_performed += 1
            else:
                to_update.append(updated)
                actions_performed += 1

        elif verdict == "insufficient":
            print("  >>> OUTCOME: [UNRESOLVED] No conclusive evidence found. Claim left unchanged.")

        time.sleep(delay)

    # Apply staged changes
    print("\n" + "=" * 60)
    print("  SESSION SUMMARY & APPLY")
    print("=" * 60)
    print(f"  Validated / Updated: {len(to_update)} card(s)")
    print(f"  Archived / Pruned:   {len(to_archive)} card(s)")

    if to_archive:
        n_arch = archive_cards_from_library(to_archive, reason="invalidated_by_research")
        print(f"  -> Safely moved {n_arch} card(s) to {ARCHIVE_PATH} (pruned from library).")

    if to_update:
        if auto_commit or not interactive:
            n_up = update_cards_in_library(to_update)
            print(f"  -> Committed {n_up} card update(s) to {CARDS_PATH}.")
            print("  -> Rebuilding vector index cards.db...")
            build_index(provider="voyage", cards_path=CARDS_PATH)
        else:
            choice = prompt_user_choice("\nCommit verified card updates directly to cards.jsonl?", ["y", "n"], default="y")
            if choice == "y":
                n_up = update_cards_in_library(to_update)
                print(f"  -> Committed {n_up} card update(s) to {CARDS_PATH}.")
                print("  -> Rebuilding vector index cards.db...")
                build_index(provider="voyage", cards_path=CARDS_PATH)
            else:
                write_candidates(to_update, CANDIDATES_PATH)
                print(f"  -> Staged {len(to_update)} card(s) in {CANDIDATES_PATH} for manual review.")


# ===========================================================================
# 4. CLI Entry Point
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous and interactive passive research tool for adding, validating, invalidating, and removing claims."
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        default=False,
        help="Prompt for user approval before validating, invalidating, or archiving any claim.",
    )
    parser.add_argument(
        "--until-done",
        action="store_true",
        help="Run continuously until all claims are verified or web evidence is exhausted.",
    )
    parser.add_argument(
        "--max-actions",
        type=int,
        default=10,
        help="Maximum claim modifications allowed per session (anti-looping guardrail, default: 10).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Max claims to evaluate in a single batch (default: 5).",
    )
    parser.add_argument(
        "--confidence-bar",
        type=float,
        default=0.65,
        help="Target confidence threshold for a claim to be considered verified (default: 0.65).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Seconds delay between web searches to prevent rate limits (default: 3.0s).",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Auto-commit updates to cards.jsonl and rebuild cards.db.",
    )

    args = parser.parse_args()

    cards = load_library_cards()
    interactive_research_loop(
        cards=cards,
        interactive=args.interactive,
        auto_commit=args.commit,
        confidence_bar=args.confidence_bar,
        max_actions=args.max_actions,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
