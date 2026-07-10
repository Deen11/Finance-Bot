import unittest

from transaction_rules import (
    canonical_category,
    extract_transaction_amount,
    format_parse_prompt,
    infer_category,
    parse_positive_amount,
    resolve_category,
)


class TransactionRulesTests(unittest.TestCase):
    def test_currency_amount_wins_over_an_earlier_date_number(self):
        self.assertEqual(
            extract_transaction_amount("on 3 July spent $12 on lunch"),
            12.0,
        )

    def test_common_sgd_amount_formats(self):
        self.assertEqual(extract_transaction_amount("paid SGD 1,234.56"), 1234.56)
        self.assertEqual(extract_transaction_amount("lunch S$12.50"), 12.5)
        self.assertEqual(extract_transaction_amount("water 1.50"), 1.5)

    def test_invalid_amounts_are_rejected(self):
        invalid_values = (0, -1, "0", "-2.50", "NaN", "Infinity", float("nan"), float("inf"), True)
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_positive_amount(value))

        self.assertIsNone(extract_transaction_amount("spent $-5 on lunch"))

    def test_valid_model_amount_is_normalized(self):
        self.assertEqual(parse_positive_amount("$1,200.50"), 1200.5)

    def test_screenshot_beverages_are_food(self):
        self.assertEqual(infer_category("1.50 for water"), "Food")
        self.assertEqual(infer_category("$3.50 vitamin c water"), "Food")

    def test_explicit_category_is_respected(self):
        self.assertEqual(infer_category("log 1.50 under food"), "Food")
        self.assertEqual(infer_category("$20 category shopping"), "Shopping")

    def test_non_food_water_items_are_not_food(self):
        self.assertEqual(infer_category("paid $40 water bill"), "Other")
        self.assertEqual(infer_category("bought a water bottle $12"), "Shopping")

    def test_other_common_categories_still_work(self):
        self.assertEqual(infer_category("paid mom $800"), "Family")
        self.assertEqual(infer_category("took MRT $2"), "Transport")
        self.assertEqual(infer_category("salary $3000", "income"), "Income")

    def test_unknown_model_category_is_rejected(self):
        self.assertEqual(canonical_category("Food & Drink"), "Other")
        self.assertEqual(canonical_category("food"), "Food")

    def test_uncertain_model_result_is_repaired_from_message(self):
        self.assertEqual(
            resolve_category("Other", source_text="$3.50 vitamin c water"),
            "Food",
        )

    def test_explicit_other_overrides_model(self):
        self.assertEqual(
            resolve_category("Food", source_text="$3 water under other"),
            "Other",
        )

    def test_prompt_is_formatted_once_with_literal_json_braces(self):
        template = 'Today: {today}\nMessage: "{message}"\n{{"amount": 1}}'
        prompt = format_parse_prompt(template, today="2026-07-10", message="1.50 for water")
        self.assertIn('Message: "1.50 for water"', prompt)
        self.assertIn('{"amount": 1}', prompt)


if __name__ == "__main__":
    unittest.main()
