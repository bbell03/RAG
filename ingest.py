"""
Pluggable ingest — scrape a source, draft candidate cards, leave cards.jsonl alone
until you review.

Add a scraper by writing `scrape_*(spec: str) -> dict` with keys
source_type / reference / text, then register it on SCRAPERS.

Add a drafter by writing `draft_*(doc, schema) -> list[dict]` and registering
it on DRAFTERS. Voyage is not a drafter; it only embeds already-written claims.

Safety:
    scrape_url/file cap at MAX_FETCH_BYTES (2 MiB). Claude ingest truncates
    source text to MAX_DRAFT_CHARS. HTML parser is O(page size).
"""

import json
import os
import re
from datetime import date
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urlparse

SCHEMA_PATH = "schema.json"
CARDS_PATH = "cards.jsonl"
CANDIDATES_PATH = "candidates.jsonl"
MAX_FETCH_BYTES = 2_000_000
MAX_DRAFT_CHARS = 12_000


def load_schema(path: str = SCHEMA_PATH) -> dict:
    """Load schema.json. Raises FileNotFoundError / JSONDecodeError."""
    with open(path) as f:
        return json.load(f)


def _vocab(prop: dict) -> List[str]:
    """Preferred labels from x-preferred, else a closed enum."""
    if prop.get("x-preferred"):
        return list(prop["x-preferred"])
    items = prop.get("items") or {}
    if "enum" in items:
        return list(items["enum"])
    return list(prop.get("enum") or [])


def schema_enums(schema: Optional[dict] = None) -> Dict[str, List[str]]:
    """Preferred vocab from schema.json. Unknown slugs are still kept by normalize_card."""
    schema = schema or load_schema()
    props = schema["properties"]
    return {
        "variables": _vocab(props["variables"]),
        "style_scope": _vocab(props["style_scope"]),
        "phase": _vocab(props["phase"]),
        "kind": _vocab(props["kind"]),
        "status": _vocab(props["status"]),
        "relevance_targets": _vocab(props["relevance_targets"]),
        "source_type": _vocab(props["source"]["properties"]["type"]),
    }


