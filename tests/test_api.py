import json
from pathlib import Path
import tempfile
import unittest
from uuid import UUID
from unittest.mock import patch

from app.main import app
from app.intent import ParserUnavailable, get_intent_parser
from app.providers import get_meal_provider


async def asgi_request(payload=None, *, method="POST", path="/recommend", raw_body=None):
    """Exercise the ASGI application without an HTTP client or network socket."""
    messages = []

    async def receive():
        body = json.dumps(payload).encode() if raw_body is None else raw_body
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1), "server": ("test", 80),
    }, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    headers = next(dict(message["headers"]) for message in messages if message["type"] == "http.response.start")
    return status, json.loads(body), headers


async def post_recommend(payload):
    status, body, _ = await asgi_request(payload)
    return status, body


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_mock_response_contract(self):
        status, body = await post_recommend({"budget": 200})
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "mock_mcp")
        self.assertEqual(body["count"], 2)
        self.assertEqual([row["dish"] for row in body["recommendations"]], ["Vegetable Fried Rice", "Paneer Rice Bowl"])

    async def test_provider_dependency_can_be_replaced_without_domain_changes(self):
        class EmptyProvider:
            name = "local_test"

            def search_meals(self):
                return []

        with patch.dict(app.dependency_overrides, {get_meal_provider: EmptyProvider}):
            status, body = await post_recommend({"budget": 200})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"count": 0, "recommendations": [], "source": "local_test"})

    async def test_unavailable_catalog_is_503_without_internal_details(self):
        with patch("app.providers.search_restaurants", side_effect=FileNotFoundError("private path")):
            status, body = await post_recommend({"budget": 200})
        self.assertEqual(status, 503)
        self.assertEqual(body, {"detail": "Meal recommendations are temporarily unavailable"})

    async def test_invalid_budget_is_rejected_before_provider_access(self):
        with patch("app.providers.search_restaurants") as search:
            status, _ = await post_recommend({"budget": 0})
        self.assertEqual(status, 422)
        search.assert_not_called()

    async def test_invalid_inputs_are_not_silently_coerced_or_ignored(self):
        requests = [
            {}, {"budget": None}, {"budget": -1}, {"budget": True},
            {"budget": "200"}, {"budget": 200.5}, {"budget": 200.0},
            {"budget": 200, "avoid_ingredients": "rice"},
            {"budget": 200, "avoid_ingredients": [1]},
            {"budget": 200, "avoid_ingredients": [" "]},
            {"budget": 200, "required_ingredients": [""]},
            {"budget": 200, "required_ingredients": None},
            {"budget": 200, "want_spicy": "false"},
            {"budget": 200, "preferred_cuisine": 123},
            {"budget": 200, "dietary_restrictions": ["vegan"]},
        ]
        for payload in requests:
            with self.subTest(payload=payload):
                with patch("app.providers.search_restaurants") as search:
                    status, _ = await post_recommend(payload)
                self.assertEqual(status, 422)
                search.assert_not_called()

    async def test_malformed_json_is_422(self):
        with patch("app.providers.search_restaurants") as search:
            status, _, _ = await asgi_request(raw_body=b"{bad json")
        self.assertEqual(status, 422)
        search.assert_not_called()

    async def test_empty_matches_are_a_successful_response(self):
        status, body = await post_recommend({"budget": 100})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"count": 0, "recommendations": [], "source": "mock_mcp"})

    async def test_required_ingredients_are_applied_by_the_api(self):
        status, body = await post_recommend({"budget": 200, "required_ingredients": ["PANEER"]})
        self.assertEqual(status, 200)
        self.assertEqual([row["dish"] for row in body["recommendations"]], ["Paneer Rice Bowl"])

    async def test_malformed_provider_catalogs_return_502(self):
        for catalog in (None, {}, [None], [{"price": 100}], [{"price": -1}]):
            with self.subTest(catalog=catalog):
                with patch("app.providers.search_restaurants", return_value=catalog):
                    status, body = await post_recommend({"budget": 200})
                self.assertEqual(status, 502)
                self.assertEqual(body, {"detail": "Meal provider returned an invalid catalog"})

    async def test_provider_timeout_returns_504_without_retry(self):
        with patch("app.providers.search_restaurants", side_effect=TimeoutError("synthetic private details")) as search:
            status, body = await post_recommend({"budget": 200})
        self.assertEqual(status, 504)
        self.assertEqual(body, {"detail": "Meal provider timed out"})
        self.assertEqual(search.call_count, 1)

    async def test_unexpected_error_is_sanitized_and_correlated(self):
        secret = "synthetic-secret-do-not-log"
        with patch("app.providers.search_restaurants", side_effect=RuntimeError(secret)):
            with self.assertLogs("mealpilot", level="INFO") as logs:
                status, body, headers = await asgi_request({"budget": 200})
        self.assertEqual(status, 500)
        self.assertEqual(body, {"detail": "Internal server error"})
        request_id = headers[b"x-request-id"].decode()
        UUID(request_id)
        self.assertNotIn(secret, " ".join(logs.output))
        events = [json.loads(record.getMessage()) for record in logs.records]
        self.assertTrue(all(event["request_id"] == request_id for event in events))
        self.assertEqual(events[0]["event"], "unexpected_error")

    async def test_health_and_correlation_on_success_and_validation_errors(self):
        status, body, headers = await asgi_request(method="GET", path="/health")
        self.assertEqual((status, body), (200, {"status": "ok", "project": "MealPilot AI"}))
        first_id = UUID(headers[b"x-request-id"].decode())
        status, _, headers = await asgi_request({"budget": 0})
        self.assertEqual(status, 422)
        self.assertNotEqual(first_id, UUID(headers[b"x-request-id"].decode()))

    async def test_parse_only_endpoint_returns_intent_without_provider_access(self):
        with patch("app.providers.search_restaurants") as search:
            status, body, _ = await asgi_request({"text": "budget 220 without chicken"}, path="/intent")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "parsed")
        self.assertEqual(body["intent"]["hard_constraints"]["avoid_ingredients"], ["chicken"])
        self.assertNotIn("result", body)
        search.assert_not_called()

    async def test_text_pipeline_matches_existing_structured_recommendations(self):
        status, text, _ = await asgi_request({"text": "spicy Indian meal at most 220 with paneer without chicken"}, path="/recommend/text")
        structured_status, structured = await post_recommend({
            "budget": 220, "required_ingredients": ["paneer"], "avoid_ingredients": ["chicken"],
            "preferred_cuisine": "indian", "want_spicy": True,
        })
        self.assertEqual((status, structured_status), (200, 200))
        self.assertEqual(text["result"], structured)
        self.assertEqual(text["result"]["source"], "mock_mcp")

    async def test_unresolved_text_never_searches_or_recommends(self):
        for text, outcome in (
            ("vegetarian food budget 200", "unsupported"),
            ("high protein food budget 200", "unsupported"),
            ("allergy-safe budget 200", "unsupported"),
            ("spicy food", "clarification_required"),
            ("budget 200 without peanuts", "clarification_required"),
        ):
            with self.subTest(text=text), patch("app.providers.search_restaurants") as search:
                status, body, _ = await asgi_request({"text": text}, path="/recommend/text")
                self.assertEqual((status, body["status"]), (200, outcome))
                self.assertNotIn("intent", body)
                self.assertNotIn("result", body)
                search.assert_not_called()

    async def test_malformed_text_request_is_422_before_parser_or_provider(self):
        for payload in ({}, {"text": ""}, {"text": " "}, {"text": 200}, {"text": None},
                        {"text": "x" * 2001}, {"text": "budget 200", "budget": 300}):
            with self.subTest(payload=payload), patch("app.intent.DeterministicIntentParser.parse") as parser:
                with patch("app.providers.search_restaurants") as search:
                    status, _, _ = await asgi_request(payload, path="/recommend/text")
                self.assertEqual(status, 422)
                parser.assert_not_called()
                search.assert_not_called()

    async def test_parser_errors_are_sanitized_and_do_not_search(self):
        for output, error, expected in (
            ({"status": "parsed"}, None, 502),
            (None, ParserUnavailable("synthetic-private-detail"), 503),
            (None, TimeoutError("synthetic-private-detail"), 504),
        ):
            class FailingParser:
                def parse(self, text):
                    if error:
                        raise error
                    return output
            with self.subTest(expected=expected), patch.dict(app.dependency_overrides, {get_intent_parser: FailingParser}):
                with patch("app.providers.search_restaurants") as search:
                    status, body, _ = await asgi_request({"text": "budget 220"}, path="/recommend/text")
                self.assertEqual(status, expected)
                self.assertNotIn("synthetic-private-detail", json.dumps(body))
                search.assert_not_called()

    async def test_text_provider_failure_uses_existing_error_mapping(self):
        with patch("app.providers.search_restaurants", side_effect=FileNotFoundError("private")):
            status, body, _ = await asgi_request({"text": "budget 220"}, path="/recommend/text")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"detail": "Meal recommendations are temporarily unavailable"})

    async def test_unexpected_parser_exception_is_sanitized(self):
        with patch("app.intent.DeterministicIntentParser.parse", side_effect=RuntimeError("synthetic-parser-secret")):
            with patch("app.providers.search_restaurants") as search:
                with self.assertLogs("mealpilot", level="INFO") as logs:
                    status, body, _ = await asgi_request({"text": "budget 200"}, path="/recommend/text")
        self.assertEqual(status, 500)
        self.assertEqual(body, {"detail": "Internal server error"})
        self.assertNotIn("synthetic-parser-secret", " ".join(logs.output))
        search.assert_not_called()

    async def test_ambiguous_exclusion_scope_never_searches(self):
        for text in ("without chicken and spicy budget 220", "no chicken and Indian budget 220", "budget 220 with rice and"):
            with self.subTest(text=text), patch("app.providers.search_restaurants") as search:
                status, body, _ = await asgi_request({"text": text}, path="/recommend/text")
                self.assertEqual((status, body["status"]), (200, "clarification_required"))
                self.assertNotIn("result", body)
                search.assert_not_called()

    async def test_schema_valid_but_contradictory_parser_output_is_502(self):
        class InaccurateParser:
            def parse(self, text):
                return {
                    "status": "parsed", "intent": {
                        "hard_constraints": {"budget": 300, "avoid_ingredients": [], "required_ingredients": []},
                        "soft_preferences": {"preferred_cuisine": None, "want_spicy": False}, "explicit_fields": ["budget"],
                    },
                }
        with patch.dict(app.dependency_overrides, {get_intent_parser: InaccurateParser}):
            with patch("app.providers.search_restaurants") as search:
                status, body, _ = await asgi_request({"text": "budget 300 without chicken"}, path="/recommend/text")
        self.assertEqual((status, body), (502, {"detail": "Intent parser returned invalid output"}))
        search.assert_not_called()

    async def test_actual_mock_files_fail_without_partial_results(self):
        valid = {"restaurant": "Synthetic", "dish": "Bowl", "price": 100, "cuisine": "Indian", "spicy": False, "ingredients": ["rice"]}
        files = [
            (b"[", 503), (b"\xff", 503), (b"{}", 502),
            (json.dumps([valid, {"dish": "Incomplete"}]).encode(), 502),
            (json.dumps([{**valid, "ingredients_complete": False}]).encode(), 502),
            (json.dumps([{**valid, "ingredients": ["rice", "unknown"]}]).encode(), 502),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            for data, expected in files:
                path.write_bytes(data)
                with self.subTest(data=data), patch("app.mock_mcp.DATA_FILE", path):
                    status, body = await post_recommend({"budget": 200, "avoid_ingredients": ["chicken"]})
                self.assertEqual(status, expected)
                self.assertEqual(set(body), {"detail"})
                self.assertNotIn("recommendations", body)
            with patch("app.mock_mcp.DATA_FILE", Path(directory) / "missing.json"):
                status, _ = await post_recommend({"budget": 200})
            self.assertEqual(status, 503)

    async def test_invalid_response_is_sanitized_by_actual_http_serialization(self):
        with patch("app.main.recommend_meals", return_value=[{"dish": "synthetic-private-detail"}]):
            with self.assertLogs("mealpilot", level="INFO") as logs:
                status, body = await post_recommend({"budget": 200})
        self.assertEqual((status, body), (500, {"detail": "Internal server error"}))
        self.assertNotIn("synthetic-private-detail", " ".join(logs.output))
