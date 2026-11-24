# Use cases, feedback, and modulation

What each facility is for, what the operator/app should show afterward, and how to turn that facility on or off.

Code source of truth: [`research_features.py`](research_features.py) (`ResearchFeatures` + `--profile` + override flags).

---

## Profiles (bundles)

| Profile | Intent | On | Off |
| :--- | :--- | :--- | :--- |
| **`verify`** | Check existing claims only | substantiate | expand, seed, propagate, evidence_adds, focused_propagate, recommend drafts |
| **`grow`** | Verify + mint children from support | substantiate, expand, propagate, evidence_adds, focused_propagate | seed |
| **`full`** | Overnight / continuous growth (default) | everything | — |
| **`ask`** | Product Q&A defaults | — | research growth; gap drafts **off** until `--draft-gaps` |

Fine-grained overrides: `--no-expand`, `--no-seed`, `--no-propagate`, `--no-evidence-adds`, `--no-focused-propagate`, `--growth-requires-sourcewise`, and for recommend `--no-draft` / `--draft-gaps`.

---

## Use cases

### 1. Search the library

| | |
| :--- | :--- |
| **When** | User asks “what do we already know about X?” |
| **Command** | `python embed_pipeline.py query --provider voyage --text "…"` |
| **Toggle** | Always on (retrieval) |
| **Feedback** | Ranked card hits + scores. **No writes.** |
| **App hook** | Results list / “related insights” panel |

### 2. Ask / recommend (product question)

| | |
| :--- | :--- |
| **When** | Diagnostic UI: “is this Neapolitan ready to bake?” |
| **Command** | `python embed_pipeline.py recommend --provider voyage --reasoner local --text "…" --profile ask` |
| **With gap drafts** | add `--draft-gaps` (or use default `full` profile without `--no-draft`) |
| **Toggle** | `recommend_gap_draft` ← `--no-draft` / `--draft-gaps` / profile `ask` |
| **Feedback** | `ready` + advice text, or `gaps[]`. If drafting on: new rows in `candidates.jsonl` |
| **App hook** | Answer card; optional “Save proposed claims for research” when gaps exist |

### 3. Verify internal (library only)

| | |
| :--- | :--- |
| **When** | “Does our library already support or contradict this sentence?” |
| **Command** | `python synthesis.py validate --claim "…" --bot local --evidence internal` |
| **Batch** | `python passive-research.py --profile verify --evidence internal --batch-size 5` |
| **Toggle** | substantiate + `--evidence internal` (disables growth via profile) |
| **Feedback** | JSON/report: `verdict`, `sources[]` (library neighbors), no page fetches |
| **App hook** | “Library consistency” badge; do not treat as independent proof |

### 4. Verify external (web + library)

| | |
| :--- | :--- |
| **When** | “Do independent sources support this?” |
| **Command** | `python synthesis.py validate --claim "…" --bot local --evidence external` |
| **Batch** | `python passive-research.py --profile verify --evidence external` |
| **Toggle** | substantiate + `--evidence external` |
| **Feedback** | `verdict`, full `sources[]` (fetch status + cited), `sourcewise_supported`; controller also writes `research-reports/` |
| **App hook** | Source list UI; promote only when support + URL citations |

### 5. Promote / fix / demote (after verify)

| | |
| :--- | :--- |
| **When** | Critic returned support or conflict on an existing card |
| **Command** | Default research / `modify --id …` under `--profile verify` |
| **Toggle** | substantiate only (verify profile turns off adds/propagate) |
| **Feedback** | Ops: validate / modify / remove (or demote). Card `confidence`, `status`, `corroboration`, tags |
| **App hook** | Claim inbox: Accept / Edit sentence / Archive |

### 6. Evidence adds (new sibling mechanisms)

| | |
| :--- | :--- |
| **When** | Support verdict; evidence clearly states another atomic mechanism |
| **Not when** | insufficient / conflict / mid-escalation |
| **Command** | `python passive-research.py --profile grow` (or `full`) |
| **Toggle** | `evidence_adds` ← `--no-evidence-adds`; optional `--growth-requires-sourcewise` |
| **Feedback** | `add` ops → `candidates.jsonl` (or `cards.jsonl` with `--commit`); drafts ≤ 0.45 |
| **App hook** | “Proposed related claims” after a green verify |