def is_jsonl_note(line: str) -> bool:
    """Blank lines and # comments are notes, not cards."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def load_card_ids(path: str = CARDS_PATH) -> List[str]:
    """Existing card ids, for unique slugs. Empty list if the file is missing.

    Skips notes, blanks, and malformed lines so a bad row cannot block add/ingest.
    """
    if not os.path.exists(path):
        return []
    ids = []
    with open(path) as f:
        for line in f:
            if is_jsonl_note(line):
                continue
            try:
                ids.append(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return ids


# ---------------------------------------------------------------------------
# Scrapers — register your own on SCRAPERS.
# ---------------------------------------------------------------------------

class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.chunks: List[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "h1", "h2", "h3", "h4", "li", "tr", "div"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = re.sub(r"\s+", " ", data).strip()
        if not text or self._skip:
            return
        if self._in_title:
            self.title += (" " if self.title else "") + text
        else:
            self.chunks.append(text + " ")


def html_to_text(html: str) -> str:
    """Best-effort visible text. Drops script/style. Output is O(len(html))."""
    parser = _HTMLText()
    parser.feed(html)
    body = re.sub(r"\n{3,}", "\n\n", "".join(parser.chunks)).strip()
    if parser.title:
        return f"{parser.title}\n\n{body}"
    return body


def scrape_url(spec: str) -> dict:
    """GET http(s) and return {source_type, reference, text}.

    Contract: scheme must be http/https. Raises ValueError if oversized or bad URL.
    Note: reads incrementally; stops at MAX_FETCH_BYTES so a huge page cannot fill RAM.
    """
    import requests

    parsed = urlparse(spec)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"url scraper expected http(s), got {spec!r}")
    resp = requests.get(
        spec,
        timeout=30,
        headers={"User-Agent": "dough-research-ingest/0.1"},
        stream=True,
    )
    try:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > MAX_FETCH_BYTES:
                raise ValueError(f"refusing to ingest more than {MAX_FETCH_BYTES} bytes from {spec}")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        text = bytes(buf).decode(resp.encoding or "utf-8", errors="replace")
    finally:
        resp.close()
    if "html" in ctype or text.lstrip().startswith("<"):
        text = html_to_text(text)
    return {"source_type": "article", "reference": spec, "text": text.strip()}


def scrape_file(spec: str) -> dict:
    """Read a local file, same return shape as scrape_url. Caps at MAX_FETCH_BYTES."""
    with open(spec, encoding="utf-8", errors="replace") as f:
        raw = f.read(MAX_FETCH_BYTES + 1)
    if len(raw) > MAX_FETCH_BYTES:
        raise ValueError(f"refusing to ingest more than {MAX_FETCH_BYTES} bytes from {spec}")
    text = html_to_text(raw) if spec.endswith((".html", ".htm")) or raw.lstrip().startswith("<") else raw
    return {"source_type": "article", "reference": spec, "text": text.strip()}


def scrape_text(spec: str) -> dict:
    """Wrap pasted text as a conversation source. Raises ValueError if blank."""
    if not (spec or "").strip():
        raise ValueError("text scraper needs non-empty text")
    return {"source_type": "conversation", "reference": "pasted text", "text": spec.strip()}


SCRAPERS = {"url": scrape_url, "file": scrape_file, "text": scrape_text}


# ---------------------------------------------------------------------------
# Drafters — turn a source document into candidate cards. Not Voyage.
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """First 8 hyphenated tokens of a claim. Fallback 'draft-card' if empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    parts = [p for p in slug.split("-") if p]
    return "-".join(parts[:8]) or "draft-card"


def _unique_id(claim: str, taken: List[str]) -> str:
    """Mint a slug id and append it to `taken` so later cards cannot collide."""
    base = slugify(claim)
    candidate = base
    n = 2
    while candidate in taken:
        candidate = f"{base}-{n}"
        n += 1
    taken.append(candidate)
    return candidate


_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


def _slug(value) -> Optional[str]:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return text if text and _SLUG.match(text) else None


def _labels(values, fallback: Optional[str] = None) -> List[str]:
    """Keep preferred and unknown slug labels. Drop junk. Dedup, order preserved."""
    if isinstance(values, str):
        values = [values]
    out = []
    for raw in values or []:
        label = _slug(raw)
        if label and label not in out:
            out.append(label)
    if out:
        return out
    return [fallback] if fallback else []


def normalize_card(raw: dict, doc: dict, enums: Dict[str, List[str]], taken: List[str]) -> dict:
    """Shape a draft onto the schema. Mutates `taken` with the new id.

    Contract: claim required. Confidence clamped to [0, 1].
    Unknown slug labels are kept (humidity is not rewritten to hydration).
    Non-slug junk is dropped; empty lists fall back to tactility / general /
    general_reference. phase accepts a string or a list. kind/status kept
    only if they are in the closed vocab.
    """
    claim = re.sub(r"\s+", " ", str(raw.get("claim") or "")).strip()
    if not claim:
        raise ValueError("draft card missing claim")
    source_type = doc.get("source_type") if doc.get("source_type") in enums["source_type"] else "article"
    phases = _labels(raw.get("phase"))
    kind = raw.get("kind") if raw.get("kind") in enums["kind"] else None
    status = raw.get("status") if raw.get("status") in enums["status"] else None
    card = {
        "id": _unique_id(raw.get("id") or claim, taken),
        "claim": claim,
        "variables": _labels(raw.get("variables"), "tactility"),
        "style_scope": _labels(raw.get("style_scope"), "general"),
        "source": {"type": source_type, "reference": doc.get("reference", "")},
        "confidence": min(1.0, max(0.0, float(raw.get("confidence", 0.4)))),
        "contradicts": [c for c in raw.get("contradicts", []) if isinstance(c, str)],
        "refines": [c for c in raw.get("refines", []) if isinstance(c, str)],
        "relevance_targets": _labels(raw.get("relevance_targets"), "general_reference"),
        "created_at": date.today().isoformat(),
    }
    if phases:
        card["phase"] = phases
    if kind:
        card["kind"] = kind
    if status:
        card["status"] = status
    if raw.get("corroboration"):
        card["corroboration"] = raw["corroboration"]
    return card


