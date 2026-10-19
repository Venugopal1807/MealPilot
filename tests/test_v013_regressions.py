"""Counterexamples first run against v0.1.2; see docs/v0.1.3-verification.md."""

import unittest
from unittest.mock import Mock, patch

import evaluation
from app.candidates import InvalidCandidateData, normalize_candidates
from app.intent import DeterministicIntentParser, InvalidParserOutput, parse_intent
from app.main import get_recommendations, get_text_recommendations
from app.schemas import UnresolvedIntent
from test_candidates import candidate


STRUCTURED_CASE = {
    "name": "response_integrity", "request": {"budget": 200},
    "expected": [["Vegetable Fried Rice", 0], ["Paneer Rice Bowl", 0]],
}


class DemonstratedRegressionTests(unittest.TestCase):
    def test_exclusion_scope_cannot_turn_into_a_positive_spicy_preference(self):
        for text in ("without chicken and spicy budget 220", "no chicken and Indian budget 220",
                     "without chicken, and spicy budget 220", "without chicken, Indian budget 220"):
            with self.subTest(text=text):
                result = parse_intent(text, DeterministicIntentParser())
                self.assertIsInstance(result, UnresolvedIntent)
                self.assertEqual(result.status, "clarification_required")

    def test_schema_valid_parser_cannot_drop_explicit_exclusions(self):
        parser = Mock()
        parser.parse.return_value = DeterministicIntentParser().parse("budget 300")
        with self.assertRaises(InvalidParserOutput):
            parse_intent("budget 300 without chicken", parser)

    def test_known_incomplete_ingredient_evidence_is_rejected(self):
        for change in ({"ingredients": ["rice", "unknown"]}, {"ingredients_complete": False}):
            with self.subTest(change=change), self.assertRaises(InvalidCandidateData):
                normalize_candidates([candidate(**change)])

    def test_evaluator_rejects_a_fabricated_catalog_price(self):
        def corrupted(request, provider):
            body = get_recommendations(request, provider)
            body["recommendations"][0]["price"] = 1
            return body

        with patch("evaluation.get_recommendations", side_effect=corrupted):
            result = evaluation.evaluate_scenario(STRUCTURED_CASE)
        self.assertFalse(result["passed"])

    def test_evaluator_rejects_an_inconsistent_response_count(self):
        def corrupted(request, provider):
            body = get_recommendations(request, provider)
            body["count"] = 999
            return body

        with patch("evaluation.get_recommendations", side_effect=corrupted):
            result = evaluation.evaluate_scenario(STRUCTURED_CASE)
        self.assertFalse(result["passed"])

    def test_evaluator_detects_provider_access_for_unresolved_intent(self):
        case = {
            "name": "unresolved_provider_access", "mode": "text",
            "request": {"text": "vegan budget 200"},
            "expected_outcome": "unsupported", "expected_issue": "unsupported_dietary", "expected": [],
        }

        def unsafe(request, parser, provider):
            provider.search_meals()
            return get_text_recommendations(request, parser, provider)

        with patch("evaluation.get_text_recommendations", side_effect=unsafe):
            result = evaluation.evaluate_scenario(case)
        self.assertFalse(result["passed"])
