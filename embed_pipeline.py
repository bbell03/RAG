"""
Dough Research Insight Card — Embedding Pipeline
=================================================

Turns cards.jsonl (schema.json) into a queryable vector index.

Usage:
    export VOYAGE_API_KEY=...
    export GEMINI_API_KEY=...      # free Gemini drafts / validates; Voyage only embeds
    python embed_pipeline.py build --provider voyage
    python embed_pipeline.py query --provider voyage --card windowpane-neapolitan-periodic
    python embed_pipeline.py query --text "how long should Neapolitan dough ferment"
    python embed_pipeline.py add --claim "For Neapolitan dough, poke-test spring-back beats clock time."
    python embed_pipeline.py recommend --provider voyage \\
        --text "is this Neapolitan dough ready to bake" --threshold 0.7 --commit
    python embed_pipeline.py recommend --provider voyage --reasoner none \\
        --text "is this Neapolitan dough ready to bake" --threshold 0.7

Swap in your own embedding provider by adding a function to PROVIDERS below —
everything else (storage, filtering, ranking) is provider-agnostic.

Safety (runtime / memory):
    query() loads every row in cards.db into RAM and scores them — fine for
    tens/hundreds of cards, not for tens of thousands. Voyage batches at build
    time; a single query embeds one string. Always close the SQLite connection.
    Build and query must use the same provider or cosine is meaningless.
"""

import argparse
import json
import os
import sqlite3
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from ingest import (
    CANDIDATES_PATH,
    DRAFTERS,
    SCRAPERS,
    STYLE_ALIASES,
    TARGET_ALIASES,
    card_from_claim,
    commit_candidates,
    ingest,
    is_jsonl_note,
    write_candidates,
    _mentions,
)
from recommend import REASONERS, assess_drafts, recommend as run_recommend

CARDS_PATH = "cards.jsonl"
DB_PATH = "cards.db"


# ---------------------------------------------------------------------------
# Embedding providers — pluggable. Add your own and register it in PROVIDERS.
# ---------------------------------------------------------------------------

def _require_nonempty_texts(texts: List[str]) -> List[str]:
    """Reject empty batches — Voyage 400s on [""]."""
    if not texts or any(not (t or "").strip() for t in texts):
        raise ValueError("Embedding input must be a non-empty list of non-empty strings.")
    return texts


def _post_embeddings(url: str, api_key: str, payload: dict) -> List[List[float]]:
    """POST embeddings. Error bodies are truncated so a 400 cannot blow memory."""
    import requests

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code} from {url}: {resp.text[:800]}")
    data = resp.json()["data"]
    data = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


def embed_voyage(
    texts: List[str],
    model: str = "voyage-4",
    input_type: Optional[str] = None,
) -> List[List[float]]:
    """Embed texts with Voyage.

    Contract:
        texts: non-empty strings (Voyage rejects blanks).
        input_type: "document" at index time, "query" at search time.
        returns: one float vector per input, order preserved via `index`.
        raises: RuntimeError if VOYAGE_API_KEY is missing or the API errors.
    Note:
        Response size is O(n * dims). dims default 1024. Do not embed huge
        batches in query(); build() is the batched path.
    """
    key = os.environ.get("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError("embed_voyage needs VOYAGE_API_KEY")
    payload = {"input": _require_nonempty_texts(texts), "model": model}
    if input_type:
        payload["input_type"] = input_type
    return _post_embeddings("https://api.voyageai.com/v1/embeddings", key, payload)


PROVIDERS = {"voyage": embed_voyage}


# ---------------------------------------------------------------------------
# Storage — plain SQLite + numpy cosine similarity. No external vector DB
# dependency; swap for chromadb/pgvector/etc. later if the card count grows.
# ---------------------------------------------------------------------------

def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Open SQLite and ensure the cards table exists. Caller must close()."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            claim TEXT,
            variables TEXT,
            style_scope TEXT,
            relevance_targets TEXT,
            confidence REAL,
            source TEXT,
            embedding BLOB
        )
        """
    )
    return conn


def card_embedding_text(card: dict) -> str:
    """Claim plus variable/style tags — this is what Voyage sees at build time."""
    parts = [card["claim"]]
    if card.get("variables"):
        parts.append("Variables: " + ", ".join(card["variables"]))
    if card.get("style_scope"):
        parts.append("Styles: " + ", ".join(card["style_scope"]))
    return " | ".join(parts)


def load_cards(path: str = CARDS_PATH) -> List[dict]:
    """Load cards.jsonl. Skips blank lines and # notes. Raises on a bad card line."""
    cards = []
    with open(path) as f:
        for line in f:
            if is_jsonl_note(line):
                continue
            cards.append(json.loads(line))
    return cards


