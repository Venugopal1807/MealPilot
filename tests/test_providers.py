import json
import unittest
from unittest.mock import patch

from app import mock_mcp
from app.providers import MockMealProvider, ProviderTimeout, ProviderUnavailable, get_meal_provider


class MockProviderTests(unittest.TestCase):
    def test_default_provider_preserves_existing_mock_catalog(self):
        provider = get_meal_provider()
        self.assertIsInstance(provider, MockMealProvider)
        self.assertEqual(provider.name, "mock_mcp")
        self.assertEqual(provider.search_meals(), mock_mcp.search_restaurants())

    def test_file_and_decode_failures_are_provider_failures(self):
        errors = [FileNotFoundError("private path"), PermissionError("private path"),
                  json.JSONDecodeError("invalid", "", 0), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch("app.providers.search_restaurants", side_effect=error):
                    with self.assertRaisesRegex(ProviderUnavailable, "Meal catalog is unavailable"):
                        MockMealProvider().search_meals()

    def test_unexpected_programming_errors_are_not_hidden(self):
        with patch("app.providers.search_restaurants", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                MockMealProvider().search_meals()

    def test_timeout_is_classified_without_retry(self):
        with patch("app.providers.search_restaurants", side_effect=TimeoutError("private details")) as search:
            with self.assertRaises(ProviderTimeout):
                MockMealProvider().search_meals()
        self.assertEqual(search.call_count, 1)
