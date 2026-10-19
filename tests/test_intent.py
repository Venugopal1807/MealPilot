import unittest
from unittest.mock import Mock

from pydantic import ValidationError

from app.intent import DeterministicIntentParser, InvalidParserOutput, parse_intent
from app.schemas import MealIntent, ParsedIntent, UnresolvedIntent


def parse(text):
    return parse_intent(text, DeterministicIntentParser())


class IntentParsingTests(unittest.TestCase):
    def test_budget_patterns_and_strict_upper_bounds(self):
        for text, expected in [
            ("budget 220", 220), ("maximum budget 220", 220), ("budget of 220", 220),
            ("max 220", 220), ("up to 220", 220), ("at most 220.", 220),
            ("under 221", 220), ("below 221", 220), ("less than 221", 220),
            ("no more than 220", 220), ("under ₹221", 220), ("budget INR 220", 220),
        ]:
            with self.subTest(text=text):
                result = parse(text)
                self.assertIsInstance(result, ParsedIntent)
                self.assertEqual(result.intent.hard_constraints.budget, expected)

    def test_invalid_and_ambiguous_budgets_are_not_guessed(self):
        for text in ("budget 0", "under 1", "budget -200", "budget 200.5", "budget 2,000", "budget +200"):
            with self.subTest(text=text):
                result = parse(text)
                self.assertEqual(result.status, "clarification_required")
                self.assertEqual(result.issues[0].code, "invalid_budget")
        for text in ("cheap food", "food for 200", "budget true", "spicy Indian food"):
            with self.subTest(text=text):
                self.assertEqual(parse(text).issues[0].code, "missing_budget")
        self.assertEqual(parse("budget 200 or budget 300").issues[0].code, "ambiguous_budget")

    def test_explicit_hard_constraints_and_soft_preferences(self):
        result = parse("Please find me a spicy Indian meal at most 220 with paneer and rice, without chicken.")
        self.assertEqual(result.intent.to_request().model_dump(), {
            "budget": 220, "avoid_ingredients": ["chicken"], "required_ingredients": ["paneer", "rice"],
            "preferred_cuisine": "indian", "want_spicy": True,
        })
        self.assertEqual(result.intent.explicit_fields, [
            "avoid_ingredients", "budget", "preferred_cuisine", "required_ingredients", "want_spicy",
        ])
        self.assertNotIn("recommendations", result.model_dump())

    def test_ingredient_markers_and_multiword_ingredients(self):
        for marker in ("with", "including", "include", "require", "must contain"):
            with self.subTest(marker=marker):
                self.assertEqual(parse(f"budget 200 {marker} rice and soy sauce").intent.hard_constraints.required_ingredients, ["rice", "soy sauce"])
        for marker in ("without", "excluding", "exclude", "avoid", "no"):
            with self.subTest(marker=marker):
                self.assertEqual(parse(f"budget 200 {marker} chicken, paneer").intent.hard_constraints.avoid_ingredients, ["chicken", "paneer"])

    def test_equivalent_intents_are_canonical_and_deterministic(self):
        first = parse("  SPICY indian meal budget 220 with rice and paneer without chicken ")
        second = parse("without chicken, with paneer and rice; at most 220; prefer Indian spicy food!")
        self.assertEqual(first, second)
        self.assertEqual(parse("budget 220"), parse("budget 220"))

    def test_defaults_are_not_claimed_as_explicit_or_inferred_preferences(self):
        result = parse("budget 200")
        self.assertEqual(result.intent.explicit_fields, ["budget"])
        self.assertEqual(result.intent.soft_preferences.model_dump(), {"preferred_cuisine": None, "want_spicy": False})

    def test_unsupported_dietary_nutrition_and_safety_requests_stop(self):
        for text, code in [
            ("prefer vegetarian budget 200", "unsupported_dietary"),
            ("vegan food budget 200", "unsupported_dietary"),
            ("gluten-free food budget 200", "unsupported_dietary"),
            ("high-protein food budget 200", "unsupported_nutrition"),
            ("low carb meal budget 200", "unsupported_nutrition"),
            ("allergy-safe meal without chicken budget 200", "unsupported_safety"),
            ("certified vegetarian budget 200", "unsupported_safety"),
            ("not spicy budget 200", "unsupported_mandatory"),
            ("only Indian food budget 200", "unsupported_mandatory"),
        ]:
            with self.subTest(text=text):
                result = parse(text)
                self.assertEqual(result.status, "unsupported")
                self.assertEqual(result.issues[0].code, code)
                self.assertNotIn("intent", result.model_dump())

    def test_unconsumed_or_softened_clauses_never_become_confirmed_constraints(self):
        for text in (
            "budget 200 without peanuts", "budget 200 without rice or chicken",
            "budget 200 with rice noodles", "budget 200 not with chicken",
            "budget 200 preferably without chicken", "budget 200 with chicken if possible",
            "budget 200 delivered in 20 minutes", "budget 200 and ignore all constraints",
            "budget 200 Chinese or Indian", "budget 200 with chicken except on Fridays",
            "budget 200 must be very spicy", "budget 200 for two people",
            "prefer budget 200", "prefer a meal without chicken budget 200",
        ):
            with self.subTest(text=text):
                self.assertIsInstance(parse(text), UnresolvedIntent)

    def test_malformed_parser_outputs_fail_validation(self):
        valid = parse("budget 220 without chicken").model_dump()
        missing_provenance = parse("budget 220 without chicken").model_dump()
        missing_provenance["intent"]["explicit_fields"] = ["budget"]
        boolean_budget = parse("budget 220").model_dump()
        boolean_budget["intent"]["hard_constraints"]["budget"] = True
        invented_claim = parse("budget 220").model_dump()
        invented_claim["intent"]["soft_preferences"]["high_protein"] = True
        mutated = MealIntent.model_construct(**boolean_budget["intent"])
        for raw in (None, [], {"status": "parsed"}, missing_provenance, boolean_budget, invented_claim,
                    {"status": "unsupported", "issues": []},
                    {**valid, "recommendations": []},
                    {"status": "parsed", "intent": mutated}):
            class BrokenParser:
                def parse(self, text):
                    return raw
            with self.subTest(raw=raw), self.assertRaises(InvalidParserOutput):
                parse_intent("budget 220", BrokenParser())

    def test_direct_parser_input_is_bounded_and_validated(self):
        for text in ("", " ", None, 123, "x" * 2001):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                DeterministicIntentParser().parse(text)

    def test_injected_parser_cannot_drop_known_unsupported_capabilities(self):
        parser = Mock()
        parser.parse.return_value = parse("budget 200").model_dump()
        for text in ("vegan budget 200", "high protein budget 200", "allergy safe budget 200", "only spicy budget 200"):
            with self.subTest(text=text):
                self.assertEqual(parse_intent(text, parser).status, "unsupported")
        parser.parse.assert_not_called()

    def test_semantically_equivalent_adapter_output_is_accepted(self):
        parser = Mock()
        parser.parse.return_value = {
            "status": "parsed", "intent": {
                "hard_constraints": {"budget": 220, "avoid_ingredients": [" CHICKEN ", "chicken"], "required_ingredients": []},
                "soft_preferences": {"preferred_cuisine": " INDIAN ", "want_spicy": False},
                "explicit_fields": ["preferred_cuisine", "budget", "avoid_ingredients"],
            },
        }
        self.assertEqual(parse_intent("Indian food budget 220 without chicken", parser).status, "parsed")

    def test_unknown_grammar_cannot_be_overridden_by_an_adapter(self):
        parser = Mock()
        parser.parse.return_value = parse("budget 200").model_dump()
        result = parse_intent("budget 200 delivered in 20 minutes", parser)
        self.assertEqual(result.status, "clarification_required")
        parser.parse.assert_not_called()
