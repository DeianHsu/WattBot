"""显式约束重写的回归用例。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wattbot import generate


class FormatRewriteTests(unittest.TestCase):
    def test_extractive_shortening_is_accepted(self):
        result = generate.FormattedAnswer(answer_value="data center efficiency")
        with patch.object(generate, "invoke_json", return_value=result):
            shortened = generate.rewrite_answer_value(
                "What does it measure? Answer with a noun phrase of at most three words.",
                "data center energy efficiency",
                "is_blank",
                3,
            )
        self.assertEqual(shortened, "data center efficiency")

    def test_answer_with_new_words_is_rejected(self):
        result = generate.FormattedAnswer(answer_value="data center power efficiency")
        with patch.object(generate, "invoke_json", return_value=result):
            shortened = generate.rewrite_answer_value(
                "What does it measure? Answer with a noun phrase of at most three words.",
                "data center energy efficiency",
                "is_blank",
                3,
            )
        self.assertIsNone(shortened)

    def test_over_limit_answer_is_not_rewritten_without_explicit_limit(self):
        self.assertIsNone(generate.normalize_categorical_answer(
            "reducing the scale of its initially proposed development",
            "what is it considering doing?",
        ))


if __name__ == "__main__":
    unittest.main()
