import argparse
import importlib.util
import unittest
from pathlib import Path

from research_features import (
    ResearchFeatures,
    describe_use_cases,
    features_from_args,
    profile_features,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_passive_research():
    path = ROOT / "passive-research.py"
    spec = importlib.util.spec_from_file_location("passive_research", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ResearchFeaturesTests(unittest.TestCase):
    def test_profiles(self):
        verify = profile_features("verify")
        self.assertTrue(verify.substantiate)
        self.assertFalse(verify.expand)
        self.assertFalse(verify.seed)
        self.assertFalse(verify.propagate)
        self.assertFalse(verify.evidence_adds)

        grow = profile_features("grow")
        self.assertTrue(grow.expand)
        self.assertTrue(grow.propagate)
        self.assertFalse(grow.seed)

        ask = profile_features("ask")
        self.assertFalse(ask.recommend_gap_draft)

        full = profile_features("full")
        self.assertTrue(full.seed)
        self.assertTrue(full.recommend_gap_draft)

    def test_overrides(self):
        ns = argparse.Namespace(
            profile="full",
            no_expand=True,
            no_seed=True,
            no_propagate=False,
            no_evidence_adds=True,
            no_focused_propagate=False,
            growth_requires_sourcewise=True,
            no_draft=False,
            draft_gaps=False,
        )
        feats = features_from_args(ns)
        self.assertFalse(feats.expand)
        self.assertFalse(feats.seed)
        self.assertFalse(feats.evidence_adds)
        self.assertTrue(feats.require_sourcewise_for_growth)

    def test_allow_growth_sourcewise(self):
        feats = ResearchFeatures(require_sourcewise_for_growth=True)
        self.assertFalse(feats.allow_growth({"verdict": "support", "sourcewise_supported": False}))
        self.assertTrue(feats.allow_growth({"verdict": "support", "sourcewise_supported": True}))

    def test_use_case_catalog(self):
        rows = list(describe_use_cases())
        self.assertGreaterEqual(len(rows), 8)
        ids = {r["id"] for r in rows}
        self.assertIn("verify_external", ids)
        self.assertIn("propagate", ids)

    def test_select_batch_respects_features(self):
        pr = _load_passive_research()
        supported = {
            "id": "supported-parent",
            "claim": "Cold fermentation improves extensibility.",
            "confidence": 0.9,
            "status": "reviewed",
            "style_scope": ["ny_slice"],
            "variables": ["fermentation_time"],
            "corroboration": {"verdict": "support", "score": 0.9},
        }
        phase, batch = pr.select_batch(
            [supported],
            set(),
            0.65,
            5,
            allow_propagate=False,
            features=ResearchFeatures(expand=False, seed=False),
        )
        self.assertEqual(phase, "done")
        self.assertEqual(batch, [])

    def test_ops_from_plan_verify_profile_blocks_growth(self):
        pr = _load_passive_research()
        parent = {
            "id": "cold-open",
            "claim": "Extended cold ferment makes lower-hydration dough easier to open.",
            "variables": ["hydration", "fermentation_time"],
            "style_scope": ["ny_slice"],
            "phase": "bulk_fermentation",
            "relevance_targets": ["pizza_cart_app"],
            "confidence": 0.5,
            "refines": [],
        }
        truth = {
            "verdict": "support",
            "score": 0.8,
            "rationale": "ok",
            "citations": [{"title": "Cold ferment", "url": "https://example.com/cold"}],
            "sourcewise_supported": True,
        }
        plan = {
            "action": "validate",
            "revised_claim": None,
            "why": "ok",
            "adds": [
                {
                    "claim": "Protease activity during cold fermentation increases dough extensibility.",
                    "variables": ["fermentation_time"],
                    "style_scope": ["general"],
                    "phase": ["bulk_fermentation"],
                    "relevance_targets": ["general_reference"],
                }
            ],
            "propagations": [
                {
                    "claim": "On the lab dashboard, record whether cold-retarded NY dough opens cleanly.",
                    "direction": "specialize",
                    "variables": ["hydration"],
                    "style_scope": ["ny_slice"],
                    "phase": ["bulk_fermentation"],
                    "relevance_targets": ["fermentation_lab_dashboard"],
                }
            ],
        }
        feats = profile_features("verify")
        ops = pr.ops_from_plan(parent, truth, plan, [parent], ["cold-open"], features=feats)
        kinds = {op.op for op in ops}
        self.assertIn("validate", kinds)
        self.assertNotIn("add", kinds)
        self.assertNotIn("propagate", kinds)


if __name__ == "__main__":
    unittest.main()
