"""
Recommend — Voyage (or any embedder) scores library coverage; a chat reasoner
validates the answer and drafts missing cards until a knowledge threshold.

Voyage does not run prompts. It only embeds. Plug a reasoner in REASONERS
the same way embed providers are registered.

Safety: each recommend round may call Voyage + one chat model + web fetches.
max_rounds caps the loop. Drafts stay in memory until write_candidates.
"""

import json
from typing import Callable, Dict, List, Optional, Tuple

from ingest import (
    CANDIDATES_PATH,
    commit_candidates,
    load_card_ids,
    normalize_card,
    schema_enums,
    write_candidates,
)
from validate import apply_truth, evaluate_truth, is_url

Hit = Tuple[float, dict]


def knowledge_score(hits: List[Hit], sim_floor: float = 0.32) -> float:
    """Retrieval certainty: similarity * stored card confidence, rank-weighted.

    Hits below sim_floor do not count. Empty list → 0.0. Result in [0, 1].
    """
    if not hits:
        return 0.0
    weighted = []
    weights = []
    for i, (sim, card) in enumerate(hits):
        if sim < sim_floor:
            continue
        weight = 1.0 / (i + 1)
        conf = float(card.get("confidence") or 0.5)
        weighted.append(max(0.0, min(1.0, sim)) * max(0.0, min(1.0, conf)))
        weights.append(weight)
    if not weighted:
        best = max(sim for sim, _ in hits)
        return round(max(0.0, min(1.0, best * 0.25)), 4)
    return round(sum(s * w for s, w in zip(weighted, weights)) / sum(weights), 4)


def classify_coverage(best_sim: float, retrieval: float) -> str:
    """Library label only: duplicate | covered | refines | gap. Not truth."""
    if best_sim >= 0.82:
        return "duplicate"
    if retrieval >= 0.70:
        return "covered"
    if best_sim >= 0.45:
        return "refines"
    return "gap"


def assess_claim(claim: str, hits: List[Hit], self_id: Optional[str] = None) -> dict:
    """Score one claim against retrieved neighbors. Drops self_id so add is not a self-hit."""
    filtered = [(sim, card) for sim, card in hits if card.get("id") != self_id]
    retrieval = knowledge_score(filtered)
    best_sim, best = filtered[0] if filtered else (0.0, None)
    return {
        "claim": claim,
        "retrieval": retrieval,
        "best_sim": round(float(best_sim), 4),
        "best_id": best.get("id") if best else None,
        "best_claim": best.get("claim") if best else None,
        "status": classify_coverage(float(best_sim), retrieval),
        "hits": filtered,
    }


def assess_drafts(
    drafts: List[dict],
    retrieve_fn: Callable[[str], List[Hit]],
    check_truth: bool = True,
    use_web: bool = True,
    critic: Optional[str] = None,
) -> List[dict]:
    """Voyage library coverage, then a chat-model truth evaluation.

    Mutates each draft in-place with corroboration (and maybe contradicts).
    retrieve_fn is called once per draft — each call may hit Voyage.
    """
    reports = []
    for card in drafts:
        hits = retrieve_fn(card["claim"])
        row = assess_claim(card["claim"], hits, self_id=card.get("id"))
        row["id"] = card.get("id")
        extra = []
        ref = ((card.get("source") or {}).get("reference") or "")
        if is_url(ref):
            extra.append(ref)
        if check_truth:
            truth = evaluate_truth(card["claim"], row["hits"], extra_urls=extra, use_web=use_web, critic=critic)
            row["truth"] = truth
            card["corroboration"] = {k: truth[k] for k in ("verdict", "score", "rationale", "citations", "contradicts", "checked_at") if k in truth}
            if truth.get("contradicts"):
                existing = card.get("contradicts") or []
                card["contradicts"] = list(dict.fromkeys(existing + truth["contradicts"]))
        reports.append(row)
    return reports


def combined_score(retrieval: float, coverage: Optional[float]) -> float:
    """0.6 * retrieval + 0.4 * composer coverage. Coverage None → retrieval only."""
    if coverage is None:
        return round(retrieval, 4)
    return round(0.6 * retrieval + 0.4 * max(0.0, min(1.0, coverage)), 4)


