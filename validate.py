"""
Truth evaluation — critic stage after Voyage library coverage.

Voyage answers "do we already have this?"
This module answers "is the claim supported, contradicted, or unproven?"
using library neighbors, ranked URLs from research_sources, page fetches,
and a chat critic (gemini / claude / local).

Safety: page count capped (MAX_PAGES, overridable), each page truncated to
MAX_PAGE_CHARS. Missing keys → skipped verdict, not a crash.
"""

import os
import re
from datetime import date
from typing import List, Optional, Tuple
from urllib.parse import unquote, urlparse

from ingest import scrape_url

Hit = Tuple[float, dict]
VERDICTS = ("support", "conflict", "insufficient")
MAX_PAGES = 5
MAX_PAGE_CHARS = 2500
SOURCEWISE_TAG = "sourcewise_supported"


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
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
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
    # Try uddg redirect encoding
    candidates = [unquote(m) for m in re.findall(r"uddg=([^&\"]+)", html)]
    # Also parse direct result hrefs (DDG HTML modern format)
    if not candidates:
        candidates = re.findall(
            r'<a[^>]+class=[\"\'][^\"\']*(?:result__url|result__snippet)[^\"\']*[\"\'][^>]*href=[\"\']([^\"\']+)[\"\']',
            html,
        )
    for url in candidates:
        if not is_url(url) or url in found:
            continue
        host = urlparse(url).netloc.lower()
        if any(skip in host for skip in ("duckduckgo.com", "youtube.com", "facebook.com")):
            continue
        found.append(url)
        if len(found) >= limit:
            break
    return found


def _fetch_pages(urls: List[str], max_pages: Optional[int] = None) -> List[dict]:
    """Fetch up to max_pages successful pages, skipping blocked/failed pages."""
    limit = max_pages if max_pages is not None else MAX_PAGES
    pages = []
    for url in urls:
        if len([p for p in pages if not p.get("error")]) >= limit:
            break
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
    max_pages: Optional[int] = None,
    search_limit: int = 5,
    search_queries: Optional[List[str]] = None,
) -> dict:
    """Build the critic's evidence pack. Does not call Claude.

    Returns {library, pages, discovered, search_error, urls_considered, evidence_mode}.
    pages[].text is truncated. URL discovery goes through research_sources.discover_sources
    so ranking and dedupe stay in one place.
    """
    page_limit = max_pages if max_pages is not None else MAX_PAGES
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
    search_error = None
    discovered: List[dict] = []
    candidate_cap = max(page_limit * 2, 8)
    try:
        from research_sources import discover_sources

        discovered = discover_sources(
            claim,
            hits,
            extra_urls=extra_urls,
            use_web=use_web,
            max_candidates=candidate_cap,
            search_limit=search_limit,
            search_queries=search_queries,
        )
        urls = [item["url"] for item in discovered if item.get("url")]
    except Exception as exc:
        search_error = str(exc)
        urls = _urls_from_hits(hits, extra_urls)
        discovered = [
            {"url": u, "discovered_via": "library", "rank": 0.5, "reason": "fallback hit ref"}
            for u in urls
        ]
        if use_web and len(urls) < page_limit:
            try:
                for url in _ddg_urls(f"{claim} pizza dough", limit=search_limit):
                    if url not in urls:
                        urls.append(url)
                        discovered.append(
                            {
                                "url": url,
                                "discovered_via": "search",
                                "rank": 0.6,
                                "reason": "ddg fallback",
                            }
                        )
            except (OSError, RuntimeError) as search_exc:
                search_error = str(search_exc)
    # Internal evidence (use_web=False): library neighbor payloads only — no HTTP.
    # External evidence (use_web=True): ranked URLs + fetched page text.
    if use_web:
        pages = _fetch_pages(urls, max_pages=page_limit)
        considered = urls
    else:
        pages = []
        considered = []
        # Keep library-derived URL metadata for the source inventory; drop web search hits.
        discovered = [d for d in discovered if d.get("discovered_via") == "library"]
        search_error = search_error or None
    return {
        "library": library,
        "pages": pages,
        "discovered": discovered,
        "search_error": search_error,
        "urls_considered": considered,
        "evidence_mode": "external" if use_web else "internal",
    }


def build_source_list(evidence: dict, citations: Optional[List] = None) -> List[dict]:
    """Full inventory of sources considered in one search/verify loop.

    Includes library neighbors and discovered URLs with discovery provenance,
    fetch status, and whether the critic cited them.
    """
    cited_urls = set()
    cited_ids = set()
    for item in citations or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or ""
        if is_url(url):
            cited_urls.add(url)
        elif url:
            cited_ids.add(str(url))
        title = str(item.get("title") or "")
        if title:
            cited_ids.add(title)
    pages_by_url = {p.get("url"): p for p in (evidence.get("pages") or []) if p.get("url")}
    mode = evidence.get("evidence_mode") or "external"
    sources: List[dict] = []
    seen_urls = set()

    for item in evidence.get("library") or []:
        ref = ((item.get("source") or {}).get("reference") or "")
        url = ref if is_url(ref) else None
        entry = {
            "kind": "library",
            "id": item.get("id"),
            "claim": str(item.get("claim") or "")[:160],
            "url": url,
            "similarity": item.get("similarity"),
            "discovered_via": "library",
            "rank": item.get("similarity"),
            "reason": f"voyage neighbor sim={item.get('similarity')}",
            "fetch_status": "not_applicable",
            "cited": bool((url and url in cited_urls) or (item.get("id") in cited_ids)),
        }
        sources.append(entry)
        if url:
            seen_urls.add(url)

    for cand in evidence.get("discovered") or []:
        url = cand.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        page = pages_by_url.get(url)
        if mode != "external":
            status = "skipped_internal"
        elif page is None:
            status = "not_fetched"
        elif page.get("error"):
            status = "fetched_failed"
        else:
            status = "fetched_ok"
        sources.append(
            {
                "kind": cand.get("discovered_via") or "search",
                "id": None,
                "claim": None,
                "url": url,
                "similarity": None,
                "discovered_via": cand.get("discovered_via") or "search",
                "rank": cand.get("rank"),
                "reason": cand.get("reason") or "",
                "fetch_status": status,
                "cited": url in cited_urls,
            }
        )

    # Citations that never appeared in discovery still belong on the inventory.
    for url in cited_urls:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        page = pages_by_url.get(url)
        if page and not page.get("error"):
            status = "fetched_ok"
        elif page:
            status = "fetched_failed"
        else:
            status = "cited_only"
        sources.append(
            {
                "kind": "citation",
                "id": None,
                "claim": None,
                "url": url,
                "similarity": None,
                "discovered_via": "citation",
                "rank": None,
                "reason": "critic citation not in discovery list",
                "fetch_status": status,
                "cited": True,
            }
        )
    return sources


