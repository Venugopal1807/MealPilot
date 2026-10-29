"""Network-free checks of the separately opted-in connectivity diagnostic."""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import gemini_connectivity_probe as probe


class ConnectivityProbeTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GEMINI_API_KEY": "fake-probe-key"})
        environment.start()
        self.addCleanup(environment.stop)
        network = patch("socket.socket", side_effect=AssertionError("Network forbidden"))
        self.network = network.start()
        self.addCleanup(network.stop)
        opener = patch.object(probe, "build_opener")
        self.opener_factory = opener.start()
        self.addCleanup(opener.stop)
        process = patch.object(probe.subprocess, "run")
        self.process = process.start()
        self.addCleanup(process.stop)

    def invoke(self, args, confirmation="PROBE 1"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                status = probe.main(args, confirm=lambda _: confirmation)
            except SystemExit as error:
                status = error.code
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("fake-probe-key", output)
        return status, output

    def test_missing_opt_in_or_configuration_never_starts_a_worker(self):
        for args in (["--model", "gemini-test"], ["--live"],
                     ["--live", "--model", "not/a/model"]):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(args)[0], 2)
        for key in ("", "   ", "fake\nkey"):
            with self.subTest(key_kind="empty_or_invalid"), patch.dict(os.environ, {"GEMINI_API_KEY": key}):
                self.assertEqual(self.invoke(["--live", "--model", "gemini-test"])[0], 2)
        self.process.assert_not_called()
        self.opener_factory.assert_not_called()
        self.network.assert_not_called()

    def test_confirmation_must_be_exact_before_any_worker(self):
        for answer in ("", "yes", "RUN 1", "PROBE 2"):
            self.assertEqual(self.invoke(["--live", "--model", "gemini-test"], answer)[0], 2)
        self.process.assert_not_called()
        self.opener_factory.assert_not_called()

    def test_worker_flag_cannot_bypass_confirmation_or_supervision(self):
        self.assertEqual(self.invoke(["--live", "--model", "gemini-test", "--_worker"])[0], 2)
        self.process.assert_not_called()
        self.opener_factory.assert_not_called()

    def test_import_has_no_network_or_worker_side_effects(self):
        import runpy
        runpy.run_path(str(Path(probe.__file__).resolve()), run_name="probe_import_test")
        self.process.assert_not_called()
        self.opener_factory.assert_not_called()
        self.network.assert_not_called()

    def test_confirmation_eof_or_interrupt_cancels_without_worker(self):
        for error in (EOFError, KeyboardInterrupt):
            stdout = io.StringIO()
            with self.subTest(error=error), contextlib.redirect_stdout(stdout):
                self.assertEqual(probe.main(["--live", "--model", "gemini-test"],
                                            confirm=Mock(side_effect=error)), 2)
            self.assertIn("may consume API quota", stdout.getvalue())
            self.assertNotIn("fake-probe-key", stdout.getvalue())
        self.process.assert_not_called()
        self.opener_factory.assert_not_called()

    def test_one_post_uses_header_auth_and_never_reads_response_body(self):
        opener = self.opener_factory.return_value
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        result = probe.request_once("gemini-test")
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["requests_attempted"], 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-goog-api-key"), "fake-probe-key")
        self.assertEqual(json.loads(request.data),
                         {"contents": [{"parts": [{"text": "Reply with OK."}]}]})
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 15.0})
        self.assertIsInstance(self.opener_factory.call_args.args[0], probe.NoRedirect)
        opener.open.assert_called_once()
        response.read.assert_not_called()
        self.assertNotIn("fake-probe-key", json.dumps(result))

    def test_http_error_statuses_are_not_confused_with_socket_timeouts(self):
        for status in (301, 400, 401, 403, 404, 408, 429, 500, 504):
            with self.subTest(status=status):
                error = HTTPError("https://example.invalid/", status, "private error", {}, io.BytesIO(b"private body"))
                self.opener_factory.return_value.open.side_effect = error
                self.opener_factory.return_value.open.reset_mock()
                result = probe.request_once("gemini-test")
                self.assertEqual(result["outcome"], "http_response")
                self.assertEqual(result["http_status"], status)
                self.assertNotIn("private", json.dumps(result))
                self.opener_factory.return_value.open.assert_called_once()

    def test_socket_timeouts_and_dns_tls_connection_errors_are_distinct(self):
        cases = (
            (TimeoutError("private"), "socket_timeout", None),
            (URLError(TimeoutError("private")), "socket_timeout", None),
            (URLError(socket.gaierror("private")), "connection_error", "dns"),
            (URLError(ssl.SSLError("private")), "connection_error", "tls"),
            (ConnectionRefusedError("private"), "connection_error", "connection"),
            (URLError("private"), "connection_error", "other"),
            (RuntimeError("private"), "probe_error", None),
        )
        for error, outcome, kind in cases:
            with self.subTest(outcome=outcome, kind=kind):
                self.opener_factory.return_value.open.side_effect = error
                result = probe.request_once("gemini-test")
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["connection_error_kind"], kind)
                self.assertIsNone(result["http_status"])
                self.assertNotIn("private", json.dumps(result))

    def test_parent_uses_one_worker_and_does_not_put_key_on_command_line(self):
        worker_result = probe.report("http_response", status=200, attempted=1)
        worker_result["untrusted_extra"] = "private response"
        self.process.return_value = Mock(stdout=json.dumps(worker_result), stderr="private error", returncode=0)
        status, output = self.invoke(["--live", "--model", "gemini-test"])
        self.assertEqual(status, 0)
        self.assertIn('"http_status": 200', output)
        self.assertNotIn("private", output)
        self.process.assert_called_once()
        command = self.process.call_args.args[0]
        self.assertNotIn("fake-probe-key", " ".join(command))
        self.assertEqual(command[:3], [probe.sys.executable, "-B", "-c"])
        self.assertEqual(command[-1], "gemini-test")
        self.assertNotIn("--_worker", command)
        self.assertEqual(self.process.call_args.kwargs["cwd"], str(Path(probe.__file__).resolve().parent))
        self.assertEqual(self.process.call_args.kwargs["timeout"], 15.0)
        self.opener_factory.assert_not_called()

    def test_slow_dns_or_stalled_worker_hits_parent_deadline_without_retry(self):
        self.process.side_effect = subprocess.TimeoutExpired("private", 15, output="private response", stderr="private key")
        status, output = self.invoke(["--live", "--model", "gemini-test"])
        self.assertEqual(status, 1)
        self.assertIn('"outcome": "deadline_timeout"', output)
        self.assertIn('"requests_attempted": null', output)
        self.assertNotIn("private", output)
        self.process.assert_called_once()
        self.opener_factory.assert_not_called()

    def test_malformed_or_untrusted_worker_output_is_never_forwarded(self):
        for raw in ("private raw response", '{"outcome":"private response"}',
                    json.dumps({**probe.report("http_response", status=200, attempted=1),
                                "http_status": "private response"})):
            with self.subTest(output_kind="malformed_or_untrusted"):
                self.process.return_value = Mock(stdout=raw, stderr="private error", returncode=1)
                result = probe.supervised_request("gemini-test")
                self.assertEqual(result["outcome"], "probe_error")
                self.assertNotIn("private", json.dumps(result))

    def test_redirect_policy_does_not_follow_location(self):
        self.assertIsNone(probe.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))


if __name__ == "__main__":
    unittest.main()
