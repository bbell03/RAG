"""Unit tests for `instance_analytics.py` and `admin_server.py`."""

from __future__ import annotations

import os
import tempfile
import unittest

from admin_server import dispatch_api_request
from instance_analytics import (
    append_trial_log,
    build_dough_instances,
    compare_trial_instances,
    ensure_store,
    export_app_bundle,
    import_app_bundle,
    stage_empirical_claim,
)


class TestInstanceAnalyticsAndBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.profiles_path = os.path.join(self.tmp.name, "profiles.jsonl")
        self.logs_path = os.path.join(self.tmp.name, "logs.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_seed_instances_journey_and_convergence(self) -> None:
        profiles, logs = ensure_store(self.profiles_path, self.logs_path)
        self.assertGreaterEqual(len(profiles), 3)
        self.assertGreaterEqual(len(logs), 7)

        instances = build_dough_instances(profiles, logs)
        neap = next(i for i in instances if i["shortCode"] == "#5678")
        self.assertEqual(len(neap["trials"]), 4)
        self.assertGreater(neap["improvementDelta"], 0)
        self.assertEqual(set(neap["trials"][0]["phases"].keys()), {"mixing", "resting", "fermentation"})
        self.assertGreater(neap["ballComposition"]["totalDoughGrams"], 800)
        self.assertGreaterEqual(len(neap["taskOrders"]), 5)
        self.assertGreaterEqual(len(neap["matchedCards"]), 1)

    def test_scientific_comparison_single_variable_vs_confounded(self) -> None:
        profiles, logs = ensure_store(self.profiles_path, self.logs_path)
        instances = build_dough_instances(profiles, logs)
        neap = next(i for i in instances if i["shortCode"] == "#5678")
        t1, t2, t3, t4 = neap["trials"]

        # T1 (60% hydr) -> T2 (64% hydr) holds 68°F bulk, 70°F rest, 00 flour constant.
        clean = compare_trial_instances(t1, t2)
        self.assertEqual(clean["scientificRigor"], "single-variable")
        self.assertEqual(len(clean["isolatedVariables"]), 1)
        self.assertEqual(clean["isolatedVariables"][0]["definition"]["key"], "hydrationPct")
        self.assertEqual(clean["outcomeShift"], "improved")
        self.assertIsNotNone(clean["draftedClaim"])
        self.assertIn("hydration", clean["draftedClaim"]["variables"])
        self.assertLessEqual(clean["draftedClaim"]["confidence"], 0.45)

        # T2 -> T3 shifted hydration + ferment temp + rest temp + ambient + humidity -> confounded.
        confounded = compare_trial_instances(t2, t3)
        self.assertEqual(confounded["scientificRigor"], "confounded")
        self.assertGreaterEqual(len(confounded["isolatedVariables"]), 3)
        self.assertIsNone(confounded["draftedClaim"])

        # T2 -> T4 reverted to 64% & 68°F and isolated 20% preferment share.
        poolish_cmp = compare_trial_instances(t2, t4)
        self.assertEqual(poolish_cmp["scientificRigor"], "single-variable")
        self.assertEqual(poolish_cmp["isolatedVariables"][0]["definition"]["key"], "prefermentShare")

    def test_api_dispatch_compose_compare_and_bridge_roundtrip(self) -> None:
        status, dash = dispatch_api_request(
            "GET",
            "/api/dashboard",
            {},
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(dash["instances"]), 3)

        # Compose a new single-variable trial via POST /api/logs
        status, created_resp = dispatch_api_request(
            "POST",
            "/api/logs",
            {},
            body={
                "profileId": "prof-neapolitan-5678",
                "title": "T5 · Isolate 66% Hydration",
                "hydration": 66,
                "flour": "00",
                "prefermentType": "poolish",
                "prefermentFlourPercent": 20,
                "phaseTempF": {"rest": 70, "ferment": 68, "proof": 72},
                "rise": 79,
                "outcomeRating": "good",
                "result": "Extra light rim, still clean peel launch",
                "experimentMeta": {
                    "hypothesis": "+2% hydration on 20% poolish base",
                    "isolatedVariables": ["hydrationPct"],
                    "objectRef": "#5678 · Ball E1",
                },
            },
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self.assertEqual(status, 201)
        new_id = created_resp["created"]["id"]

        status, cmp_resp = dispatch_api_request(
            "GET",
            "/api/compare",
            {"control": ["log-5678-t4"], "variant": [new_id]},
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self.assertEqual(status, 200)
        self.assertEqual(cmp_resp["scientificRigor"], "single-variable")

        # Export & re-import into a fresh store
        exported = export_app_bundle(self.profiles_path, self.logs_path)
        self.assertEqual(exported["schema"], "dough-instance-bridge/v1")

        p2 = os.path.join(self.tmp.name, "p2.jsonl")
        l2 = os.path.join(self.tmp.name, "l2.jsonl")
        stats = import_app_bundle(exported, profiles_path=p2, logs_path=l2)
        self.assertEqual(stats["logsMerged"], len(exported["logs"]))

    def test_stage_empirical_claim_caps_confidence(self) -> None:
        cand_Backup = None
        if os.path.exists("candidates.jsonl"):
            with open("candidates.jsonl", "r", encoding="utf-8") as fh:
                cand_Backup = fh.read()
        try:
            res = stage_empirical_claim(
                {
                    "claim": "For Neapolitan dough, raising hydration from 60% to 64% at 68°F bulk improves extensibility.",
                    "variables": ["hydration", "extensibility"],
                    "style_scope": ["neapolitan"],
                    "phase": ["mixing"],
                    "confidence": 0.9,
                    "reference": "experiment:#5678:T1->T2",
                },
                commit=False,
            )
            self.assertLessEqual(res["card"]["confidence"], 0.45)
            self.assertEqual(res["card"]["source"]["type"], "experiment")
        finally:
            if cand_Backup is not None:
                with open("candidates.jsonl", "w", encoding="utf-8") as fh:
                    fh.write(cand_Backup)
            elif os.path.exists("candidates.jsonl"):
                os.remove("candidates.jsonl")


if __name__ == "__main__":
    unittest.main()
