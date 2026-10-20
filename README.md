# Dough Research — Insight Card Pipeline

This repository implements a small, modular retrieval-and-verification system for dough-research knowledge. It uses:

- Voyage for embeddings and semantic retrieval
- SQLite for a local vector-style card index
- Claude for drafting, validation, and recommendation synthesis
- JSONL card records as the source-of-truth knowledge library

The core idea is to store research knowledge as atomic, schema-validated "insight cards" and then:

1. create or ingest a card
2. embed it into a retrieval index
3. ask a question or make a claim
4. retrieve related cards
5. verify whether the claim is supported, contradicted, or insufficiently evidenced
6. optionally feed the result to an LLM for a recommendation or action

This is intentionally modular: the storage layer, embedding provider, scrapers, drafters, and reasoners all have explicit extension points.

## Repository at a glance

- `schema.json` — the canonical card schema
- `cards.jsonl` — the curated knowledge library in JSONL form
- `cards.db` — generated local SQLite index created by `embed_pipeline.py`
- `ingest.py` — scraping and drafting logic for new cards
- `embed_pipeline.py` — indexing, retrieval, add/ingest/recommend commands
- `recommend.py` — retrieval scoring and recommendation loop
- `validate.py` — evidence compilation and claim verification
- `synthesis.py` — Claude-based ingest/validation/recommendation logic
- `VOYAGE.md` — Voyage-focused behavior and score expectations

## What the app models

Each "insight card" represents one atomic claim about dough behavior, not a sprawling note. Cards are meant to be small, checkable, and relevant to a particular product surface or phase of the workflow.

Example fields from `schema.json`:

- `id`: stable slug like `windowpane-test-gluten-check`
- `claim`: one factual sentence
- `variables`: what the claim concerns (`hydration`, `temperature`, `fermentation_time`, etc.)
- `style_scope`: applicable dough style (`neapolitan`, `ny_slice`, `general`, etc.)
- `relevance_targets`: where the card is relevant (`fermentation_lab_dashboard`, `pizza_cart_app`, `mise_en_place`)
- `confidence`: confidence score between 0 and 1
- `phase`: workflow phase such as `proofing`, `baking`, `diagnostic`
- `source`: article, book, web page, conversation, or experiment reference
- `corroboration`: validation result from Claude after checking evidence

## Architecture

The project follows a simple pipeline:

1. Source material
   - text pasted in manually
   - a URL scraped from the web
   - a local file
   - a typed claim in the terminal

2. Card drafting
   - `ingest.py` scrapes source content and normalizes it
   - `synthesis.py` can use Claude to generate candidate cards
   - `card_from_claim()` shapes a manually authored card into schema form

3. Storage and indexing
   - `cards.jsonl` stores the human-reviewed library
   - `embed_pipeline.py build` embeds those claims with Voyage and writes them to `cards.db`

4. Retrieval
   - `embed_pipeline.py query` or `recommend` searches the library by semantic similarity
   - structured filters can restrict by style, variable, or target

5. Verification
   - `validate.py` gathers nearby cards and source URLs
   - Claude assesses whether a claim is supported, contradicted, or insufficiently evidenced

6. Recommendation / LLM usage
   - `recommend.py` decides whether the library is strong enough to answer the question
   - if not, it can draft additional cards and continue the loop
   - the final answer is a recommendation or a status such as "needs more evidence"

## File-by-file summary and specification

### `schema.json`

Purpose: canonical card schema for all cards.

What it defines:
- required fields for a valid card
- allowed enums and preferred vocabularies
- optional metadata such as `source`, `phase`, `kind`, `status`, `corroboration`, and `relevance_targets`

Why it matters:
- keeps all cards structurally consistent
- enables schema validation and downstream filtering
- provides the contract used by ingestion, drafting, and retrieval

Key schema rules:
- `id` is required and should be a stable slug
- `claim` is required and should be a single, falsifiable fact
- `variables`, `style_scope`, and `relevance_targets` are arrays of normalized slugs
- `confidence` is numeric between 0 and 1
- `corroboration` is a separate validation object produced after check/verify steps

### `cards.jsonl`

Purpose: the primary card library in a line-delimited JSON format.

What it contains:
- one JSON object per line
- each object matches `schema.json`
- a human-curated or reviewed set of cards

Why it matters:
- source-of-truth for the research library
- readable and easily appendable
- used by `embed_pipeline.py` to build the vector index

Notes:
- this file is not binary and is intended for version control
- a card is typically added by `embed_pipeline.py add` or by appending a valid JSON line manually
- after adding a card, rebuild the embedded index with `python embed_pipeline.py build --provider voyage`

### `cards.db`

Purpose: generated SQLite database containing the embedded cards used for retrieval.

