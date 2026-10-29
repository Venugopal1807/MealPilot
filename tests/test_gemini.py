"""Offline adapter contract checks; no Gemini accuracy claim or external I/O."""
import io
import json
import os
import threading
import time
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.gemini import GeminiIntentParser, GeminiRestClient, NoRedirect, gemini_intent_schema, policy_reference
from app.intent import DeterministicIntentParser, InvalidParserOutput, ParserUnavailable, get_intent_parser, parse_intent
from app.main import app, get_text_recommendations
from app.providers import get_meal_provider, MockMealProvider
from app.schemas import NaturalLanguageRequest, ParsedIntent
from test_api import asgi_request


TEXT = "budget 220; Indian; with paneer; without chicken"
PARAPHRASE = "my spending limit is 220; I would prefer Indian; make sure it contains paneer; leave out chicken"


def envelope(output=None):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [
        {"text": json.dumps(output if output is not None else DeterministicIntentParser().parse(TEXT))}
    ]}}]}


def parser(output=None):
    client = Mock()
    client.generate.return_value = envelope(output)
    return GeminiIntentParser(client=client, api_key="fake-test-key", model="gemini-test")


class GeminiContractTests(unittest.TestCase):
    def test_schema_conversion_preserves_all_other_schema_facts(self):
        original = ParsedIntent.model_json_schema()
        expected = deepcopy(original)
        status = expected["properties"]["status"]
        status["enum"] = [status.pop("const")]
        budget = expected["$defs"]["HardConstraints"]["properties"]["budget"]
        del budget["exclusiveMinimum"]
        budget["minimum"] = 1
        soft = expected["$defs"]["SoftPreferences"]["properties"]
        del soft["preferred_cuisine"]["default"]
        del soft["want_spicy"]["default"]
        self.assertEqual(gemini_intent_schema(), expected)
        self.assertEqual(gemini_intent_schema(), expected)

    def test_schema_conversion_does_not_mutate_source_or_application_defaults(self):
        original = ParsedIntent.model_json_schema()
        snapshot = deepcopy(original)
        with patch.object(ParsedIntent, "model_json_schema", return_value=original):
            converted = gemini_intent_schema()
        self.assertEqual(original, snapshot)
        converted["$defs"]["HardConstraints"]["properties"]["budget"]["minimum"] = 99
        self.assertEqual(original, snapshot)
        result = ParsedIntent.model_validate({"status": "parsed", "intent": {
            "hard_constraints": {"budget": 1}, "soft_preferences": {},
            "explicit_fields": ["budget"]}})
        self.assertIsNone(result.intent.soft_preferences.preferred_cuisine)
        self.assertFalse(result.intent.soft_preferences.want_spicy)
        self.assertEqual(result.intent.hard_constraints.avoid_ingredients, [])
        self.assertEqual(result.intent.hard_constraints.required_ingredients, [])

    def test_unreviewed_schema_constraints_stop_before_model_and_provider(self):
        for keyword, value in (("pattern", "^positive$"), ("exclusiveMinimum", 0.5)):
            source = ParsedIntent.model_json_schema()
            budget = source["$defs"]["HardConstraints"]["properties"]["budget"]
            budget[keyword] = value
            p, provider = parser(), Mock()
            with self.subTest(keyword=keyword), patch.object(ParsedIntent, "model_json_schema", return_value=source):
                with self.assertRaises(Exception) as caught:
                    get_text_recommendations(NaturalLanguageRequest(text=TEXT), p, provider)
                self.assertEqual(caught.exception.status_code, 503)
                p.client.generate.assert_not_called()
                provider.search_meals.assert_not_called()

    def test_separate_llm_evaluation_and_original_expectations(self):
        from pathlib import Path
        from evaluation import run_evaluation
        report = run_evaluation(Path(__file__).parent / "fixtures" / "llm_cases.json")
        self.assertEqual(report["summary"]["total_scenarios"], 9)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["fault_injection"]["detected"], 10)

    def test_supported_and_equivalent_wording(self):
        p = parser()
        a, b = parse_intent(TEXT, p), parse_intent(PARAPHRASE, p)
        self.assertEqual(a.intent.semantic_key(), b.intent.semantic_key())
        self.assertEqual(a.intent.hard_constraints.budget, 220)
        self.assertEqual(a.intent.hard_constraints.required_ingredients, ["paneer"])
        self.assertEqual(a.intent.hard_constraints.avoid_ingredients, ["chicken"])
        self.assertEqual(a.intent.soft_preferences.preferred_cuisine, "indian")
        self.assertNotEqual(DeterministicIntentParser().parse(PARAPHRASE)["status"], "parsed")
        request = p.client.generate.call_args.kwargs
        self.assertEqual(request["timeout"], 10)
        self.assertEqual(request["payload"]["contents"][0]["parts"][0]["text"], PARAPHRASE)
        self.assertEqual(request["payload"]["generationConfig"], {
            "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": gemini_intent_schema()}},
            "maxOutputTokens": 2048,
        })

    def test_omitted_invented_and_changed_constraints_fail_before_search(self):
        for wrong in ("budget 220; Indian; with paneer", "budget 221; Indian; with paneer; without chicken",
                      "budget 220; Indian; without paneer; with chicken", "budget 220; Chinese; with paneer; without chicken",
                      TEXT + "; spicy", TEXT + "; with rice"):
            with self.subTest(wrong=wrong):
                p = parser(DeterministicIntentParser().parse(wrong))
                provider = Mock()
                with self.assertRaises(Exception) as caught:
                    get_text_recommendations(NaturalLanguageRequest(text=PARAPHRASE), p, provider)
                self.assertEqual(caught.exception.status_code, 502)
                provider.search_meals.assert_not_called()

    def test_scope_ambiguity_unknown_and_safety_stop_without_model(self):
        for text in ("without chicken and spicy budget 220", "no chicken and Indian budget 220",
                     "maybe without chicken budget 220", "budget 200 or 300", "budget 220; not without chicken",
                     "budget 220; delivered in 20 minutes", "budget 220; no peanuts",
                     "vegan budget 220", "allergy safe budget 220", "protein budget 220",
                     "budget 220; only Indian", "budget 220; not spicy", "budget 220; leave out chicken or rice"):
            with self.subTest(text=text):
                p, provider = parser(), Mock()
                result = get_text_recommendations(NaturalLanguageRequest(text=text), p, provider)
                self.assertIn(result.status, ("unsupported", "clarification_required"))
                p.client.generate.assert_not_called()
                provider.search_meals.assert_not_called()

    def test_negation_and_strict_budget(self):
        text = "under 221; don't include chicken"
        output = DeterministicIntentParser().parse("budget 220 without chicken")
        self.assertEqual(parse_intent(text, parser(output)).intent.hard_constraints.avoid_ingredients, ["chicken"])
        with self.assertRaises(InvalidParserOutput):
            parse_intent(text, parser(DeterministicIntentParser().parse("budget 221 without chicken")))

    def test_contradiction_preserved_and_optimizer_runs(self):
        text = "budget 220 with chicken without chicken"
        p = parser(DeterministicIntentParser().parse(text))
        result = get_text_recommendations(NaturalLanguageRequest(text=text), p, MockMealProvider())
        self.assertEqual(result["result"]["count"], 0)

    def test_catalog_and_optimizer_cannot_be_bypassed(self):
        result = get_text_recommendations(NaturalLanguageRequest(text=PARAPHRASE), parser(), MockMealProvider())
        self.assertEqual([(r["dish"], r["score"]) for r in result["result"]["recommendations"]], [("Paneer Rice Bowl", 2)])
        bad = Mock(name="bad_catalog"); bad.name = "fake"
        bad.search_meals.return_value = [{"dish": "No facts"}]
        with self.assertRaises(Exception) as caught:
            get_text_recommendations(NaturalLanguageRequest(text=PARAPHRASE), parser(), bad)
        self.assertEqual(caught.exception.status_code, 502)

    def test_malformed_and_refused_outputs(self):
        for raw in (None, [], {}, {"promptFeedback": []}, {"candidates": [None]}, {"candidates": []}, {"candidates": [{}]}, envelope({"status": "parsed"}),
                    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{bad"}]}}]}):
            p = parser(); p.client.generate.return_value = raw
            with self.subTest(raw=raw), self.assertRaises(InvalidParserOutput):
                parse_intent(TEXT, p)
        p = parser(); p.client.generate.return_value["candidates"][0]["finishReason"] = "SAFETY"
        with self.assertRaises(ParserUnavailable):
            parse_intent(TEXT, p)
        p.client.generate.return_value = {"promptFeedback": {"blockReason": "SAFETY"}}
        with self.assertRaises(ParserUnavailable):
            parse_intent(TEXT, p)

    def test_rest_request_auth_body_timeout_and_redirect_protection(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope()).encode()
        with patch("app.gemini.build_opener", return_value=opener):
            result = GeminiIntentParser(api_key="fake", model="gemini-test", timeout=3).parse(TEXT)
        self.assertEqual(result, DeterministicIntentParser().parse(TEXT))
        args, kwargs = opener.open.call_args
        request = args[0]
        self.assertEqual(request.full_url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent")
        self.assertNotIn("fake", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "fake")
        body = json.loads(request.data)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(body["contents"], [{"role": "user", "parts": [{"text": TEXT}]}])
        self.assertTrue(body["systemInstruction"]["parts"][0]["text"])
        config = body["generationConfig"]
        self.assertEqual(config, {
            "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": gemini_intent_schema()}},
            "maxOutputTokens": 2048,
        })
        for legacy in ("responseMimeType", "responseSchema", "responseJsonSchema", "_responseJsonSchema"):
            self.assertNotIn(legacy, config)
        self.assertEqual(kwargs, {"timeout": 3})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, "", {}, "https://other"))
        for data in (b"not json", b"x" * 262145):
            opener.open.return_value.__enter__.return_value.read.return_value = data
            with patch("app.gemini.build_opener", return_value=opener), self.assertRaises(InvalidParserOutput):
                GeminiRestClient().generate(model="gemini-test", api_key="fake", timeout=3, payload={})

    def test_configuration_is_explicit_and_safe(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(get_intent_parser(), DeterministicIntentParser)
        for env in ({"MEALPILOT_INTENT_BACKEND": "gemini"}, {"MEALPILOT_INTENT_BACKEND": "typo"},
                    {"MEALPILOT_INTENT_BACKEND": "gemini", "GEMINI_API_KEY": "fake", "MEALPILOT_GEMINI_MODEL": "gemini-test", "MEALPILOT_LLM_TIMEOUT_SECONDS": "nan"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch.object(GeminiRestClient, "generate") as call:
                with self.assertRaises(ParserUnavailable):
                    parse_intent(TEXT, get_intent_parser())
                call.assert_not_called()

    def test_http_errors_and_network_failure_no_retry(self):
        for code, exception in ((400, ParserUnavailable), (401, ParserUnavailable), (403, ParserUnavailable),
                                (429, ParserUnavailable), (500, ParserUnavailable), (503, ParserUnavailable),
                                (408, TimeoutError), (504, TimeoutError)):
            opener = Mock(); opener.open.side_effect = HTTPError("https://test", code, "secret", {}, io.BytesIO(b"secret"))
            with self.subTest(code=code), patch("app.gemini.build_opener", return_value=opener), self.assertRaises(exception):
                GeminiRestClient().generate(model="gemini-test", api_key="secret", timeout=1, payload={})
            self.assertEqual(opener.open.call_count, 1)
        opener = Mock(); opener.open.side_effect = URLError("private details")
        with patch("app.gemini.build_opener", return_value=opener), self.assertRaises(ParserUnavailable):
            GeminiRestClient().generate(model="gemini-test", api_key="secret", timeout=1, payload={})

    def test_real_socket_read_timeout_on_local_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(0.25)
                self.send_response(200); self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        original = build_opener(NoRedirect())
        class LocalOpener:
            def open(self, req, timeout):
                return original.open(Request(f"http://127.0.0.1:{server.server_port}/", data=req.data), timeout=timeout)
        try:
            with patch("app.gemini.build_opener", return_value=LocalOpener()), self.assertRaises(TimeoutError):
                GeminiRestClient().generate(model="gemini-test", api_key="fake", timeout=0.03, payload={})
        finally:
            server.shutdown(); server.server_close(); thread.join()


class GeminiApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_smoke_and_zero_search_on_failure(self):
        p = parser()
        with patch.dict(app.dependency_overrides, {get_intent_parser: lambda: p}):
            status, body, _ = await asgi_request({"text": PARAPHRASE}, path="/recommend/text")
            self.assertEqual(status, 200); self.assertEqual(body["result"]["count"], 1)
            status, body, _ = await asgi_request({"budget": 200})
            self.assertEqual(status, 200); self.assertEqual(body["count"], 2)
            for error, expected in ((TimeoutError("secret"), 504), (ParserUnavailable("secret"), 503), (InvalidParserOutput("secret"), 502)):
                p.client.generate.side_effect = error
                with patch("app.providers.search_restaurants") as search:
                    status, body, _ = await asgi_request({"text": PARAPHRASE}, path="/recommend/text")
                    self.assertEqual(status, expected); self.assertNotIn("secret", json.dumps(body))
                    search.assert_not_called()