def _unique(values: Iterable[str]) -> List[str]:
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def card_vocab(cards: List[dict]) -> Dict[str, List[str]]:
    """Unique ids / styles / targets / variables across the loaded library."""
    return {
        "ids": [c["id"] for c in cards],
        "styles": _unique(s for c in cards for s in c.get("style_scope", [])),
        "targets": _unique(t for c in cards for t in c.get("relevance_targets", [])),
        "variables": _unique(v for c in cards for v in c.get("variables", [])),
    }


def _infer_label(text: str, labels: Iterable[str], aliases: Dict[str, List[str]]) -> Optional[str]:
    """One label if uniquely implied; None if ambiguous or none. Prefer non-general."""
    hits = []
    for label in labels:
        names = aliases.get(label, [label])
        if any(_mentions(text, name) for name in names) and label not in hits:
            hits.append(label)
    specific = [h for h in hits if h not in ("general", "general_reference")]
    if len(specific) == 1:
        return specific[0]
    if len(specific) == 0 and len(hits) == 1:
        return hits[0]
    return None


def _style_from_card(card: dict) -> Optional[str]:
    scopes = card.get("style_scope") or []
    specific = [s for s in scopes if s != "general"]
    return specific[0] if len(specific) == 1 else None


def _target_from_card(card: dict) -> Optional[str]:
    targets = card.get("relevance_targets") or []
    return targets[0] if len(targets) == 1 else None


def resolve_query(
    text: Optional[str] = None,
    style: Optional[str] = None,
    target: Optional[str] = None,
    variable: Optional[str] = None,
    card_id: Optional[str] = None,
    cards_path: str = CARDS_PATH,
) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[dict]]:
    """Resolve query text and filters from CLI and/or a card.

    Returns: (text, style, target, variable, source_card_or_None)
    Raises: ValueError if neither --text nor --card yields text, or card id unknown.
    Note: CLI overrides inference. 'all' clears a filter.
    """
    cards = load_cards(cards_path)
    by_id = {c["id"]: c for c in cards}
    vocab = card_vocab(cards)
    card = None

    if card_id:
        if card_id not in by_id:
            known = ", ".join(vocab["ids"]) or "(none)"
            raise ValueError(f"unknown card id {card_id!r}. cards.jsonl has: {known}")
        card = by_id[card_id]

    resolved_text = (text or "").strip() or (card["claim"] if card else "")
    if not resolved_text:
        known = ", ".join(vocab["ids"]) or "(none)"
        raise ValueError(f"query needs --text or --card. cards.jsonl has: {known}")

    def _override(value: Optional[str], inferred: Optional[str]) -> Optional[str]:
        if value is None:
            return inferred
        if value in ("", "all", "*"):
            return None
        return value

    inferred_style = _style_from_card(card) if card else _infer_label(resolved_text, vocab["styles"], STYLE_ALIASES)
    inferred_target = _target_from_card(card) if card else _infer_label(resolved_text, vocab["targets"], TARGET_ALIASES)
    inferred_variable = None
    if variable is None and not card:
        inferred_variable = _infer_label(resolved_text, vocab["variables"], {})

    return (
        resolved_text,
        _override(style, inferred_style),
        _override(target, inferred_target),
        _override(variable, inferred_variable),
        card,
    )


def build_index(provider: str = "voyage", cards_path: str = CARDS_PATH, db_path: str = DB_PATH) -> None:
    """Embed all cards as documents and upsert into SQLite.

    Note: one Voyage batch for the whole file. Peak RAM is cards + embeddings
    (n * 1024 * 4 bytes). Connection is always closed.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}. providers: {sorted(PROVIDERS)}")
    cards = load_cards(cards_path)
    if not cards:
        raise ValueError(f"no cards in {cards_path}")
    embed_fn = PROVIDERS[provider]
    texts = [card_embedding_text(c) for c in cards]
    embeddings = embed_fn(texts, input_type="document")
    if len(embeddings) != len(cards):
        raise RuntimeError("embedder returned a different number of vectors than cards")

    conn = init_db(db_path)
    try:
        for card, emb in zip(cards, embeddings):
            vec = np.asarray(emb, dtype=np.float32).tobytes()
            conn.execute(
                "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?)",
                (
                    card["id"],
                    card["claim"],
                    json.dumps(card.get("variables", [])),
                    json.dumps(card.get("style_scope", [])),
                    json.dumps(card.get("relevance_targets", [])),
                    card.get("confidence", 0.5),
                    json.dumps(card.get("source", {})),
                    vec,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    print(f"Indexed {len(cards)} cards -> {db_path}")


# ---------------------------------------------------------------------------
# Retrieval — hard-filter on structured fields, then rank by cosine sim.
# ---------------------------------------------------------------------------

def cosine_sim(a, b) -> float:
    """Cosine similarity. asarray avoids an extra copy when `b` is already float32."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-8
    return float(np.dot(a, b) / denom)