def _hits_payload(hits: List[Hit]) -> List[dict]:
    """Slim cards for the composer prompt. Drops embeddings and unused fields."""
    out = []
    for sim, card in hits:
        out.append(
            {
                "id": card.get("id"),
                "claim": card.get("claim"),
                "confidence": card.get("confidence"),
                "variables": card.get("variables"),
                "style_scope": card.get("style_scope"),
                "relevance_targets": card.get("relevance_targets"),
                "similarity": round(float(sim), 4),
            }
        )
    return out


def _parse_reasoner_json(text) -> dict:
    """Accept a dict or JSON string. Coverage clamped to [0, 1]. Bad cards list → []."""
    payload = json.loads(text) if isinstance(text, str) else text
    if not isinstance(payload, dict):
        raise ValueError("reasoner returned non-object JSON")
    coverage = payload.get("coverage")
    if coverage is not None:
        coverage = max(0.0, min(1.0, float(coverage)))
    cards = payload.get("cards") or []
    if not isinstance(cards, list):
        cards = []
    rec = payload.get("recommendation")
    if rec is not None:
        rec = str(rec).strip() or None
    gaps = [str(g) for g in (payload.get("gaps") or []) if str(g).strip()]
    return {
        "coverage": coverage,
        "ready": bool(payload.get("ready")),
        "recommendation": rec,
        "gaps": gaps,
        "cards": cards,
    }


def reason_none(
    question: str,
    hits: List[Hit],
    retrieval: float,
    threshold: float,
    extra: Optional[str] = None,
) -> dict:
    """Voyage-only path: score the library, do not draft or validate in prose."""
    del question, extra
    ready = retrieval >= threshold
    top = hits[0][1] if hits else None
    return {
        "coverage": None,
        "ready": ready,
        "recommendation": (
            f"Closest card [{top['id']}]: {top['claim']}" if ready and top else None
        ),
        "gaps": (
            []
            if ready
            else ["Retrieval knowledge is below threshold. Add cards, or rerun with --reasoner gemini."]
        ),
        "cards": [],
    }


def _reason_with(
    name: str,
    question: str,
    hits: List[Hit],
    retrieval: float,
    threshold: float,
    extra: Optional[str] = None,
) -> dict:
    """Chat composer: coverage, recommendation, and optional gap cards."""
    from synthesis import make_bot

    return _parse_reasoner_json(
        make_bot(name).synthesize(
            question,
            _hits_payload(hits),
            retrieval,
            threshold,
            extra,
        )
    )


def reason_claude(
    question: str,
    hits: List[Hit],
    retrieval: float,
    threshold: float,
    extra: Optional[str] = None,
) -> dict:
    """Claude composer. Needs ANTHROPIC_API_KEY."""
    return _reason_with("claude", question, hits, retrieval, threshold, extra)


def reason_gemini(
    question: str,
    hits: List[Hit],
    retrieval: float,
    threshold: float,
    extra: Optional[str] = None,
) -> dict:
    """Gemini composer. Needs GEMINI_API_KEY."""
    return _reason_with("gemini", question, hits, retrieval, threshold, extra)


REASONERS = {"gemini": reason_gemini, "none": reason_none, "claude": reason_claude}


def cards_from_reasoner(raw_cards: List[dict], question: str) -> List[dict]:
    """Normalize composer drafts. Bad cards are skipped. Confidence capped at 0.45."""
    enums = schema_enums()
    taken = load_card_ids()
    doc = {"source_type": "conversation", "reference": f"recommend: {question[:120]}"}
    out = []
    for raw in raw_cards:
        try:
            card = normalize_card(raw, doc, enums, taken)
        except ValueError:
            continue
        card["confidence"] = min(card["confidence"], 0.45)
        card["status"] = "draft"
        out.append(card)
    return out


