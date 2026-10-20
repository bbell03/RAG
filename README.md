# Dough Research — Insight Card Pipeline

Stack: **Voyage** embeds and ranks cards. **Claude** drafts and validates claims.
Anthropic has no embeddings API; Voyage is the retrieval partner.

- **`schema.json`** — the shape of one "insight card": a single atomic claim,
  tagged by variable (hydration/kneading/etc.), style scope, workflow phase,
  confidence, and which downstream system it's relevant to.
- **`cards.jsonl`** — seed cards pulled from the conversation so far
  (windowpane test, bubble-structure readiness, time-vs-hydration tradeoff,
  etc.), one JSON object per line, matching the schema.
- **`embed_pipeline.py`** — builds a SQLite + vector index from the cards
  and lets you query it with both structured filters and semantic search.
- **`VOYAGE.md`** — what Voyage queries do in this repo (embeddings only),
  what to expect from scores, and when to use `query` vs `add` vs `recommend`.
- **`synthesis.py`** — Claude bot for ingest (draft cards) and validate (truth).

## Quick start

```bash
pip install requests numpy

export VOYAGE_API_KEY=...
export ANTHROPIC_API_KEY=...     # only needed for Claude draft / recommend

python embed_pipeline.py build --provider voyage

python embed_pipeline.py query --provider voyage \
  --text "how do I know if Neapolitan dough is over-proofed" \
  --style neapolitan \
  --target fermentation_lab_dashboard
```

## Adding new cards

```bash
python embed_pipeline.py add --provider voyage \
  --claim "For Neapolitan dough, poke-test spring-back beats clock time."

python embed_pipeline.py ingest --url https://example.com/article --drafter claude
# review candidates.jsonl, then:
python embed_pipeline.py ingest --url https://example.com/article --drafter claude --commit
```

Or append a JSON line to `cards.jsonl` matching `schema.json`. Then re-run
`build` (it upserts by `id`).

## Recommend (coverage + optional drafting)

Voyage only ranks existing cards. Claude is optional:

```bash
python embed_pipeline.py build --provider voyage

# score the library only
python embed_pipeline.py recommend --provider voyage --reasoner none \
  --text "is this Neapolitan dough ready to bake" --threshold 0.7

# validate / draft missing cards with Claude
python embed_pipeline.py recommend --provider voyage --reasoner claude \
  --text "is this Neapolitan dough ready to bake" --threshold 0.7 --commit
```

Drafts land in `candidates.jsonl` unless you pass `--commit`. Re-`build` after committing.

Coverage now has two parts: Voyage says whether the library already has the claim
(`gap` / `refines` / `covered` / `duplicate`). Claude then judges **truth**
(`support` / `conflict` / `insufficient`) from neighbor cards, cited URLs, and a
short web fetch. `conflict` and `insufficient` block a `recommend` decision.
Use `--no-web` or `--no-truth` to skip those steps.

## Wiring into your other systems

`relevance_targets` is the hook: filter by `fermentation_lab_dashboard` to
pull only cards worth surfacing as live thresholds/alerts on the IoT
dashboard, or by `pizza_cart_app` for cards worth showing as dough-status
guidance in the cart app. `mise_en_place` is there for anything that should
inform recipe/substitution logic.

## Where this could go next

- Swap SQLite for a proper vector store (pgvector, chromadb) once the card
  count grows past a few hundred.
- Walk `contradicts` / `refines` so query and recommend surface related cards
  with the top hit, not just the nearest claim.
# RAG