def query(
    text: str,
    provider: str = "voyage",
    db_path: str = DB_PATH,
    style: Optional[str] = None,
    variable: Optional[str] = None,
    relevance_target: Optional[str] = None,
    top_k: int = 5,
) -> List[Tuple[float, dict]]:
    """Rank cards by cosine similarity after structured filters.

    Contract:
        text: non-empty query string.
        returns: up to top_k (score, card) pairs, score in roughly [-1, 1].
        raises: ValueError on empty text; RuntimeError if Voyage fails.
    Note:
        Scores the full table (O(n) RAM). Connection closed in finally.
        frombuffer is a view into the BLOB — do not mutate it.
    """
    if not (text or "").strip():
        raise ValueError("query requires non-empty --text")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}. providers: {sorted(PROVIDERS)}")
    embed_fn = PROVIDERS[provider]
    q_emb = embed_fn([text], input_type="query")[0]

    conn = init_db(db_path)
    try:
        rows = conn.execute("SELECT * FROM cards").fetchall()
    finally:
        conn.close()

    results = []
    for cid, claim, variables, style_scope, relevance_targets, confidence, source, emb_blob in rows:
        variables = json.loads(variables)
        style_scope = json.loads(style_scope)
        relevance_targets = json.loads(relevance_targets)

        if style and style not in style_scope and "general" not in style_scope:
            continue
        if variable and variable not in variables:
            continue
        if relevance_target and relevance_target not in relevance_targets:
            continue

        emb = np.frombuffer(emb_blob, dtype=np.float32)
        score = cosine_sim(q_emb, emb)
        results.append(
            (
                score,
                {
                    "id": cid,
                    "claim": claim,
                    "variables": variables,
                    "style_scope": style_scope,
                    "relevance_targets": relevance_targets,
                    "confidence": confidence,
                    "source": json.loads(source) if source else {},
                },
            )
        )

    results.sort(key=lambda x: x[0], reverse=True)
    return results[:top_k]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_draft_coverage(cards, args) -> None:
    """After drafting, ask Voyage whether the library already covers each claim."""
    if not cards:
        return

    def retrieve(claim: str):
        return query(claim, provider=args.provider, top_k=args.top_k)

    try:
        critic = args.drafter if args.drafter in ("claude", "gemini") else None
        reports = assess_drafts(
            cards,
            retrieve,
            check_truth=not args.no_truth,
            use_web=not args.no_web,
            critic=critic,
        )
    except (RuntimeError, KeyError, ValueError) as exc:
        print(f"coverage skipped: {exc}")
        return

    print("coverage (Voyage library + truth critic):")
    gaps = 0
    for row in reports:
        truth = row.get("truth") or {}
        print(
            f"  {row['status']:9} retrieval={row['retrieval']:.3f}  "
            f"nearest={row['best_sim']:.3f}  "
            f"truth={truth.get('verdict', 'n/a')}  [{row.get('id')}]"
        )
        if row["best_id"]:
            print(f"            nearest card [{row['best_id']}]  {row['best_claim']}")
        if truth.get("rationale"):
            print(f"            truth: {truth['rationale']}")
        if truth.get("citations"):
            cites = ", ".join(
                c.get("url") or c.get("title") or "?" for c in truth["citations"][:3]
            )
            print(f"            citations: {cites}")
        if row["status"] == "gap":
            gaps += 1
    print(
        f"  {gaps}/{len(reports)} draft(s) look like new library coverage; "
        "truth=conflict/insufficient means do not treat the claim as established"
    )


