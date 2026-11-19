import unittest

from validate import (
    SOURCEWISE_TAG,
    apply_sourcewise_tag,
    build_source_list,
    is_sourcewise_supported,
)


class SourceListTests(unittest.TestCase):
    def test_build_source_list_includes_library_and_web(self):
        evidence = {
            "evidence_mode": "external",
            "library": [
                {
                    "id": "windowpane-gluten-check",
                    "claim": "Windowpane indicates gluten development.",
                    "confidence": 1.0,
                    "source": {"type": "article", "reference": "https://fond.kitchen/glossary/gluten-window-test/"},
                    "similarity": 0.81,
                }
            ],
            "discovered": [
                {
                    "url": "https://example.com/bake",
                    "discovered_via": "search",
                    "rank": 0.6,
                    "reason": "ddg search",
                },
                {
                    "url": "https://fond.kitchen/glossary/gluten-window-test/",
                    "discovered_via": "library",
                    "rank": 0.81,
                    "reason": "library hit",
                },
            ],
            "pages": [
                {"url": "https://example.com/bake", "text": "ok", "error": None},
            ],
        }
        citations = [{"title": "Bake guide", "url": "https://example.com/bake"}]
        sources = build_source_list(evidence, citations)
        kinds = {s["kind"] for s in sources}
        self.assertIn("library", kinds)
        self.assertIn("search", kinds)
        cited = next(s for s in sources if s.get("url") == "https://example.com/bake")
        self.assertTrue(cited["cited"])
        self.assertEqual(cited["fetch_status"], "fetched_ok")
        # library URL already listed as library neighbor — not duplicated as search/library candidate
        lib_urls = [s.get("url") for s in sources if s.get("kind") == "library"]
        self.assertEqual(lib_urls.count("https://fond.kitchen/glossary/gluten-window-test/"), 1)

    def test_internal_mode_skips_web_fetch_status(self):
        evidence = {
            "evidence_mode": "internal",
            "library": [
                {
                    "id": "c1",
                    "claim": "Salt slows yeast.",
                    "source": {"type": "article", "reference": "https://example.com/salt"},
                    "similarity": 0.7,
                }
            ],
            "discovered": [
                {
                    "url": "https://example.com/salt",
                    "discovered_via": "library",
                    "rank": 0.7,
                    "reason": "library",
                }
            ],
            "pages": [],
        }
        sources = build_source_list(evidence, [])
        self.assertEqual(sources[0]["kind"], "library")
        # duplicate library URL from discovered is skipped (already seen)
        self.assertEqual(len(sources), 1)

    def test_sourcewise_tag(self):
        truth = {
            "verdict": "support",
            "citations": [{"title": "x", "url": "https://example.com/a"}],
            "sources": [
                {
                    "kind": "search",
                    "url": "https://example.com/a",
                    "cited": True,
                    "fetch_status": "fetched_ok",
                }
            ],
        }
        self.assertTrue(is_sourcewise_supported(truth))
        card = apply_sourcewise_tag({"id": "c1", "tags": ["other"]}, truth)
        self.assertIn(SOURCEWISE_TAG, card["tags"])
        self.assertIn("other", card["tags"])

        cleared = apply_sourcewise_tag(card, {"verdict": "conflict", "citations": [], "sources": []})
        self.assertNotIn(SOURCEWISE_TAG, cleared.get("tags") or [])


if __name__ == "__main__":
    unittest.main()
