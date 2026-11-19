import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_sources import discover_sources


def _load_passive_research():
    path = ROOT / "passive-research.py"
    spec = importlib.util.spec_from_file_location("passive_research", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pr = _load_passive_research()


class ResearchLoopCoherenceTests(unittest.TestCase):
    def test_search_queries_expand_bake_doubts(self):
        card = {
            "claim": "A Sicilian pan dough baked in a steel pan needs a lower oven temperature.",
            "style_scope": ["sicilian"],
            "variables": ["bake_temp", "bake_time"],
        }
        queries = pr._search_queries(card)
        self.assertGreaterEqual(len(queries), 3)
        self.assertEqual(queries[0], card["claim"])
        self.assertTrue(any("bake" in q.lower() for q in queries))

    def test_report_states_found_and_not_found(self):
        text = pr.format_research_report(
            [
                {
                    "id": "c1",
                    "claim": "Steel pans need lower temp than Neapolitan.",
                    "phase": "substantiate",
                    "verdict": "insufficient",
                    "summary": "off-topic pages",
                    "conclusion": "Claim unresolved; sources were inadequate after escalation.",
                    "found": ["windowpane discussion only"],
                    "not_found": ["pan bake temperature comparison"],
                    "open_doubts": ["steel vs stone heat transfer"],
                    "pages_ok": ["https://example.com/a"],
                    "pages_failed": [],
                    "urls_considered": ["https://example.com/a", "https://example.com/b"],
                    "sources": [
                        {
                            "kind": "library",
                            "id": "neighbor-1",
                            "url": "https://example.com/lib",
                            "similarity": 0.7,
                            "fetch_status": "not_applicable",
                            "cited": False,
                        },
                        {
                            "kind": "search",
                            "url": "https://example.com/a",
                            "discovered_via": "search",
                            "fetch_status": "fetched_ok",
                            "cited": False,
                        },
                    ],
                    "sourcewise_supported": False,
                    "actions": ["recorded unresolved attempt"],
                }
            ],
            session_meta={"actions": 0},
        )
        self.assertIn("Session conclusion", text)
        self.assertIn("pan bake temperature comparison", text)
        self.assertIn("steel vs stone heat transfer", text)
        self.assertIn("Full source list", text)
        self.assertIn("https://example.com/a", text)
        self.assertIn("neighbor-1", text)

    def test_discover_sources_accepts_multiple_queries(self):
        out = discover_sources(
            "claim",
            [],
            extra_urls=["https://example.com/source"],
            use_web=False,
            search_queries=["sicilian bake temperature", "steel pan pizza bake time"],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "https://example.com/source")


if __name__ == "__main__":
    unittest.main()
