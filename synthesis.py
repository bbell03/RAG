"""
Chat bots — the generative side of this pipeline.

Voyage embeds and ranks. This module is the only place a chat model writes
or critiques claims:

  ingest     source text → candidate cards          (author)
  validate   claim + evidence → support/conflict    (critic)
  synthesize question + cards → recommendation      (composer)
  reason     scores + truth + cards → next action   (planner; not wired yet)

    python synthesis.py ingest --text "..." --bot gemini
    python synthesis.py validate --claim "..." --bot gemini

Claude uses ANTHROPIC_API_KEY. Gemini 3.8 Flash uses GEMINI_API_KEY and is
on Google's free tier (aistudio.google.com/apikey). Prompts sent on that
free tier can be used to improve Google's products.

A local OpenAI-compatible server (Ollama, LM Studio, llama.cpp) can take the
same prompt when the cloud model is out of quota or unreachable. Set
LOCAL_LLM_URL and LOCAL_LLM_MODEL. Pass --bot local to skip the cloud call.

Safety: one request at a time, 90s timeout. Error bodies truncated. Ingest
source is sliced to MAX_DRAFT_CHARS before the prompt.
"""

import argparse
import json
import os
import time
import warnings
from typing import List, Optional

warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")
warnings.filterwarnings("ignore", message=".*compiled with 'LibreSSL.*")


def load_env(path: Optional[str] = None) -> None:
    """Load KEY=VALUE lines from the project .env. Existing shell vars win.

    The scripts read VOYAGE_API_KEY and GEMINI_API_KEY from the environment.
    A .env file is optional so a local terminal can keep the keys out of git.
    Empty values are ignored. Does not override a variable already set.
    """
    env_path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


load_env()

CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
BOTS = ("gemini", "claude", "local")
# Status codes that mean "this cloud model cannot answer right now".
_UNAVAILABLE_STATUS = (401, 403, 408, 429, 500, 502, 503, 504, 529)