def draft_template(doc: dict, schema: Optional[dict] = None) -> List[dict]:
    """No-LLM fallback: one low-confidence shell you fill in by hand."""
    enums = schema_enums(schema)
    excerpt = re.sub(r"\s+", " ", doc.get("text") or "")[:240].strip()
    claim = excerpt or "REPLACE with one atomic claim from the source."
    return [
        normalize_card(
            {
                "claim": claim,
                "variables": ["tactility"],
                "style_scope": ["general"],
                "confidence": 0.2,
                "relevance_targets": ["general_reference"],
            },
            doc,
            enums,
            load_card_ids(),
        )
    ]


def draft_claude(doc: dict, schema: Optional[dict] = None) -> List[dict]:
    """Claude author path. Needs ANTHROPIC_API_KEY. Same return as draft_template."""
    from synthesis import ClaudeBot

    return ClaudeBot().ingest(doc, schema)


DRAFTERS = {"template": draft_template, "claude": draft_claude}


def ingest(
    source: str,
    spec: str,
    drafter: str = "template",
) -> List[dict]:
    """Scrape `spec` via SCRAPERS[source], then draft cards. Does not write jsonl."""
    if source not in SCRAPERS:
        raise ValueError(f"unknown source {source!r}. scrapers: {sorted(SCRAPERS)}")
    if drafter not in DRAFTERS:
        raise ValueError(f"unknown drafter {drafter!r}. drafters: {sorted(DRAFTERS)}")
    doc = SCRAPERS[source](spec)
    if not doc.get("text"):
        raise ValueError(f"{source} produced no text")
    return DRAFTERS[drafter](doc)


def write_candidates(cards: List[dict], path: str = CANDIDATES_PATH) -> None:
    """Overwrite path with one JSON object per line."""
    with open(path, "w") as f:
        for card in cards:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")


def commit_candidates(cards: List[dict], path: str = CARDS_PATH) -> List[dict]:
    """Insert new cards above trailing # notes. Returns the ones written."""
    existing = set(load_card_ids(path))
    fresh = [c for c in cards if c["id"] not in existing]
    if not fresh:
        return []
    body, notes = [], []
    if os.path.exists(path):
        with open(path) as f:
            lines = f.readlines()
        while lines and is_jsonl_note(lines[-1]):
            notes.insert(0, lines.pop())
        body = lines
    with open(path, "w") as f:
        f.writelines(body)
        if body and not body[-1].endswith("\n"):
            f.write("\n")
        for card in fresh:
            f.write(json.dumps(card, ensure_ascii=False) + "\n")
        f.writelines(notes)
    return fresh


# ---------------------------------------------------------------------------
# Terminal add — turn one typed claim into a schema card and append it.
# ---------------------------------------------------------------------------

