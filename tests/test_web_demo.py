"""Same-origin demo assets plus the real API journey, without external I/O."""
import asyncio
import os
import unittest
from unittest.mock import patch

from app.main import app
from app.providers import get_meal_provider
from test_api import asgi_request


async def get_asset(path):
    messages = []
    request_sent = False
    complete = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            complete.set()

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
               "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
               "query_string": b"", "root_path": "", "headers": [],
               "client": ("test", 1), "server": ("test", 80)}, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages)
    return start["status"], dict(start["headers"]), body


class WebDemoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"MEALPILOT_INTENT_BACKEND": "deterministic"})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_page_and_assets_are_served_without_catalog_or_model_access(self):
        with patch("app.providers.search_restaurants") as search, patch("app.gemini.GeminiRestClient.generate") as model:
            for path, content_type, expected in (
                ("/", b"text/html", b'MOCK CATALOG DEMO'),
                ("/static/demo.css", b"text/css", b"--accent: #FF5A1F"),
                ("/static/demo.js", b"javascript", b'fetch("/recommend/text"'),
            ):
                with self.subTest(path=path):
                    status, headers, body = await get_asset(path)
                    self.assertEqual(status, 200)
                    self.assertIn(content_type, headers[b"content-type"])
                    self.assertIn(expected, body)
                    self.assertIn(b"x-request-id", headers)
            search.assert_not_called()
            model.assert_not_called()

    async def test_real_api_journey_preserves_intent_results_and_empty_state(self):
        status, body, _ = await asgi_request({"text": "Indian meal at most 220 with paneer without chicken"}, path="/recommend/text")
        self.assertEqual(status, 200)
        self.assertEqual(body["intent"]["hard_constraints"]["avoid_ingredients"], ["chicken"])
        self.assertEqual(body["result"]["recommendations"][0]["dish"], "Paneer Rice Bowl")
        self.assertEqual(body["result"]["recommendations"][0]["price"], 180)
        self.assertIn("Matches your preferred cuisine", body["result"]["recommendations"][0]["reasons"])
        status, body, _ = await asgi_request({"text": "budget 100"}, path="/recommend/text")
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["count"], 0)
        self.assertEqual(body["intent"]["hard_constraints"]["budget"], 100)

    async def test_unresolved_and_invalid_demo_requests_never_search(self):
        with patch("app.providers.search_restaurants") as search:
            for text, expected in (("without chicken and spicy budget 220", "clarification_required"),
                                   ("vegan budget 220", "unsupported")):
                status, body, _ = await asgi_request({"text": text}, path="/recommend/text")
                self.assertEqual(status, 200)
                self.assertEqual(body["status"], expected)
                self.assertTrue(body["issues"][0]["message"])
                self.assertNotIn("result", body)
            status, _, _ = await asgi_request({"text": " "}, path="/recommend/text")
            self.assertEqual(status, 422)
            search.assert_not_called()

    async def test_static_mount_does_not_expose_project_files(self):
        for path in ("/static/../schemas.py", "/static/.env", "/static/nonexistent"):
            status, _, _ = await get_asset(path)
            self.assertEqual(status, 404)