def _parse_json_text(text: str, label: str) -> dict:
    """Strip a markdown fence and parse one JSON object."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} returned non-object JSON")
    return payload


class CardBot:
    """Shared author / critic / composer prompts. Subclasses only speak HTTP."""

    label = "model"

    def complete_json(self, user_content: str, system: Optional[str] = None) -> dict:
        """Return one JSON object. Raises RuntimeError (key/HTTP) or ValueError (bad JSON)."""
        raise NotImplementedError

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
            raise ValueError(f"{self.label}.ingest returned no cards")
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

        Policy (pseudocode — local, no extra model call)::

            if truth.verdict == "conflict":
                return refuse     # do not advise; sources disagree
            if truth.verdict == "insufficient" and retrieval < threshold:
                return fetch      # need a cited page or another card
            if retrieval < threshold or (coverage is not None and coverage < threshold):
                return draft      # library gap → ingest / synthesize cards
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

    def plan_claim(self, card: dict, evidence: dict, library: Optional[List[dict]] = None) -> dict:
        """Controller planner: one card + fresh evidence → library edits.

        Does not write cards.jsonl. The caller (passive-research) decides
        what to stage or commit.

        Returns
            action          validate | modify | remove | skip
            revised_claim   one sentence, only when action is modify
            why             short rationale
            adds            0–2 new atomic claims the evidence states that
                            this card does not already say
            propagations    0–3 learning cards derived from this one:
                            direction "specialize" (style, phase, or workflow
                            target) or "expand" (broader style or new variable)
        """
        from ingest import schema_enums

        enums = schema_enums()
        neighbors = []
        for item in (library or [])[:24]:
            neighbors.append(
                {
                    "id": item.get("id"),
                    "claim": (item.get("claim") or "")[:240],
                    "style_scope": item.get("style_scope"),
                    "phase": item.get("phase"),
                    "variables": item.get("variables"),
                    "relevance_targets": item.get("relevance_targets"),
                    "refines": item.get("refines"),
                }
            )
        payload = self.complete_json(
            (
                "You maintain an atomic pizza-dough claim library.\n"
                "Given one existing card and fresh evidence, choose a single action:\n"
                "- validate: evidence supports the claim as written. revised_claim null.\n"
                "- modify: evidence supports a corrected or narrower sentence. "
                "revised_claim is one falsifiable sentence. Same fact, fixed scope or mechanism. "
                "Do not invent measurements.\n"
                "- remove: the mechanism is contradicted and cannot be repaired by a narrower true claim.\n"
                "- skip: evidence is insufficient, off-topic, or only restates the claim.\n"
                "Also return learning drafts when action is validate or modify.\n"
                "adds: 0-2 NEW claims the evidence actually states, not paraphrases of this card, "
                "not duplicates of library neighbors. confidence at most 0.45.\n"
                "propagations: 0-3 cards that spread this claim into learning workflows.\n"
                "  specialize: same mechanism, but a different style_scope, phase, or relevance_target "
                "so a lab, cart, or mise workflow can use it. Set direction to specialize.\n"
                "  expand: a broader style (often general) or an adjacent variable the evidence supports. "
                "Set direction to expand.\n"
                "Do not copy the parent claim verbatim. One sentence each. status draft.\n"
                f"Preferred variables: {enums['variables']} (other slugs ok)\n"
                f"Preferred style_scope: {enums['style_scope']} (other slugs ok)\n"
                f"Preferred phase: {enums['phase']}\n"
                f"kind: {enums['kind']}\n"
                f"Preferred relevance_targets: {enums['relevance_targets']}\n"
                "Workflow targets you may specialize into: fermentation_lab_dashboard, "
                "pizza_cart_app, mise_en_place, general_reference.\n"
                "Return JSON only:\n"
                '{"action": "validate|modify|remove|skip", "revised_claim": null, "why": "...", '
                '"adds": [{"claim": "...", "variables": [], "style_scope": [], "phase": [], '
                '"kind": null, "confidence": 0.4, "relevance_targets": []}], '
                '"propagations": [{"claim": "...", "direction": "specialize|expand", '
                '"variables": [], "style_scope": [], "phase": [], "kind": null, '
                '"confidence": 0.4, "relevance_targets": []}]}\n\n'
                f"card: {json.dumps({'id': card.get('id'), 'claim': card.get('claim'), 'variables': card.get('variables'), 'style_scope': card.get('style_scope'), 'phase': card.get('phase'), 'relevance_targets': card.get('relevance_targets'), 'refines': card.get('refines')}, ensure_ascii=False)}\n"
                f"evidence: {json.dumps(evidence, ensure_ascii=False)[:6000]}\n"
                f"library: {json.dumps(neighbors, ensure_ascii=False)[:6000]}\n"
            ),
            system="You edit a dough-research claim library from evidence. Reply with JSON only.",
        )
        return _normalize_claim_plan(payload)


def _normalize_claim_plan(payload: dict) -> dict:
    """Clamp a plan_claim payload onto the controller contract."""
    action = str(payload.get("action") or "skip").strip().lower()
    if action not in ("validate", "modify", "remove", "skip"):
        action = "skip"
    revised = payload.get("revised_claim")
    revised = " ".join(str(revised).split()).strip() if revised else None
    if action == "modify" and not revised:
        action = "validate"
    if action != "modify":
        revised = None
    why = " ".join(str(payload.get("why") or "").split()).strip()[:500]

    def _drafts(key: str, limit: int) -> List[dict]:
        raw = payload.get(key) or []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            claim = " ".join(str(item.get("claim") or "").split()).strip()
            if not claim:
                continue
            direction = str(item.get("direction") or item.get("mode") or "").strip().lower()
            if direction not in ("specialize", "expand"):
                direction = ""
            out.append(
                {
                    "claim": claim,
                    "direction": direction,
                    "variables": item.get("variables") or [],
                    "style_scope": item.get("style_scope") or [],
                    "phase": item.get("phase") or [],
                    "kind": item.get("kind"),
                    "confidence": item.get("confidence", 0.4),
                    "relevance_targets": item.get("relevance_targets") or [],
                }
            )
            if len(out) >= limit:
                break
        return out

    return {
        "action": action,
        "revised_claim": revised,
        "why": why or "No rationale provided.",
        "adds": _drafts("adds", 2),
        "propagations": _drafts("propagations", 3),
    }


class ClaudeBot(CardBot):
    """Anthropic client. Needs ANTHROPIC_API_KEY."""

    label = "Claude"

    def __init__(self, model: str = CLAUDE_MODEL):
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
            CLAUDE_URL,
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
        return _parse_json_text("\n".join(parts), self.label)


class GeminiBot(CardBot):
    """Gemini Flash client. Needs GEMINI_API_KEY. Default model is on the free tier."""

    label = "Gemini"

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.environ.get("GEMINI_MODEL") or GEMINI_MODEL

    def complete_json(self, user_content: str, system: Optional[str] = None) -> dict:
        """POST generateContent and parse a JSON object.

        Thinking is pinned to low so the token budget is left for the JSON.
        Temperature is omitted: Gemini 3.6+ ignores it. Retries on 429/503/timeout.
        """
        import requests

        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError(
                "GeminiBot needs GEMINI_API_KEY. Free key: https://aistudio.google.com/apikey. Voyage only embeds."
            )
        body = {
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": {
                "maxOutputTokens": 4096,
                "responseMimeType": "application/json",
                "thinkingConfig": {"thinkingLevel": "low"},
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        last_err = None
        max_attempts = 4
        for attempt in range(max_attempts):
            try:
                resp = requests.post(
                    GEMINI_URL.format(model=self.model),
                    headers={
                        "x-goog-api-key": os.environ["GEMINI_API_KEY"],
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=60,
                )
                if resp.status_code in (429, 503):
                    last_err = RuntimeError(f"{resp.status_code} from Gemini: {resp.text[:800]}")
                    if attempt < max_attempts - 1:
                        time.sleep(2 * (attempt + 1))
                        continue
                if not resp.ok:
                    raise RuntimeError(f"{resp.status_code} from Gemini: {resp.text[:800]}")
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:800]}")
                parts = ((candidates[0].get("content") or {}).get("parts") or [])
                texts = [p.get("text", "") for p in parts if not p.get("thought")]
                return _parse_json_text("\n".join(texts), self.label)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_err = exc
                if attempt < max_attempts - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        if last_err:
            raise last_err


def local_bot_configured() -> bool:
    """True when both the local server URL and a model name are set."""
    return bool((os.environ.get("LOCAL_LLM_URL") or "").strip() and (os.environ.get("LOCAL_LLM_MODEL") or "").strip())


def _chat_completions_url(base: str) -> str:
    """OpenAI-compatible chat URL. Accepts a root, a /v1 base, or the full path."""
    root = (base or "").rstrip("/")
    if root.endswith("/chat/completions"):
        return root
    if root.endswith("/v1"):
        return root + "/chat/completions"
    return root + "/v1/chat/completions"


def _cloud_unavailable(exc: BaseException) -> bool:
    """True for quota, overload, missing cloud keys, and connection failures.

    A 400 or a JSON parse error is not unavailability. Those stay on the caller.
    """
    if type(exc).__name__ in ("Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"):
        return True
    text = str(exc)
    if "GEMINI_API_KEY" in text or "ANTHROPIC_API_KEY" in text:
        return True
    lowered = text.lower()
    if "no candidates" in lowered:
        return True
    for code in _UNAVAILABLE_STATUS:
        if text.startswith(f"{code} ") or f"{code} from " in text:
            return True
    return any(token in lowered for token in ("quota", "resource_exhausted", "unavailable", "overloaded", "timed out"))


class LocalBot(CardBot):
    """OpenAI-compatible local server. Ollama, LM Studio, and llama.cpp all speak this.

    Needs LOCAL_LLM_URL (for example http://127.0.0.1:11434/v1) and LOCAL_LLM_MODEL.
    LOCAL_LLM_API_KEY is optional. LOCAL_LLM_TIMEOUT seconds defaults to 180.
    """

    label = "local"

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.base_url = (base_url or os.environ.get("LOCAL_LLM_URL") or "").strip()
        self.model = (model or os.environ.get("LOCAL_LLM_MODEL") or "").strip()

    def complete_json(self, user_content: str, system: Optional[str] = None) -> dict:
        """POST /chat/completions and parse one JSON object.

        Sends response_format json_object, then retries once without it when
        the server rejects that field. Does not call Gemini or Claude.
        """
        import requests

        if not self.base_url or not self.model:
            raise RuntimeError(
                "LocalBot needs LOCAL_LLM_URL and LOCAL_LLM_MODEL. "
                "Example: LOCAL_LLM_URL=http://127.0.0.1:11434/v1 LOCAL_LLM_MODEL=llama3.1"
            )
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        headers = {"Content-Type": "application/json"}
        key = (os.environ.get("LOCAL_LLM_API_KEY") or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        timeout = float(os.environ.get("LOCAL_LLM_TIMEOUT") or 180)
        url = _chat_completions_url(self.base_url)
        resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code == 400 and "response_format" in body:
            body.pop("response_format")
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code} from local model: {resp.text[:800]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"local model returned no choices: {json.dumps(data)[:800]}")
        text = ((choices[0].get("message") or {}).get("content")) or ""
        return _parse_json_text(text, self.label)


class FallbackBot(CardBot):
    """Try the cloud bot. On quota or an outage, send the same prompt to the local bot."""

    def __init__(self, primary: CardBot, fallback: CardBot):
        self.primary = primary
        self.fallback = fallback
        self.label = primary.label

    def complete_json(self, user_content: str, system: Optional[str] = None) -> dict:
        try:
            return self.primary.complete_json(user_content, system=system)
        except Exception as exc:
            if not _cloud_unavailable(exc):
                raise
            print(
                f"{self.primary.label} unavailable ({str(exc)[:180]}). "
                f"Sending the same prompt to local model {getattr(self.fallback, 'model', 'local')}."
            )
            try:
                return self.fallback.complete_json(user_content, system=system)
            except Exception as local_exc:
                raise RuntimeError(
                    f"{self.primary.label} unavailable ({exc}); local model failed ({local_exc})"
                ) from local_exc


def make_bot(name: str = "gemini") -> CardBot:
    """gemini → GeminiBot, claude → ClaudeBot, local → LocalBot.

    When LOCAL_LLM_URL and LOCAL_LLM_MODEL are set, gemini and claude fall
    back to the local server after a quota or availability failure. The same
    prompt is sent. Voyage is not involved.
    """
    if name == "local":
        return LocalBot()
    if name == "gemini":
        primary: CardBot = GeminiBot()
    elif name == "claude":
        primary = ClaudeBot()
    else:
        raise ValueError(f"unknown bot {name!r}. bots: {list(BOTS)}")
    if local_bot_configured():
        return FallbackBot(primary, LocalBot())
    return primary


def claude_json(user_content: str, system: Optional[str] = None) -> dict:
    """Module-level shortcut. Same contract as ClaudeBot.complete_json."""
    return ClaudeBot().complete_json(user_content, system=system)


def main() -> None:
    """CLI: ingest --text/--url, or validate --claim. Prints JSON to stdout."""
    p = argparse.ArgumentParser(description="Chat bot for ingest and validation.")
    p.add_argument("action", choices=["ingest", "validate"])
    p.add_argument("--bot", default="gemini", choices=list(BOTS), help="gemini is the free tier. claude needs ANTHROPIC_API_KEY. local uses LOCAL_LLM_URL.")
    p.add_argument("--text", default=None, help="Source text for ingest")
    p.add_argument("--url", default=None, help="Source URL for ingest")
    p.add_argument("--claim", default=None, help="Claim to validate")
    p.add_argument("--no-web", action="store_true")
    args = p.parse_args()
    bot = make_bot(args.bot)

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

    print(json.dumps(evaluate_truth(claim, [], extra_urls=[], use_web=not args.no_web, critic=args.bot), indent=2))


if __name__ == "__main__":
    main()