def is_sourcewise_supported(truth: Optional[dict]) -> bool:
    """True when support is backed by at least one cited http(s) source URL."""
    if not truth or truth.get("verdict") != "support":
        return False
    for src in truth.get("sources") or []:
        if src.get("cited") and is_url(src.get("url")):
            return True
    for item in truth.get("citations") or []:
        if isinstance(item, dict) and is_url(item.get("url")):
            return True
    return False


def apply_sourcewise_tag(card: dict, truth: Optional[dict]) -> dict:
    """Add or remove the sourcewise_supported tag from a card copy."""
    updated = dict(card)
    tags = [t for t in (updated.get("tags") or []) if t != SOURCEWISE_TAG]
    if is_sourcewise_supported(truth):
        tags.append(SOURCEWISE_TAG)
    if tags:
        updated["tags"] = tags
    elif "tags" in updated:
        updated["tags"] = []
    return updated


def _string_list(values, limit: int = 8, width: int = 240) -> List[str]:
    out = []
    for item in values or []:
        text = str(item or "").strip()
        if text:
            out.append(text[:width])
        if len(out) >= limit:
            break
    return out


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
        "found": _string_list(payload.get("found")),
        "not_found": _string_list(payload.get("not_found")),
        "open_doubts": _string_list(payload.get("open_doubts")),
        "checked_at": date.today().isoformat(),
    }


def _skip(why: str) -> dict:
    return {
        "verdict": "skipped",
        "score": 0.0,
        "rationale": why,
        "citations": [],
        "contradicts": [],
        "found": [],
        "not_found": [],
        "open_doubts": [why],
        "checked_at": date.today().isoformat(),
    }


def resolve_critic(critic: Optional[str] = None) -> Optional[str]:
    """Pick claude, gemini, or local. Explicit name wins. Otherwise the key that is set.

    Gemini wins when both cloud keys are set and no critic was requested, because
    that path does not require an Anthropic bill. A configured local model is
    used when neither cloud key exists. Returns None if nothing is configured.
    """
    if critic in ("claude", "gemini", "local"):
        return critic
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    from synthesis import local_bot_configured

    if local_bot_configured():
        return "local"
    return None


def evaluate_truth(
    claim: str,
    hits: List[Hit],
    extra_urls: Optional[List[str]] = None,
    use_web: bool = True,
    critic: Optional[str] = None,
    max_pages: Optional[int] = None,
    search_limit: int = 5,
    search_queries: Optional[List[str]] = None,
) -> dict:
    """Critic: support / conflict / insufficient.

    Contract:
        always returns a dict with verdict (including 'skipped').
        never raises on API/network failure — those become skipped.
        does not mutate hits. May fetch the network if use_web.
        critic is 'claude', 'gemini', 'local', or None (whichever key is set).
    """
    name = resolve_critic(critic)
    if name is None:
        return _skip("No GEMINI_API_KEY, ANTHROPIC_API_KEY, or local model; truth check skipped.")
    if name != "local":
        needed = "GEMINI_API_KEY" if name == "gemini" else "ANTHROPIC_API_KEY"
        if not os.environ.get(needed):
            from synthesis import local_bot_configured

            if local_bot_configured():
                name = "local"
            else:
                return _skip(f"{needed} not set; truth check needs {name}.")

    evidence = gather_evidence(
        claim,
        hits,
        extra_urls=extra_urls,
        use_web=use_web,
        max_pages=max_pages,
        search_limit=search_limit,
        search_queries=search_queries,
    )
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
            "found": [],
            "not_found": [],
            "open_doubts": [],
            "checked_at": date.today().isoformat(),
        }
    pages = evidence.get("pages") or []
    result["pages_checked"] = [p.get("url") for p in pages if p.get("url")]
    result["pages_ok"] = [p.get("url") for p in pages if p.get("url") and not p.get("error")]
    result["pages_failed"] = [p.get("url") for p in pages if p.get("url") and p.get("error")]
    result["urls_considered"] = list(evidence.get("urls_considered") or [])
    result["evidence_mode"] = evidence.get("evidence_mode") or ("external" if use_web else "internal")
    result["sources"] = build_source_list(evidence, result.get("citations") or [])
    result["sourcewise_supported"] = is_sourcewise_supported(result)
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