def _run_add(args, parser) -> None:
    """Terminal add: normalize one claim, score coverage, append cards.jsonl."""
    claim = (args.claim or args.text or "").strip()
    if not claim:
        parser.error("add requires --claim")
    try:
        card = card_from_claim(
            claim,
            style=args.style,
            target=args.target,
            variable=args.variable,
            phase=args.phase,
            card_id=args.id,
            confidence=args.confidence,
            reference=args.reference,
            refines=args.refines,
        )
    except ValueError as exc:
        parser.error(str(exc))

    _print_draft_coverage([card], args)
    if (card.get("corroboration") or {}).get("verdict") == "conflict":
        print("truth=conflict: card will still be stored; review before trusting it in recommend")
    written = commit_candidates([card])
    if not written:
        parser.error(f"card id {card['id']!r} already exists in {CARDS_PATH}")
    print(f"added [{card['id']}] -> {CARDS_PATH}")
    print(json.dumps(card, ensure_ascii=False, indent=2))
    print("index it later with: python embed_pipeline.py build --provider voyage")


def _run_ingest(args, parser) -> None:
    """Scrape one source, draft candidates, optionally commit."""
    sources = [(name, getattr(args, name)) for name in SCRAPERS if getattr(args, name)]
    if len(sources) != 1:
        parser.error("ingest needs exactly one of --url, --file, or --text")
    source, spec = sources[0]
    try:
        cards = ingest(source, spec, drafter=args.drafter)
    except (ValueError, FileNotFoundError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))

    print(f"drafted {len(cards)} candidate(s)  (drafter={args.drafter})")
    for card in cards:
        print(f"  [{card['id']}]  {card['claim']}")
    _print_draft_coverage(cards, args)
    write_candidates(cards, args.out)
    print(f"wrote {args.out}")

    if args.commit:
        written = commit_candidates(cards)
        print(f"committed {len(written)} new card(s) -> {CARDS_PATH}")
        print("review the claims, then: python embed_pipeline.py build --provider voyage")
    else:
        print("edit candidates, then re-run with --commit, or append the lines into cards.jsonl")


def _run_recommend(args, parser) -> None:
    """Resolve the question, run the recommend loop, print rounds and verdict."""
    try:
        text, style, target, variable, source_card = resolve_query(
            text=args.text or args.claim,
            style=args.style,
            target=args.target,
            variable=args.variable,
            card_id=args.card,
        )
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))

    def retrieve():
        return query(
            text,
            provider=args.provider,
            style=style,
            variable=variable,
            relevance_target=target,
            top_k=args.top_k,
        )

    try:
        result = run_recommend(
            text,
            retrieve_fn=retrieve,
            embed_fn=PROVIDERS[args.provider],
            card_text_fn=card_embedding_text,
            cosine_fn=cosine_sim,
            reasoner=args.reasoner,
            threshold=args.threshold,
            max_rounds=args.max_rounds,
            extra_prompt=args.prompt,
            top_k=args.top_k,
            commit=args.commit,
            out_path=args.out,
            check_truth=not args.no_truth,
            use_web=not args.no_web,
        )
    except (ValueError, RuntimeError, KeyError) as exc:
        parser.error(str(exc))

    origin = f"card {source_card['id']}" if source_card else "cards.jsonl / text"
    print(f"question: {text}")
    print(f"filters: style={style or 'all'}  target={target or 'all'}  variable={variable or 'all'}  ({origin})")
    print(f"embedder={args.provider}  reasoner={args.reasoner}  threshold={args.threshold}")
    for step in result["rounds"]:
        coverage = "n/a" if step["coverage"] is None else f"{step['coverage']:.3f}"
        print(
            f"round {step['round']}: knowledge={step['knowledge']:.3f}  "
            f"retrieval={step['retrieval']:.3f}  coverage={coverage}  "
            f"truth={step.get('truth') or 'n/a'}  ready={step['ready']}"
        )
        if step.get("truth_rationale"):
            print(f"  truth: {step['truth_rationale']}")
        for gap in step["gaps"]:
            print(f"  gap: {gap}")
        for cid in step["drafted"]:
            print(f"  drafted [{cid}]")
    for card in result["drafted"]:
        print(f"  [{card['id']}]  {card['claim']}")
    if result["drafted"]:
        dest = CARDS_PATH if result["committed"] else result["candidates_path"]
        print(f"wrote {len(result['drafted'])} card(s) -> {dest}")
        if result["committed"]:
            print("index new cards with: python embed_pipeline.py build --provider voyage")
    if result["ready"] and result["recommendation"]:
        print(f"RECOMMENDATION ({result['knowledge']:.3f} >= {args.threshold}):")
        print(result["recommendation"])
    else:
        print(f"NO RECOMMENDATION ({result['knowledge']:.3f} < {args.threshold} or not ready)")
        for gap in result["gaps"]:
            print(f"  need: {gap}")
    for score, card in result["hits"]:
        print(f"{score:.3f}  [{card['id']}]  {card['claim']}")