STYLE_ALIASES = {
    "neapolitan": ["neapolitan", "neopolitan"],
    "ny_slice": ["ny_slice", "ny-slice", "ny style", "ny-style", "new york", "new-york"],
    "sicilian": ["sicilian"],
    "al_taglio": ["al_taglio", "al taglio", "taglio"],
    "sourdough": ["sourdough", "levain"],
    "general": ["general", "style-agnostic"],
}
TARGET_ALIASES = {
    "fermentation_lab_dashboard": [
        "fermentation_lab_dashboard",
        "fermentation lab",
        "lab dashboard",
        "dashboard",
    ],
    "pizza_cart_app": ["pizza_cart_app", "pizza cart", "cart app"],
    "mise_en_place": ["mise_en_place", "mise en place", "mise"],
    "general_reference": ["general_reference", "general reference"],
}
VARIABLE_ALIASES = {
    "hydration": ["hydration", "hydrate", "hydrated"],
    "humidity": ["humidity", "humid", "relative humidity"],
    "leavening": ["leavening", "yeast", "starter"],
    "kneading": ["kneading", "knead", "windowpane"],
    "tactility": ["tactility", "poke", "feel", "touch", "windowpane"],
    "extensibility": ["extensibility", "extensible", "openability", "easier to open"],
    "gluten": ["gluten", "gluten development"],
    "fermentation_time": ["fermentation_time", "fermentation time", "ferment", "fermented", "proof", "proofed", "over-proof", "overproofed"],
    "cold_ferment": ["cold_ferment", "cold ferment", "cold fermentation", "retarded"],
    "temperature": ["temperature", "temp", "degree hours"],
    "salt": ["salt", "salinity"],
    "flour_protein": ["flour_protein", "protein", "flour"],
    "bake_temp": ["bake_temp", "oven temp", "bake temperature"],
    "bake_time": ["bake_time", "bake time"],
}
PHASE_ALIASES = {
    "mixing": ["mixing", "mix"],
    "kneading": ["kneading", "knead"],
    "bulk_fermentation": ["bulk_fermentation", "bulk fermentation", "bulk"],
    "shaping": ["shaping", "shape"],
    "proofing": ["proofing", "proof", "over-proof"],
    "baking": ["baking", "bake"],
    "diagnostic": ["diagnostic", "windowpane", "poke test"],
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _mentions(haystack: str, needle: str) -> bool:
    hay = f" {_norm(haystack)} "
    pin = f" {_norm(needle)} "
    return bool(pin.strip()) and pin in hay


def _split_csv(value: Optional[str]) -> List[str]:
    if not value or value in ("all", "*"):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def infer_labels(text: str, aliases: Dict[str, List[str]]) -> List[str]:
    """All alias keys mentioned in text, in alias-map order. May be empty."""
    hits = []
    for label, names in aliases.items():
        if any(_mentions(text, name) for name in names) and label not in hits:
            hits.append(label)
    return hits


def card_from_claim(
    claim: str,
    style: Optional[str] = None,
    target: Optional[str] = None,
    variable: Optional[str] = None,
    phase: Optional[str] = None,
    card_id: Optional[str] = None,
    confidence: float = 0.5,
    reference: str = "terminal",
    refines: Optional[str] = None,
) -> dict:
    """Process a typed claim into one schema card.

    Contract: claim non-empty. CLI style/target/variable/phase win over aliases.
    Returns a schema-shaped dict; does not write disk (caller commits).
    """
    claim = re.sub(r"\s+", " ", (claim or "")).strip()
    if not claim:
        raise ValueError("add requires a non-empty claim")

    enums = schema_enums()
    styles = _split_csv(style) or infer_labels(claim, STYLE_ALIASES)
    styles = [s for s in styles if s != "general"] or ["general"]
    targets = _split_csv(target) or infer_labels(claim, TARGET_ALIASES)
    targets = [t for t in targets if t != "general_reference"] or ["general_reference"]
    variables = _split_csv(variable) or infer_labels(claim, VARIABLE_ALIASES)
    phases = _split_csv(phase) or infer_labels(claim, PHASE_ALIASES)

    return normalize_card(
        {
            "id": card_id,
            "claim": claim,
            "variables": variables,
            "style_scope": styles,
            "phase": phases,
            "confidence": confidence,
            "refines": _split_csv(refines),
            "relevance_targets": targets,
        },
        {"source_type": "conversation", "reference": reference},
        enums,
        load_card_ids(),
    )
