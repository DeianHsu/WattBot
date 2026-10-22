"""确定性数值审计的回归用例。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wattbot.checks import audit_numeric_answer, normalize_categorical_answer


class NumericAuditTests(unittest.TestCase):
    def test_simple_multiplication_mismatch_is_corrected(self):
        self.assertEqual(
            audit_numeric_answer(
                "total tokens",
                "1920000000",
                "Per request = 128 + 512 = 640 tokens. Total = 640 × 3000 = 1,920,000,000 tokens.",
                "tokens",
            ),
            "1920000",
        )

    def test_embodied_carbon_multiplication_mismatch_is_corrected(self):
        self.assertEqual(
            audit_numeric_answer(
                "total embodied carbon",
                "29632",
                "Multiplying 463 kg/GPU by 8 GPUs gives 3704 kg; 463 × 8 = 3704 kg.",
                "kg",
            ),
            "3704",
        )

    def test_explicit_answer_value_statement_wins(self):
        self.assertEqual(
            audit_numeric_answer(
                "daily water",
                "59100000",
                "294,000 kWh × 0.18 L/kWh = 52,920 L. So answer_value 52900.",
                "liters",
            ),
            "52900",
        )
        self.assertEqual(
            audit_numeric_answer(
                "total MWh",
                "36729.6",
                "The answer_value should be 60609.6, not 36729.6.",
                "MWh",
            ),
            "60609.6",
        )

    def test_percent_is_converted_to_multiplier(self):
        self.assertEqual(
            audit_numeric_answer(
                "how many times more",
                "640",
                "The source states up to 640 percent more, so 640 percent equals 640 times.",
                "times",
            ),
            "6.4",
        )

    def test_correct_values_and_irrelevant_equations_are_not_changed(self):
        self.assertIsNone(
            audit_numeric_answer(
                "total tokens", "1920000", "Total = 640 × 3000 = 1,920,000 tokens.", "tokens"
            )
        )
        self.assertIsNone(
            audit_numeric_answer(
                "daily water",
                "52920",
                "Daily energy = 700,000,000 × 0.42 = 294,000,000 Wh.",
                "liters",
            )
        )
        self.assertIsNone(
            audit_numeric_answer(
                "bottles per completion",
                "(10, 50)",
                "The source states 10-50 medium-length responses per bottle.",
                "500 mL bottles",
            )
        )


class CategoricalShorteningTests(unittest.TestCase):
    def test_trailing_clauses_are_removed(self):
        self.assertEqual(
            normalize_categorical_answer(
                "reducing the scale of its initially proposed development",
                "what is it considering doing to its initially proposed development?",
            ),
            "reducing the scale",
        )
        self.assertEqual(
            normalize_categorical_answer(
                "increased water consumption required for cooling",
                "name one environmental cost",
            ),
            "increased water consumption",
        )

    def test_word_limit_removes_acronym_or_low_information_modifier(self):
        self.assertEqual(
            normalize_categorical_answer(
                "Power Distribution Unit (PDU)",
                "Answer with a noun phrase of at most three words.",
            ),
            "Power Distribution Unit",
        )
        self.assertEqual(
            normalize_categorical_answer(
                "data center energy efficiency",
                "What does it measure? Answer with a noun phrase of at most three words.",
            ),
            "data center efficiency",
        )

    def test_variant_and_entity_suffixes_are_preserved_without_word_limit(self):
        for value, question in (
            ("o4-mini (high)", "Which model variant?"),
            ("Backblaze (B2)", "Which storage service?"),
            ("Compute Time Calibration Function (CTCF)", "What is the full name?"),
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_categorical_answer(value, question))

    def test_numeric_values_are_not_categorical_shortened(self):
        self.assertIsNone(normalize_categorical_answer("1234", "Answer with at most three words."))


if __name__ == "__main__":
    unittest.main()