def merge_hits(
    question: str,
    existing: List[Hit],
    extras: List[dict],
    embed_fn: Callable,
    card_text_fn: Callable[[dict], str],
    cosine_fn: Callable,
    top_k: int,
) -> List[Hit]:
    """Fold newly drafted cards into the hit list. Extra Voyage embed for extras only."""
    if not extras:
        return existing[:top_k]
    q_emb = embed_fn([question], input_type="query")[0]
    texts = [card_text_fn(card) for card in extras]
    embs = embed_fn(texts, input_type="document")
    merged = list(existing)
    seen = {card.get("id") for _, card in existing}
    for card, emb in zip(extras, embs):
        if card["id"] in seen:
            continue
        merged.append((cosine_fn(q_emb, emb), card))
        seen.add(card["id"])
    merged.sort(key=lambda x: x[0], reverse=True)
    return merged[:top_k]


def recommend(
    question: str,
    retrieve_fn: Callable[[], List[Hit]],
    embed_fn: Callable,
    card_text_fn: Callable[[dict], str],
    cosine_fn: Callable,
    reasoner: str = "gemini",
    threshold: float = 0.7,
    max_rounds: int = 2,
    extra_prompt: Optional[str] = None,
    top_k: int = 5,
    commit: bool = False,
    out_path: str = CANDIDATES_PATH,
    check_truth: bool = True,
    use_web: bool = True,
    critic: Optional[str] = None,
) -> dict:
    """Retrieve → score → optional chat compose/truth → maybe draft, up to max_rounds.

    Returns a report dict (ready, recommendation, hits, drafted, rounds).
    Writes candidates only if any cards were drafted. Does not rebuild cards.db.
    """
    if reasoner not in REASONERS:
        raise ValueError(f"unknown reasoner {reasoner!r}. reasoners: {sorted(REASONERS)}")
    if max_rounds < 1:
        raise ValueError("max_rounds must be >= 1")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    drafted: List[dict] = []
    last_reason: Dict = {}
    report = []

    hits = retrieve_fn()
    if critic is None and reasoner in ("claude", "gemini"):
        critic = reasoner
    for round_i in range(1, max_rounds + 1):
        if drafted:
            hits = merge_hits(question, hits, drafted, embed_fn, card_text_fn, cosine_fn, top_k)
        retrieval = knowledge_score(hits)
        verdict = REASONERS[reasoner](question, hits, retrieval, threshold, extra_prompt)
        last_reason = verdict
        library = combined_score(retrieval, verdict.get("coverage"))
        truth = evaluate_truth(question, hits, use_web=use_web, critic=critic) if check_truth else {"verdict": "skipped"}
        score, truth_ok = apply_truth(library, truth)
        new_cards = cards_from_reasoner(verdict.get("cards") or [], question)
        # Don't re-add claims we already drafted this run.
        have = {c["claim"] for c in drafted}
        fresh = [c for c in new_cards if c["claim"] not in have]
        if check_truth and fresh:
            assess_drafts(fresh, lambda claim: hits, check_truth=True, use_web=use_web, critic=critic)
        drafted.extend(fresh)
        ready = bool(verdict.get("ready") and score >= threshold and truth_ok is not False)
        report.append(
            {
                "round": round_i,
                "retrieval": retrieval,
                "coverage": verdict.get("coverage"),
                "knowledge": score,
                "truth": truth.get("verdict"),
                "truth_rationale": truth.get("rationale"),
                "ready": ready,
                "drafted": [c["id"] for c in fresh],
                "gaps": verdict.get("gaps") or [],
            }
        )
        if ready:
            break
        if not fresh:
            break

    if drafted:
        write_candidates(drafted, out_path)
        if commit:
            commit_candidates(drafted)

    final = report[-1] if report else {"knowledge": 0.0, "ready": False}
    recommendation = last_reason.get("recommendation") if final.get("ready") else None
    return {
        "question": question,
        "threshold": threshold,
        "knowledge": final.get("knowledge", 0.0),
        "ready": bool(final.get("ready")),
        "recommendation": recommendation,
        "gaps": last_reason.get("gaps") or [],
        "hits": hits,
        "drafted": drafted,
        "rounds": report,
        "committed": bool(commit and drafted),
        "candidates_path": out_path if drafted else None,
    }
