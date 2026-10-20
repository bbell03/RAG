# Voyage in this project

This is what Voyage actually does for the dough-research card library — and what it does not do.

Voyage is an **embedding API**. You send it text; it returns a list of numbers (a vector). This repo uses those vectors to find cards in `cards.db` that are *about the same thing* as a question. Voyage does not read `schema.json`, does not write `cards.jsonl`, does not judge whether a claim is true, and does not produce a recommendation. Truth evaluation is a separate Claude critic (`corroboration` on the card) that fetches cited URLs and a short web search. `--no-truth` keeps library-only coverage.

If a step needs a sentence, a yes/no, or a new card, that is **Claude** (`--reasoner claude` / `--drafter claude`). Voyage is only the `--provider` on `build`, `query`, and the retrieval half of `recommend`.

```
you type a question
        │
        ▼
Voyage embeds the question          (input_type=query)
        │
        ▼
cosine similarity against cards.db  (vectors from an earlier `build`)
        │
        ▼
optional style / target / variable filters
        │
        ▼
top-k existing cards, with a relatedness score
```

## What a Voyage query is

```bash
export VOYAGE_API_KEY=...
python embed_pipeline.py build --provider voyage
python embed_pipeline.py query --provider voyage \
  --text "how do I know if Neapolitan dough is over-proofed"
```

1. `build` sends each card’s claim (plus its variables and styles) to Voyage with `input_type=document` and stores the vectors in `cards.db`.
2. `query` sends your `--text` (or a `--card` claim) to Voyage with `input_type=query`.
3. The program ranks stored cards by cosine similarity, then applies any style/target/variable filters.
4. You get back rows that already live in the library. The number on the left is **relatedness**, not scientific certainty.

`--card windowpane-neapolitan-periodic` is the same machinery. It just uses that card’s claim as the question so you can ask “what else do I already have that’s like this?”

## What to expect

**Voyage only knows your library.** With seven or eight cards, a question about salt or oven spring still returns *something*. The top hit may simply be the least-unrelated card. A score near 1.0 on `--card` is normal (you searched with that card’s own sentence). A score around 0.3–0.5 on a new question often means “same neighborhood,” not “this answers it.”

**The score is not a knowledge score.** Card `confidence` (0.55–0.85 on the seed cards) is a human tag you wrote. Voyage never sees it during `query`. `recommend` is the command that mixes similarity with that confidence and then asks a reasoner whether the set is enough to advise.

**Build and query must use the same Voyage index.** If you `add` a claim, it is only in `cards.jsonl` until you `build --provider voyage` again. Querying a stale `cards.db` will not see the new line.

**`input_type` matters.** Documents are indexed as documents; questions are embedded as queries. Voyage prepends a short retrieval prompt in each case. Mixing that up still returns 1024-dimensional floats, but ranking gets worse.

**Empty text fails.** Voyage rejects `[""]`. The CLI now requires `--text` or `--card` for `query`.

**Voyage will not:**

- generate a new insight card
- scrape an article
- validate that a claim is true
- raise a card’s `confidence`
- decide “ready to bake” or “wait”
- fill `style_scope` / `relevance_targets` (those are inferred from the card file or your flags)

## Which command to use

| You want to… | Command | Voyage’s job |
| --- | --- | --- |
| Index claims you already trust | `build --provider voyage` | Embed each card as a document |
| Ask a question against the library | `query --text "..."` | Embed the question; rank cards |
| Find near-duplicates of an existing card | `query --card <id>` | Embed that claim; rank neighbors |
| Narrow to one pizza style or app surface | `query --text "..." --style neapolitan --target fermentation_lab_dashboard` | Same ranking, after hard filters |
| Write a claim you already have | `add --claim "..."` | None until you `build` |
| Draft cards from a URL / paste | `ingest --url ... --drafter claude` | After draft, Voyage scores each new claim against `cards.db` (`gap` / `refines` / `covered` / `duplicate`) |
| Score whether the library is enough, no drafting | `recommend --provider voyage --reasoner none --threshold 0.7` | Retrieval knowledge only |
| Validate / draft until a threshold, then recommend | `recommend --provider voyage --reasoner claude --threshold 0.7` | Retrieval score; Claude writes/validates |

Direction of travel: **write cards → build → query to see neighbors → edit `refines` / `contradicts` → build again.** Use `recommend` when the question is operational (“is this dough ready?”) and you care whether the library is *enough*, not just which card is closest.

## Use cases that fit Voyage queries

**Fermentation lab dashboard.** A sensor path asks “is this Neapolitan bulk ready?” `query --style neapolitan --target fermentation_lab_dashboard` should surface `clock-vs-degree-hours` and `even-bubble-structure-signal` if those cards are indexed. The dashboard still has to apply the claim (poke test, volume, degree-hours). Voyage only picked the relevant rules.

**Pizza cart app.** A cook types “windowpane after kneading.” With `--target pizza_cart_app` you should get the NY checkpoint card, not the Neapolitan periodic-fold card, if filters are on. Without `--target`, Voyage may return both because the claims are close in wording.

**Dedup before `add`.** You are about to record “cold ferment helps low-hydration dough open.” `query --text` that sentence. If `time-substitutes-hydration-handling` or `cold-ferment-openability` is already on top, set `refines` instead of adding a twin.

**Coverage check for a style.** `query --text "gluten development" --style neapolitan` shows whether the library has Neapolitan-specific kneading/hydration cards or only `general` ones. Gaps here are a writing problem, not a Voyage problem.

**Recommend, not query, for a decision.** “Should I bake this dough now?” needs a sentence and a certainty bar. Voyage ranks supporting cards; `--reasoner` says whether that set clears `--threshold`. If it does not, the reasoner may draft candidate cards into `candidates.jsonl`. Those drafts are hypotheses (`confidence` capped at 0.45) until you edit and `build`.

## What “good” looks like

A useful Voyage query has three properties:

1. The question uses the same vocabulary as the cards (hydration, windowpane, Neapolitan, degree-hours), so the vector lands near the right claims.
2. Filters match how you tagged the cards. `--style neapolitan` keeps `general` cards (by design) and drops NY-only cards.
3. You interpret a high score as “this card is on-topic,” then read the claim. You do not treat the score as proof.

A *bad* use is expecting Voyage to invent the missing card, or treating the first row as an answer when the library has no card about that variable.

## Mental model

Think of Voyage as a **catalog clerk**, not a baker and not a reviewer.

- The clerk can find the existing index cards that sound like your question.
- You (or `add` / `ingest` / a reasoner) write new cards.
- `build` files them.
- A dashboard or `recommend` decides what to *do* with the ones that were found.
