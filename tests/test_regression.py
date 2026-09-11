import json
import os
import unittest
from pathlib import Path

from src.evaluate import evaluate
from src.ingest import load_job


ROOT = Path(__file__).resolve().parents[1]
_SAVED_KEYS: dict[str, str] = {}


def setUpModule():
    """Seeded decisions must be deterministic, so no provider may be reachable.

    `load_job` extracts a profile, and with a key present that becomes a live
    API call: the regression would measure the model instead of the rules.
    """
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if name in os.environ:
            _SAVED_KEYS[name] = os.environ.pop(name)


def tearDownModule():
    os.environ.update(_SAVED_KEYS)


class RegressionTests(unittest.TestCase):
    def test_seeded_decisions(self):
        cases = json.loads((ROOT / "regression" / "cases.yaml").read_text(encoding="utf-8"))["cases"]
        for case in cases:
            with self.subTest(case=case["id"]):
                job = load_job(str(ROOT / case["job_file"]), None, case["track"], str(ROOT / case["meta_file"]))
                result = evaluate(job)
                self.assertEqual(case["expected"], result.decision)
                if case["id"] == "senior-react-typescript":
                    self.assertIn("$100/h", result.suggested_compensation)
                    self.assertEqual("frontend-heavy", result.cv_positioning)
                    self.assertIn("React is newer for me", result.proposal_positioning_angle[0])
                if case["id"] == "shopify-public-app":
                    self.assertEqual(2, len(result.hard_blockers))
                if case["id"] == "creator-messaging-audit":
                    self.assertEqual("product/software", result.cv_positioning)
                    self.assertTrue(result.actual_work_shape[0].startswith("~62% audit"))

    def test_empty_input_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "empty"):
            load_job(None, "  ", "A")


if __name__ == "__main__":
    unittest.main()
