import unittest

from research_policy import ClaimLifecyclePolicy


class ClaimLifecyclePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ClaimLifecyclePolicy()
        self.support = {"verdict": "support", "score": 0.9}
        self.parent = {
            "id": "parent", "claim": "Cold fermentation improves extensibility",
            "style_scope": ["ny_slice"], "phase": ["bulk"],
            "variables": ["fermentation_time"], "relevance_targets": ["general_reference"],
        }

    def test_creation_uses_truth_score_and_draft_confidence_separately(self):
        draft = {"id": "new", "claim": "Cold fermentation changes extensibility",
                 "style_scope": ["general"], "confidence": 0.4, "status": "draft"}
        self.assertTrue(self.policy.allow_create(draft, self.support).allowed)
        self.assertFalse(self.policy.allow_create(draft, {"verdict": "support", "score": 0.2}).allowed)
        too_confident = dict(draft, confidence=0.8)
        self.assertFalse(self.policy.allow_create(too_confident, self.support).allowed)

    def test_create_rejects_duplicate_and_unscoped_claim(self):
        draft = {"claim": "same claim", "confidence": 0.3, "phase": ["bulk"]}
        self.assertFalse(self.policy.allow_create(draft, self.support, {"same claim"}).allowed)
        unscoped = {"claim": "novel", "confidence": 0.3}
        self.assertFalse(self.policy.allow_create(unscoped, self.support).allowed)

    def test_modify_requires_supported_nonidentical_text(self):
        self.assertTrue(self.policy.allow_modify("old wording", "new wording", self.support).allowed)
        self.assertFalse(self.policy.allow_modify("same", " SAME ", self.support).allowed)
        self.assertFalse(self.policy.allow_modify("old", "new", {"verdict": "conflict", "score": 0.9}).allowed)

    def test_specialization_requires_scope_delta_and_support(self):
        child = {"claim": "A shaping-specific claim", "phase": ["shaping"], "confidence": 0.4}
        self.assertTrue(self.policy.allow_propagate(self.parent, child, "specialize", self.support).allowed)
        no_delta = dict(child, phase=["bulk"])
        self.assertFalse(self.policy.allow_propagate(self.parent, no_delta, "specialize", self.support).allowed)
        self.assertFalse(self.policy.allow_propagate(self.parent, child, "specialize", {"verdict": "insufficient"}).allowed)

    def test_archive_requires_unrepairable_conflict(self):
        conflict = {"verdict": "conflict", "score": 0.8}
        self.assertTrue(self.policy.allow_archive(conflict).allowed)
        self.assertFalse(self.policy.allow_archive(conflict, repairable=True).allowed)
        self.assertFalse(self.policy.allow_archive({"verdict": "conflict", "score": 0.1}).allowed)

    def test_verify_does_not_confuse_draft_confidence_with_evidence_score(self):
        draft = {"id": "new", "claim": "A supported draft", "confidence": 0.4, "status": "draft"}
        self.assertTrue(self.policy.verify_operation({"op": "add", "card": draft}).allowed)
        invalid = dict(draft, confidence=0.8)
        self.assertFalse(self.policy.verify_operation({"op": "add", "card": invalid}).allowed)


if __name__ == "__main__":
    unittest.main()