What it is:
- built from the cards in `cards.jsonl`
- stores the serialized embedding blobs for each card
- used for fast approximate retrieval and filtering

Notes:
- generated runtime artifact, not usually hand-edited
- should be rebuilt after changes to the knowledge base

### `ingest.py`

Purpose: ingest external information into the repo, normalize it, and draft candidate cards.

What it includes:
- `load_schema()` and `schema_enums()` to read and normalize schema constraints
- `scrape_url()`, `scrape_file()`, and `scrape_text()` for ingest sources
- `normalize_card()` to convert raw drafts into validated card records
- `draft_template()` and `draft_claude()` to generate candidate cards from raw text
- `commit_candidates()` to append new cards to `cards.jsonl`

Key behaviors:
- scrapers return a document payload with `source_type`, `reference`, and `text`
- only valid cards are retained
- card IDs are generated with uniqueness checks to avoid collisions
- `cards.jsonl` is not modified until you explicitly commit a draft

This module is the place to plug in new sources or draft strategies.

### `embed_pipeline.py`

Purpose: the main runtime interface for building, querying, adding, ingesting, and recommending against the card library.

What it includes:
- embedding provider abstraction for Voyage (extendable to other providers)
- SQLite setup and storage logic
- `build_index()` for embedding the full library
- `query()` for semantic search with optional structured filters
- `resolve_query()` for inferring style/target/variable from text or a card
- CLI actions: `build`, `query`, `ingest`, `add`, `recommend`

Key behaviors:
- Voyage is used for producing embeddings
- retrieval is cosine similarity over the embedded claim text
- filters can be applied by `style`, `variable`, or `relevance_target`
- a library recommendation can include a Claude reasoner and truth validation stage

Important usage contract:
- build and query must use the same embedding provider
- after new cards are committed to `cards.jsonl`, re-run `build` before querying with the new content

### `recommend.py`

Purpose: determine whether the library already knows enough to answer a user question.

What it includes:
- `knowledge_score()` for assessing retrieval confidence
- `classify_coverage()` to label the gap state (`duplicate`, `covered`, `refines`, `gap`)
- `assess_drafts()` to evaluate candidate cards against existing neighbors
- `recommend()` to execute a retrieval → score → optional draft loop
- `REASONERS` with `none` and `claude` options

Behavior:
- initial retrieval is based on embeddings
- a reasoner may decide if the library is sufficient or if more cards should be drafted
- optional Claude synthesize step may propose missing knowledge cards
- this loop caps itself with `max_rounds` and guards against excessive recursion

### `validate.py`

Purpose: the truth-checking layer for claims.

What it includes:
- `is_url()` to guard URL validation
- `_ddg_urls()` to gather candidate search results
- `gather_evidence()` to collect nearby library cards and web pages
- `evaluate_truth()` to ask Claude to judge a claim as support/conflict/insufficient
- `apply_truth()` to blend library coverage with evidence truth into a final score

Behavior:
- if `ANTHROPIC_API_KEY` is absent, truth checks are skipped, not crashed
- if web fetches fail, the system still returns a degraded verdict instead of failing completely
- this module answers: "is this claim actually supported by evidence?"

### `synthesis.py`

Purpose: the generative LLM layer that writes or critiques cards.

What it includes:
- `ClaudeBot.complete_json()` to call Anthropic Messages API and parse JSON responses
- `ClaudeBot.ingest()` to draft candidate insight cards from source text
- `ClaudeBot.validate()` to judge a claim against evidence
- `ClaudeBot.synthesize()` to produce a recommendation and any missing cards
- `ClaudeBot.reason()` to decide the next best action based on retrieval + truth

Behavior:
- all responses are structured as JSON
- this is the only module that speaks to Anthropic directly
- it is intentionally separated from retrieval logic so you can swap out the model or call another provider later

### `VOYAGE.md`

Purpose: details for operating the embedding layer.

What it documents:
- how Voyage is used in this repo
- what `query`, `add`, and `recommend` do
- score behavior and expected similarity semantics
- when to use each API mode

This is a valuable companion file when debugging retrieval issues or tuning thresholds.

### `README.md`

Purpose: project guide and operational reference.

This file should explain:
- what the repo does
- how data flows end-to-end
- how to start from a claim or URL
- how to add cards and rebuild the index
- how to verify a claim and feed it into the LLM/recommendation path

## Moduled use cases

### 1. Create a card

Use when a source fact is known and should become a structured library entry.

Command:

```bash
python embed_pipeline.py add \
  --claim "For Neapolitan dough, poke-test spring-back beats clock time." \
  --style neapolitan \
  --target fermentation_lab_dashboard \
  --variable tactility
```