def main():
    """CLI dispatcher: build | query | ingest | add | recommend."""
    p = argparse.ArgumentParser(description="Build or query the dough-research card index.")
    p.add_argument("action", choices=["build", "query", "ingest", "add", "recommend"])
    p.add_argument("--claim", default=None, help="New insight claim to process and append to cards.jsonl")
    p.add_argument("--id", default=None, help="Optional card id slug for add")
    p.add_argument("--phase", default=None, help="Override workflow phase for add")
    p.add_argument("--confidence", type=float, default=0.5, help="Confidence for add (0-1)")
    p.add_argument("--reference", default="terminal", help="Source reference stored on an added card")
    p.add_argument("--refines", default=None, help="Comma-separated card ids this claim refines")
    p.add_argument("--card", default=None, help="Use this card's claim/style/target from cards.jsonl")
    p.add_argument("--text", default=None, help="Query text, pasted ingest text, or claim if --claim is omitted")
    p.add_argument("--url", default=None, help="Article URL to scrape (ingest)")
    p.add_argument("--file", default=None, help="Local html/txt file to ingest")
    p.add_argument("--drafter", default="gemini", choices=list(DRAFTERS.keys()), help="Default gemini. template writes a shell. claude needs ANTHROPIC_API_KEY.")
    p.add_argument("--out", default=CANDIDATES_PATH, help="Where to write drafted cards (default candidates.jsonl)")
    p.add_argument("--neighbors", action="store_true", help="Deprecated: ingest/add always run a Voyage coverage check")
    p.add_argument("--commit", action="store_true", help="Append drafted cards to cards.jsonl (still run build after)")
    p.add_argument("--provider", default="voyage", choices=list(PROVIDERS.keys()))
    p.add_argument("--style", default=None, help="Override style filter. Default comes from --card or --text. Use 'all' to disable.")
    p.add_argument("--variable", default=None, help="Override variable filter. Default is inferred from --text.")
    p.add_argument("--target", default=None, help="Override relevance_target filter. Default comes from --card or --text. Use 'all' to disable.")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--reasoner", default="gemini", choices=list(REASONERS.keys()), help="Default gemini. none = Voyage score only. claude needs ANTHROPIC_API_KEY.")
    p.add_argument("--threshold", type=float, default=0.7, help="Knowledge score required before a recommendation is issued")
    p.add_argument("--max-rounds", type=int, default=2, help="Retrieve → validate/draft loops before giving up")
    p.add_argument("--prompt", default=None, help="Extra instructions for the reasoner prompt")
    p.add_argument("--no-truth", action="store_true", help="Skip the chat-model truth evaluation (library coverage only)")
    p.add_argument("--no-web", action="store_true", help="Do not fetch cited URLs or search the web during truth checks")
    args = p.parse_args()

    if args.action == "build":
        build_index(provider=args.provider)
        return
    if args.action == "ingest":
        _run_ingest(args, p)
        return
    if args.action == "add":
        _run_add(args, p)
        return
    if args.action == "recommend":
        _run_recommend(args, p)
        return

    try:
        text, style, target, variable, source_card = resolve_query(
            text=args.text,
            style=args.style,
            target=args.target,
            variable=args.variable,
            card_id=args.card,
        )
    except (ValueError, FileNotFoundError) as exc:
        p.error(str(exc))

    origin = f"card {source_card['id']}" if source_card else "cards.jsonl / text"
    print(f"query: {text}")
    print(f"filters: style={style or 'all'}  target={target or 'all'}  variable={variable or 'all'}  ({origin})")
    results = query(
        text,
        provider=args.provider,
        style=style,
        variable=variable,
        relevance_target=target,
        top_k=args.top_k,
    )
    if not results:
        print("no matching cards")
        return
    for score, card in results:
        styles = ",".join(card["style_scope"]) or "-"
        targets = ",".join(card["relevance_targets"]) or "-"
        print(f"{score:.3f}  [{card['id']}]  {card['claim']}")
        print(f"       styles={styles}  targets={targets}")


if __name__ == "__main__":
    main()
