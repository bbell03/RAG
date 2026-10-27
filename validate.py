"""
Truth evaluation — critic stage after Voyage library coverage.

Voyage answers "do we already have this?"
This module answers "is the claim supported, contradicted, or unproven?"
using library neighbors, cited URLs, a short web fetch, and a chat model as critic.

Safety: at most MAX_PAGES pages, each truncated to MAX_PAGE_CHARS before
they go to the critic. Search HTML is not kept. A missing API key is a
skipped verdict, not a crash. Gemini (GEMINI_API_KEY) is the free critic;
Claude (ANTHROPIC_API_KEY) is the paid one.
"""

import os
import re
from datetime import date
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from ingest import scrape_url

Hit = Tuple[float, dict]
VERDICTS = ("support", "conflict", "insufficient")
MAX_PAGES = 2
MAX_PAGE_CHARS = 2500


def is_url(value: Optional[str]) -> bool:
    """True only for http(s) with a host. Blocks file: and javascript:."""
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _urls_from_hits(hits: List[Hit], extra: Optional[List[str]] = None) -> List[str]:
    """http(s) refs from card sources, then extras. Deduped, order preserved."""
    urls = []
    for _, card in hits:
        ref = ((card.get("source") or {}).get("reference") or "")
        if is_url(ref) and ref not in urls:
            urls.append(ref)
    for ref in extra or []:
        if is_url(ref) and ref not in urls:
            urls.append(ref)
    return urls


def _ddg_urls(query: str, limit: int = 3) -> List[str]:
    """Parse a few result URLs from DDG HTML. Caps the body at 200k; closes the socket."""
    import requests

    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": "dough-research-validate/0.1"},
        timeout=20,
        stream=True,
    )
    try:
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(65536):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) >= 200_000:
                break
        html = bytes(buf[:200_000]).decode(resp.encoding or "utf-8", errors="replace")
    finally:
        resp.close()
    found = []
    for encoded in re.findall(r"uddg=([^&\"]+)", html):
        url = unquote(encoded)
        if not is_url(url) or url in found:
            continue
        host = urlparse(url).netloc.lower()
        if any(skip in host for skip in ("duckduckgo.com", "youtube.com", "facebook.com")):
            continue
        found.append(url)
        if len(found) >= limit:
            break
    return found


def _fetch_pages(urls: List[str]) -> List[dict]:
    """Fetch at most MAX_PAGES urls. Failed pages stay in the list with error set."""
    pages = []
    for url in urls[:MAX_PAGES]:
        try:
            doc = scrape_url(url)
        except (OSError, ValueError, RuntimeError) as exc:
            pages.append({"url": url, "text": "", "error": str(exc)})
            continue
        pages.append({"url": url, "text": (doc.get("text") or "")[:MAX_PAGE_CHARS], "error": None})
    return pages


def gather_evidence(
    claim: str,
    hits: List[Hit],
    extra_urls: Optional[List[str]] = None,
    use_web: bool = True,
) -> dict:
    """Build the critic's evidence pack. Does not call Claude.

    Returns {library, pages, search_error}. pages[].text is truncated.
    """
    library = []
    for sim, card in hits[:5]:
        library.append(
            {
                "id": card.get("id"),
                "claim": card.get("claim"),
                "confidence": card.get("confidence"),
                "source": card.get("source") or {},
                "similarity": round(float(sim), 4),
            }
        )
    urls = _urls_from_hits(hits, extra_urls)
    search_error = None
    if use_web and len(urls) < MAX_PAGES:
        try:
            for url in _ddg_urls(f"{claim} pizza dough", limit=3):
                if url not in urls:
                    urls.append(url)
        except (OSError, RuntimeError) as exc:
            search_error = str(exc)
    pages = _fetch_pages(urls) if use_web or urls else []
    return {"library": library, "pages": pages, "search_error": search_error}


def _parse_truth(payload: dict) -> dict:
    """Clamp critic JSON onto a verdict dict. Unknown verdict → insufficient."""
    verdict = str(payload.get("verdict") or "insufficient").lower()
    if verdict not in VERDICTS:
        verdict = "insufficient"
    citations = []
    for item in payload.get("citations") or []:
        if isinstance(item, dict):
            citations.append(
                {
                    "title": str(item.get("title") or "")[:160],
                    "url": item.get("url") or item.get("id") or "",
                }
            )
        elif item:
            citations.append({"title": str(item)[:160], "url": ""})
    return {
        "verdict": verdict,
        "score": max(0.0, min(1.0, float(payload.get("score", 0.4)))),
        "rationale": str(payload.get("rationale") or "").strip()[:500],
        "citations": citations,
        "contradicts": [str(c) for c in (payload.get("contradicts") or []) if str(c).strip()],
        "checked_at": date.today().isoformat(),
    }


def _skip(why: str) -> dict:
    return {
        "verdict": "skipped",
        "score": 0.0,
        "rationale": why,
        "citations": [],
        "contradicts": [],
        "checked_at": date.today().isoformat(),
    }


def resolve_critic(critic: Optional[str] = None) -> Optional[str]:
    """Pick claude or gemini. Explicit name wins. Otherwise the key that is set.

    Gemini wins when both keys are set and no critic was requested, because
    that path does not require an Anthropic bill. Returns None if neither key exists.
    """
    if critic in ("claude", "gemini"):
        return critic
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def evaluate_truth(
    claim: str,
    hits: List[Hit],
    extra_urls: Optional[List[str]] = None,
    use_web: bool = True,
    critic: Optional[str] = None,
) -> dict:
    """Critic: support / conflict / insufficient.

    Contract:
        always returns a dict with verdict (including 'skipped').
        never raises on API/network failure — those become skipped.
        does not mutate hits. May fetch the network if use_web.
        critic is 'claude', 'gemini', or None (whichever key is set).
    """
    name = resolve_critic(critic)
    if name is None:
        return _skip("No GEMINI_API_KEY or ANTHROPIC_API_KEY; truth check skipped.")
    needed = "GEMINI_API_KEY" if name == "gemini" else "ANTHROPIC_API_KEY"
    if not os.environ.get(needed):
        return _skip(f"{needed} not set; truth check needs {name}.")

    evidence = gather_evidence(claim, hits, extra_urls=extra_urls, use_web=use_web)
    try:
        from synthesis import make_bot

        payload = make_bot(name).validate(claim, evidence)
        result = _parse_truth(payload)
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        return {
            "verdict": "skipped",
            "score": 0.0,
            "rationale": f"truth critic failed: {exc}",
            "citations": [],
            "contradicts": [],
            "checked_at": date.today().isoformat(),
        }
    result["pages_checked"] = [p.get("url") for p in evidence["pages"] if p.get("url")]
    return result


def apply_truth(library_score: float, truth: Optional[dict]) -> Tuple[float, Optional[bool]]:
    """Blend library coverage with truth.

    Returns: (knowledge_score, truth_ok)
        truth_ok is False if conflict/insufficient (blocks recommend),
        None if skipped, True if support.
    """
    if not truth or truth.get("verdict") in (None, "skipped"):
        return round(library_score, 4), None
    verdict = truth["verdict"]
    tscore = float(truth.get("score") or 0.0)
    if verdict == "conflict":
        return round(min(library_score, 0.2), 4), False
    if verdict == "insufficient":
        return round(min(library_score * 0.85, 0.55), 4), False
    return round(0.55 * library_score + 0.45 * tscore, 4), True
