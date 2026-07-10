import unittest

from transaction_rules import (
    canonical_category,
    format_parse_prompt,
    infer_category,
    resolve_category,
)


class TransactionRulesTests(unittest.TestCase):
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