This does the following:
- creates a schema-shaped card from the claim
- infers or overrides style/target/variable tags
- checks whether the claim is already covered by nearby library cards
- writes the card to `cards.jsonl` and returns a normalized JSON object

After committing the card, rebuild the index:

```bash
python embed_pipeline.py build --provider voyage
```

### 2. Make a claim and verify it

Use when you have a candidate fact and want to know if it is supported or contradicted by the library and/or web evidence.

Example:

```bash
python embed_pipeline.py recommend --provider voyage --reasoner claude \
  --text "is this Neapolitan dough ready to bake" \
  --threshold 0.7 --commit
```

This sequence:
- retrieves nearby cards by semantic similarity
- evaluates the local library coverage
- optionally asks Claude to synthesize missing cards
- verifies the claim using evidence pages and/or web search
- returns a recommendation if the library and evidence are strong enough

### 3. Verify a claim without a full recommendation

If you want a direct verification pass on a claim:

```bash
python synthesis.py validate --claim "Cold fermentation improves extensibility without making the dough slack." --no-web
```

This runs `evaluate_truth()` logic behind the scenes and returns a structured decision like:
- `support`
- `conflict`
- `insufficient`

### 4. Feed a claim to the LLM

The repo separates retrieval from generation.

Typical pattern:

1. Retrieve relevant nearby cards using `embed_pipeline.py query`
2. Build a question or claim
3. Call `recommend` with `--reasoner claude`
4. Let Claude decide whether the library is ready and whether to draft missing cards

Example:

```bash
python embed_pipeline.py recommend --provider voyage --reasoner claude \
  --text "How can I tell if a Neapolitan dough has over-proofed?" \
  --threshold 0.7
```

The output includes:
- retrieval score
- knowledge score
- rounds of coverage assessment
- any drafted cards
- final recommendation if the threshold is met

## Quick start

Install the Python dependencies you need:

```bash
pip install requests numpy
```

Set required environment variables:

```bash
export VOYAGE_API_KEY=...
export ANTHROPIC_API_KEY=...   # needed for drafting/validation/recommendation with Claude
```

Build the initial index:

```bash
python embed_pipeline.py build --provider voyage
```

Query similar cards:

```bash
python embed_pipeline.py query --provider voyage \
  --text "how do I know if Neapolitan dough is over-proofed" \
  --style neapolitan \
  --target fermentation_lab_dashboard
```

Add a card:

```bash
python embed_pipeline.py add --provider voyage \
  --claim "For Neapolitan dough, poke-test spring-back beats clock time."
```

Recommend or validate a claim:

```bash
python embed_pipeline.py recommend --provider voyage --reasoner claude \
  --text "is this Neapolitan dough ready to bake" \
  --threshold 0.7 --commit
```

## Sanity checks and operational guidance

- `cards.jsonl` is the source-of-truth; `cards.db` is derived
- after editing `cards.jsonl`, always rebuild the index
- Voyage handles embeddings and retrieval only; Claude handles drafting and truth evaluation
- claim verification depends on evidence, not just nearest neighbors
- if `ANTHROPIC_API_KEY` is missing, the truth step is skipped rather than crashing
- use `--no-web` or `--no-truth` when you want to simplify or isolate retrieval behavior

## Recommended mental model

The most important distinction is:

- retrieval answers: "Do we already know something similar?"
- verification answers: "Is this claim actually supported?"
- recommendation answers: "Given the library and evidence, should we act or ask for more info?"

This repo keeps those stages separate so it can be extended, debugged, and re-used in a larger app without mixing retrieval logic and LLM decision logic together.

## When to extend the repo

The code is already organized around extension points:

- add new scraper functions in `ingest.py`
- add a new embedding provider in `embed_pipeline.py`
- add a new reasoner in `recommend.py`
- add a new verification strategy in `validate.py`
- add a new generative skill in `synthesis.py`

This keeps the app modular and lets you adapt it to new domains or product workflows beyond pizza dough research.

## Minimal practical workflow

For many day-to-day uses, this is the simplest sequence:

```bash
# 1) write a claim or ingest a source
python embed_pipeline.py add --claim "Dough should feel springy, not wet, before proofing ends."

# 2) build the vector index
python embed_pipeline.py build --provider voyage

# 3) run a recommendation / check
python embed_pipeline.py recommend --provider voyage --reasoner claude \
  --text "when should I stop proofing Neapolitan dough?" \
  --threshold 0.7
```

This keeps the knowledge base small, reviewable, and semantically queryable while still leaving room for validation and LLM-assisted reasoning.

---

If you want, I can also rewrite this into a shorter, more polished GitHub README style with a cleaner landing-page summary and a smaller command reference section, or I can help refine the actual README in-place for your repo.
