"""Audited HSBC IFRS 4 / IFRS 17 opening ordinary-equity evidence."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from review_hsbc_restatement import OUTPUT, RAW, build_review  # noqa: E402


class HsbcRestatementReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.review = json.loads(OUTPUT.read_text(encoding="utf-8"))

    def test_restated_opening_equity_is_derived_from_reported_parent_equity_scope(self) -> None:
        review = self.review
        original = review["original_ifrs4"]
        restated = review["restated_ifrs17"]
        bridge = review["transition_bridge"]

        self.assertEqual(original["value"], "175836")
        self.assertEqual(restated["value"], "166601")
        self.assertEqual(restated["metric"], "equity_ordinary")
        self.assertEqual(restated["period_end"], "2021-12-31")
        self.assertEqual(bridge["ifrs4_total_shareholders_equity"], "198250")
        self.assertEqual(bridge["ifrs17_total_shareholders_equity"], "189015")
        self.assertEqual(bridge["other_equity_instruments_under_ifrs4"], "22414")
        self.assertEqual(bridge["other_equity_instruments_under_ifrs17"], "22414")
        self.assertEqual(198250 - 22414, int(original["value"]))
        self.assertEqual(189015 - 22414, int(restated["value"]))

    def test_reported_transition_components_reconcile_without_dropping_the_ifrs4_basis(
        self,
    ) -> None:
        review = self.review
        bridge = review["transition_bridge"]
        components = bridge["total_shareholders_equity_adjustment_components"]

        self.assertEqual(sum(row["value"] for row in components), -9235)
        self.assertEqual(198250 - 9235, 189015)
        self.assertEqual(
            int(bridge["ifrs17_ordinary_equity"]) - int(bridge["ifrs4_ordinary_equity"]),
            -9235,
        )
        self.assertIn("IFRS 17 transition impact", bridge["transition_source_text"])
        self.assertIn("189,015", bridge["transition_table_excerpt"])
        self.assertIn("(9,235)", bridge["transition_table_excerpt"])
        self.assertEqual(review["original_ifrs4"]["period_end"], "2021-12-31")
        self.assertEqual(review["restated_ifrs17"]["period_end"], "2021-12-31")

    def test_original_source_capture_metadata_and_publication_proof_are_frozen(self) -> None:
        review = self.review
        report = review["source_document"]
        index = review["publication_evidence"]["document"]

        self.assertEqual(report["http_status"], 200)
        self.assertEqual(report["bytes"], 7034549)
        self.assertEqual(
            report["sha256"],
            "2c03736c288399da08b665d72616e45b93c99764ec47891d9c2bc8fa5175c6c5",
        )
        self.assertEqual(report["published_on"], "2024-02-21")
        self.assertIn("21 Feb 2024", review["publication_evidence"]["excerpt"])
        self.assertEqual(index["http_status"], 200)
        self.assertRegex(report["raw_path"], r"^hsbc-restatement/")

    @unittest.skipUnless(
        (RAW / "hsbc-annual-report-2023.pdf").exists()
        and (RAW / "official-report-index-2023.html").exists(),
        "raw source captures are not included in a clean checkout",
    )
    def test_offline_reviewer_reproduces_the_frozen_record_from_captured_sources(self) -> None:
        self.assertEqual(build_review(), self.review)


if __name__ == "__main__":
    unittest.main()
