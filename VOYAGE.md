# Voyage in this project

Voyage is the **embedding API** only. It turns text into vectors so `query` can find related cards in `cards.db`.

It does **not** write cards, judge truth, scrape pages, or draft recommendations. Those are ingest / validate / recommend / passive-research (chat models + web).

```
question → Voyage embed (query) → cosine vs cards.db → optional style/target/variable filters → top-k cards
```

## Commands that use Voyage

```bash
python embed_pipeline.py build --provider voyage
python embed_pipeline.py query --provider voyage --text "Neapolitan over-proofed"
python embed_pipeline.py query --provider voyage --card windowpane-neapolitan-periodic
python embed_pipeline.py recommend --provider voyage --reasoner local --text "is this dough ready?" --threshold 0.7
```

| Goal | Command | Voyage’s job |
| :--- | :--- | :--- |
| Index trusted cards | `build` | Embed each card as a document |
| Ask the library | `query --text` | Embed the question; rank |
| Near-duplicates of a card | `query --card <id>` | Embed that claim; rank neighbors |
| Filtered search | `query --style … --target …` | Rank, then hard filter |
| Coverage / advise | `recommend` | Retrieval half of the score |

`add` / `ingest` / `passive-research` do not call Voyage until you `build` (except coverage checks during ingest/add and dedup during research).

## How to read scores

- Cosine on `query` is **relatedness**, not truth.
- Card `confidence` is a library tag; Voyage does not use it during `query`.
- `recommend` mixes similarity with confidence, then may call a reasoner/critic.
- Dedup in the claims controller treats ≥ **0.82** as a near-copy.

Build and query must use the same provider. After editing `cards.jsonl`, rebuild or retrieval will be stale.

## Good uses

- Lab dashboard: `query --style neapolitan --target fermentation_lab_dashboard`
- Cart app: `query --target pizza_cart_app --text "windowpane after kneading"`
- Before adding: `query --text "your new sentence"` — if a twin is on top, `refines` instead of duplicating

## Mental model

Voyage is a catalog clerk. You (or ingest / the claims controller) write the cards; `build` files them; `query` finds neighbors; validate / recommend / passive-research decide what is true or what to do next.

See [`README.md`](README.md) for the full pipeline and [`passive-research.md`](passive-research.md) for verification.