### 7. Propagate / expand phase (workflow fan-out)

| | |
| :--- | :--- |
| **When** | Parent is supported + reviewed; fewer than 3 children |
| **Command** | `python passive-research.py propagate --id …` **or** `--until-done --profile grow` |
| **Toggle** | `propagate` + `expand` ← `--no-propagate` / `--no-expand`; `focused_propagate` for the second planner pass |
| **Feedback** | `propagate` ops with `refines` (specialize) or `expands`; report lists actions |
| **App hook** | Button on supported card: “Specialize for pizza_cart_app / lab / mise_en_place” |

### 8. Seed coverage gaps

| | |
| :--- | :--- |
| **When** | Substantiate + expand queues empty; preferred style/variable missing |
| **Command** | `python3 -u passive-research.py --until-done --profile full --commit` |
| **Toggle** | `seed` ← `--no-seed` or use profile `grow` / `verify` |
| **Feedback** | Seed-phase adds; session report notes missing styles/variables |
| **App hook** | Admin/batch only — keep out of the hot product path |

### 9. Manual add

| | |
| :--- | :--- |
| **When** | Operator types a hypothesis |
| **Command** | `python passive-research.py add --claim "…" --style … --variable …` |
| **Toggle** | CLI command (not a session flag) |
| **Feedback** | Draft card staged; then run verify |
| **App hook** | “New claim” form → candidates |

### 10. Ingest from source

| | |
| :--- | :--- |
| **When** | Bulk draft from URL / file / paste |
| **Command** | `python embed_pipeline.py ingest --url "…" --drafter local` |
| **Toggle** | CLI command |
| **Feedback** | Many candidates for inbox review |
| **App hook** | “Import article” → review queue |

---

## When new claims make sense (research timeline)

```text
substantiate:  verify/fix existing ──support──► optional evidence_adds + propagate
               └──insufficient/conflict──► no new claims

expand phase:  supported parent → specialize/expand children (if expand+propagate on)

seed phase:    queues empty → coverage drafts (if seed on)
```

**Rule:** verify first; grow only from what evidence upheld. Prefer `sourcewise_supported` before trusting growth in an app (`--growth-requires-sourcewise`).

---

## Feedback surfaces (every research run)

| Surface | What you get |
| :--- | :--- |
| stdout / tee log | Per-card verdict, found/not_found, source counts, applied ops |
| `research-reports/YYYY-MM-DDTHHMMSS.md` | Timestamped basis report |
| `research-reports/latest.md` | Same content, always newest |
| `candidates.jsonl` | Staged drafts when not `--commit` |
| `cards.jsonl` | Committed updates + drafts |
| card `tags` | `sourcewise_supported` when support is URL-anchored |
| card `corroboration.sources` | Full source inventory for that loop |

---

## Copy-paste recipes

```bash
# Product ask — answer only, no candidates
python embed_pipeline.py recommend --provider voyage --reasoner local \
  --text "is this Neapolitan dough ready" --profile ask

# Product ask — allow gap drafts into candidates
python embed_pipeline.py recommend --provider voyage --reasoner local \
  --text "is this Neapolitan dough ready" --profile ask --draft-gaps

# Verify-only batch (no new claims)
python passive-research.py --profile verify --evidence external --batch-size 5

# Verify then grow children; no seeding
python3 -u passive-research.py --until-done --profile grow --commit --max-actions 40

# Full overnight loop
python3 -u passive-research.py --until-done --profile full --commit --max-actions 40 --delay 8

# Strict growth: only fan out when URL-cited support
python passive-research.py --profile grow --growth-requires-sourcewise --until-done
```

---

## Related docs

- [`README.md`](README.md) — setup + short cheat sheet  
- [`passive-research.md`](passive-research.md) — controller phases/ops/flags  
- [`CLAIM_VERIFICATION.md`](CLAIM_VERIFICATION.md) — internal vs external search loop  
- [`VOYAGE.md`](VOYAGE.md) — embedding/retrieval only  
