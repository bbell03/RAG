"""
Claude bot — the generative side of this pipeline.

Voyage embeds and ranks. This module is the only place Claude writes or
critiques claims:

  ingest     source text → candidate cards          (author)
  validate   claim + evidence → support/conflict    (critic)
  synthesize question + cards → recommendation      (composer)
  reason     scores + truth + cards → next action   (planner; not wired yet)

    python synthesis.py ingest --text "..."
    python synthesis.py validate --claim "..."

Safety: one request at a time, 90s timeout, max_tokens 1500. Error bodies
truncated. Ingest source is sliced to MAX_DRAFT_CHARS before the prompt.
"""

import argparse
import json
import os
from typing import List, Optional

MODEL = "claude-sonnet-4-6"
API_URL = "https://api.anthropic.com/v1/messages"


class ClaudeBot:
    """One Anthropic client: ingest / validate / synthesize / reason."""

    def __init__(self, model: str = MODEL):
        self.model = model

    def complete_json(self, user_content: str, system: Optional[str] = None) -> dict:
        """POST Messages API and parse a JSON object from the text block.

        Contract: returns dict. Raises RuntimeError (key/HTTP) or ValueError (bad JSON).
        Note: does not retry. Prompt size is the caller's job to cap.
        """
        import requests

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ClaudeBot needs ANTHROPIC_API_KEY. Voyage only embeds.")
        body = {
            "model": self.model,
            "max_tokens": 1500,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system:
            body["system"] = system
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=90,
        )
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code} from Anthropic: {resp.text[:800]}")
        parts = [b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text"]
        text = "\n".join(parts).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Claude returned non-JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("Claude returned non-object JSON")
        return payload

    def ingest(self, doc: dict, schema: Optional[dict] = None) -> List[dict]:
        """Author: source doc → 1–4 schema cards.

        Requires doc['text']. Does not write cards.jsonl.
        """
        from ingest import MAX_DRAFT_CHARS, load_card_ids, normalize_card, schema_enums

        enums = schema_enums(schema)
        body = (doc.get("text") or "").strip()
        if not body:
            raise ValueError("source document has no text to draft from")
        payload = self.complete_json(
            (
                "Draft 1-4 dough-research insight cards from this source.\n"
                "Each card is one atomic, paraphrased, falsifiable claim. "
                "Do not copy long passages.\n"
                f"Preferred variables: {enums['variables']} (other slugs ok)\n"
                f"Preferred style_scope: {enums['style_scope']} (other slugs ok)\n"
                f"Preferred phase (array): {enums['phase']}\n"
                f"kind: {enums['kind']}  status: {enums['status']}\n"
                f"Preferred relevance_targets: {enums['relevance_targets']}\n"
                "Return JSON: {\"cards\": [{\"claim\": \"...\", \"variables\": [], "
                "\"style_scope\": [], \"phase\": [], \"kind\": null, \"status\": \"draft\", "
                "\"confidence\": 0.5, \"relevance_targets\": [], \"refines\": [], "
                "\"contradicts\": []}]}\n\n"
                f"Source ({doc.get('reference')}):\n{body[:MAX_DRAFT_CHARS]}"
            ),
            system="You write structured insight cards for a pizza-dough research library. Reply with JSON only.",
        )
        raw_cards = payload.get("cards")
        if not isinstance(raw_cards, list) or not raw_cards:
            raise ValueError("ClaudeBot.ingest returned no cards")
        taken = load_card_ids()
        return [normalize_card(raw, doc, enums, taken) for raw in raw_cards]

    def validate(self, claim: str, evidence: dict) -> dict:
        """Critic: claim + evidence → raw JSON (caller parses verdict).

        Claim is capped at 2k chars. Evidence pages should already be truncated.
        """
        return self.complete_json(
            (
                "You are a critic, not the author of this dough-research claim.\n"
                "Decide if independent evidence SUPPORTS, CONFLICTS with, or is INSUFFICIENT "
                "to judge the claim.\n"
                "A similar library card is not support. Support requires a source page or a "
                "library card that states the same mechanism with its own citation.\n"
                "Conflict if a source or library card contradicts the mechanism.\n"
                "Insufficient if pages failed, are off-topic, or only restate the claim.\n"
                "Return JSON only:\n"
                '{"verdict": "support|conflict|insufficient", "score": 0.0, '
                '"rationale": "...", "citations": [{"title": "...", "url": "..."}], '
                '"contradicts": ["card-id"]}\n\n'
                f"claim: {claim[:2000]}\n"
                f"library_neighbors: {json.dumps(evidence.get('library') or [], ensure_ascii=False)}\n"
                f"source_pages: {json.dumps(evidence.get('pages') or [], ensure_ascii=False)}\n"
                f"search_error: {evidence.get('search_error')}\n"
            ),
            system="You evaluate dough-science claims against evidence. Reply with JSON only.",
        )

    def synthesize(
        self,
        question: str,
        cards: List[dict],
        retrieval: float,
        threshold: float,
        extra: Optional[str] = None,
    ) -> dict:
        """Composer: question + retrieved cards → coverage / recommendation / drafts."""
        from ingest import schema_enums

        enums = schema_enums()
        return self.complete_json(
            (
                "You are validating a pizza-dough research library for one user question.\n"
                "Decide whether the retrieved cards are enough to make a recommendation.\n"
                "If they are not, draft 1-3 NEW atomic cards that close the gaps. "
                "Paraphrase; do not invent lab measurements or cite sources you were not given.\n"
                "Mark new-card confidence at most 0.45 — they are hypotheses until reviewed.\n"
                f"Preferred variables: {enums['variables']} (other slugs ok)\n"
                f"Preferred style_scope: {enums['style_scope']} (other slugs ok)\n"
                f"Preferred phase (array): {enums['phase']}\n"
                f"kind: {enums['kind']}  status: draft for new cards\n"
                f"Preferred relevance_targets: {enums['relevance_targets']}\n"
                "Return JSON only:\n"
                '{"coverage": 0.0, "ready": false, "recommendation": null, '
                '"gaps": ["..."], "cards": [{"claim": "...", "variables": [], '
                "\"style_scope\": [], \"phase\": [], \"kind\": null, \"status\": \"draft\", "
                '"confidence": 0.4, "relevance_targets": [], "refines": [], '
                '"contradicts": []}]}\n'
                f"threshold: {threshold}\n"
                f"retrieval_knowledge: {retrieval}\n"
                f"question: {question[:2000]}\n"
                f"cards: {json.dumps(cards, ensure_ascii=False)}\n"
                + (f"extra_instructions: {extra}\n" if extra else "")
            ),
            system="You validate dough-research cards and draft missing ones as JSON. Reply with JSON only.",
        )

    def reason(
        self,
        question: str,
        retrieval: float,
        threshold: float,
        truth: Optional[dict] = None,
        cards: Optional[List[dict]] = None,
        coverage: Optional[float] = None,
    ) -> dict:
        """Planner: pick the next action. Does not write cards or judge truth.

        recommend.py should call this *after* Voyage retrieve + validate.

        Inputs
            question    operator question ("is this dough ready?")
            retrieval   Voyage knowledge_score
            threshold   recommend bar (e.g. 0.7)
            truth       critic output {verdict, score, rationale} or None
            cards       retrieved (and any drafted) card payloads
            coverage    composer coverage 0-1, if synthesize already ran

        Returns
            {
              "action": "recommend" | "draft" | "validate" | "refuse" | "fetch",
              "ready": bool,
              "why": str,
            }

        Policy (pseudocode — local, no extra Claude call yet)::

            if truth.verdict == "conflict":
                return refuse     # do not advise; sources disagree
            if truth.verdict == "insufficient" and retrieval < threshold:
                return fetch      # need a cited page or another card
            if retrieval < threshold or (coverage is not None and coverage < threshold):
                return draft      # library gap → ClaudeBot.ingest / synthesize cards
            if truth.verdict in (None, "skipped") and retrieval >= threshold:
                return validate   # library looks full; still need a critic pass
            if retrieval >= threshold and truth.verdict == "support":
                return recommend  # only now is a cook-facing answer allowed
            return refuse
        """
        cards = cards or []
        verdict = (truth or {}).get("verdict")
        # --- begin policy ---
        if verdict == "conflict":
            action, ready, why = "refuse", False, "critic conflict; do not recommend"
        elif verdict == "insufficient" and retrieval < threshold:
            action, ready, why = "fetch", False, "low retrieval and unproven; get a source"
        elif retrieval < threshold or (coverage is not None and coverage < threshold):
            action, ready, why = "draft", False, "library below threshold; draft gap cards"
        elif verdict in (None, "skipped") and retrieval >= threshold:
            action, ready, why = "validate", False, "library hit the bar; run critic next"
        elif retrieval >= threshold and verdict == "support":
            action, ready, why = "recommend", True, "retrieval + support clear the bar"
        else:
            action, ready, why = "refuse", False, "not enough to act"
        # --- end policy ---
        return {
            "action": action,
            "ready": ready,
            "why": why,
            "question": question,
            "retrieval": retrieval,
            "coverage": coverage,
            "truth": verdict,
            "n_cards": len(cards),
        }


def claude_json(user_content: str, system: Optional[str] = None) -> dict:
    """Module-level shortcut. Same contract as ClaudeBot.complete_json."""
    return ClaudeBot().complete_json(user_content, system=system)


def main() -> None:
    """CLI: ingest --text/--url, or validate --claim. Prints JSON to stdout."""
    p = argparse.ArgumentParser(description="Claude bot for ingest and validation.")
    p.add_argument("action", choices=["ingest", "validate"])
    p.add_argument("--text", default=None, help="Source text for ingest")
    p.add_argument("--url", default=None, help="Source URL for ingest")
    p.add_argument("--claim", default=None, help="Claim to validate")
    p.add_argument("--no-web", action="store_true")
    args = p.parse_args()
    bot = ClaudeBot()

    if args.action == "ingest":
        from ingest import scrape_text, scrape_url

        if args.url:
            doc = scrape_url(args.url)
        elif args.text:
            doc = scrape_text(args.text)
        else:
            p.error("ingest needs --text or --url")
        cards = bot.ingest(doc)
        for card in cards:
            print(json.dumps(card, ensure_ascii=False))
        return

    claim = (args.claim or args.text or "").strip()
    if not claim:
        p.error("validate needs --claim")
    from validate import evaluate_truth

    print(json.dumps(evaluate_truth(claim, [], extra_urls=[], use_web=not args.no_web), indent=2))


if __name__ == "__main__":
    main()
