"""Ranked URL discovery for the truth critic and claims controller.

discover_sources(claim, hits) → [{url, discovered_via, rank, reason}, ...]
from library hit references, operator extras, then DuckDuckGo search.
validate.gather_evidence and passive-research research_claim consume this list.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse


Hit = Tuple[float, dict]


def is_url(value: Optional[str]) -> bool:
    """True only for http(s) with a host. Blocks file: and javascript:."""
    parsed = urlparse(value or "")
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _dedupe_ordered(values: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _candidate_from_reference(url: str, source: str, rank: float, reason: str) -> Dict[str, Any]:
    return {
        "url": url,
        "discovered_via": source,
        "rank": float(rank),
        "reason": reason,
    }


def _urls_from_hits(hits: Sequence[Hit], extra: Optional[List[str]] = None) -> List[str]:
    """http(s) refs from card sources, then extras. Deduped, order preserved."""
    urls = []
    for _, card in hits:
        ref = ((card.get("source") or {}).get("reference") or "")
        if is_url(ref):
            urls.append(ref)
    for ref in extra or []:
        if is_url(ref):
            urls.append(ref)
    return _dedupe_ordered(urls)


def _ddg_urls(query: str, limit: int = 3) -> List[str]:
    """Parse a few result URLs from DDG HTML. Caps the body at 200k; closes the socket."""
    import requests

    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
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
    candidates = [unquote(m) for m in re.findall(r"uddg=([^&\"]+)", html)]
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


def discover_sources(
    claim: str,
    hits: Sequence[Hit],
    extra_urls: Optional[List[str]] = None,
    use_web: bool = True,
    max_candidates: int = 6,
    search_limit: int = 3,
    search_queries: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Phase 1-3 of evidence preparation:

    1. discover candidate URLs from card sources and search results
    2. normalize/dedupe them while preserving the original ordering intent
    3. rank them by library similarity and source provenance

    Returned objects are structured and ready for later page fetches and truth
    evaluation without forcing callers to re-parse raw URLs.
    """
    candidates: List[Dict[str, Any]] = []
    seen_urls = set()

    # Step 1: library-derived URLs first, ranked by similarity.
    for sim, card in hits[:5]:
        ref = ((card.get("source") or {}).get("reference") or "")
        if not is_url(ref):
            continue
        if ref in seen_urls:
            continue
        seen_urls.add(ref)
        candidates.append(
            _candidate_from_reference(
                ref,
                source="library",
                rank=float(sim),
                reason=f"library hit similarity {float(sim):.3f}",
            )
        )

    # Step 1b: explicit operator/extra URLs next.
    for url in extra_urls or []:
        if not is_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append(
            _candidate_from_reference(
                url,
                source="extra",
                rank=0.85,
                reason="operator-provided URL",
            )
        )

    # Step 1c: web search across primary claim plus optional reformulations.
    queries = _dedupe_ordered(
        [q.strip() for q in (list(search_queries or []) + [claim, f"{claim} pizza dough"]) if q and str(q).strip()]
    )
    if use_web and len(candidates) < max_candidates:
        for query in queries:
            if len(candidates) >= max_candidates:
                break
            try:
                for url in _ddg_urls(query, limit=search_limit):
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    candidates.append(
                        _candidate_from_reference(
                            url,
                            source="search",
                            rank=0.60,
                            reason=f"ddg search: {query[:80]}",
                        )
                    )
                    if len(candidates) >= max_candidates:
                        break
            except (OSError, RuntimeError):
                continue

    # Step 2 + 3: normalize / dedupe / rank. We keep library information first,
    # then extra URLs, then search results, while preserving a stable score.
    candidates.sort(key=lambda item: (item["rank"], item["discovered_via"]), reverse=True)
    return candidates[:max_candidates]


__all__ = ["is_url", "_ddg_urls", "discover_sources", "_urls_from_hits"]
