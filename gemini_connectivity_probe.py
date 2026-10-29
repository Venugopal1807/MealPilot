"""Opt-in, single-POST connectivity diagnostic; never part of evaluation.

Uses the existing adapter's API-key header and redirect policy, with a tiny
generateContent payload instead of the meal-intent schema. Never prints headers,
credentials, exception text, or response bodies. Public-doc verification and
user approval must precede a real invocation; creating this file makes no calls.
"""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from app.gemini import NoRedirect


TIMEOUT_SECONDS = 15.0
PAYLOAD = {"contents": [{"parts": [{"text": "Reply with OK."}]}]}
OUTCOMES = {"http_response", "socket_timeout", "connection_error",
            "deadline_timeout", "probe_error", "interrupted"}
CONNECTION_ERRORS = {"dns", "tls", "connection", "other"}


def report(outcome, *, status=None, connection_error=None, attempted=None):
    return {
        "outcome": outcome,
        "http_status": status,
        "connection_error_kind": connection_error,
        "requests_attempted": attempted,
        "max_requests": 1,
        "socket_timeout_seconds": TIMEOUT_SECONDS,
        "wall_timeout_seconds": TIMEOUT_SECONDS,
        "response_body_read": False,
    }


def request_once(model):
    """Exactly one opener call; HTTP errors retain their status, including 504."""
    started = time.perf_counter()
    attempted = 0
    try:
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(PAYLOAD).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
            method="POST",
        )
        # Preserve urllib's normal proxy discovery and TLS verification.
        opener = build_opener(NoRedirect())
        attempted = 1
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            # HTTP status is sufficient here. Do not read or log the body.
            result = report("http_response", status=response.status, attempted=1)
    except HTTPError as error:
        result = report("http_response", status=error.code, attempted=1)
        error.close()
    except (TimeoutError, socket.timeout):
        result = report("socket_timeout", attempted=attempted)
    except (URLError, OSError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, TimeoutError):
            result = report("socket_timeout", attempted=attempted)
        else:
            kind = ("dns" if isinstance(reason, socket.gaierror) else
                    "tls" if isinstance(reason, ssl.SSLError) else
                    "connection" if isinstance(reason, OSError) else "other")
            result = report("connection_error", connection_error=kind, attempted=attempted)
    except Exception:
        result = report("probe_error", attempted=attempted)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def supervised_request(model):
    """A subprocess bounds DNS and other blocking work as well as socket waits.

The key is inherited through the environment, never a command-line argument.
Timing out kills the local worker; it cannot guarantee remote cancellation.
"""
    started = time.perf_counter()
    try:
        worker = subprocess.run(
            [sys.executable, "-B", "-c",
             "import json, sys; from gemini_connectivity_probe import request_once; "
             "print(json.dumps(request_once(sys.argv[1])))", model],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        # Reconstruct only fixed fields. Never forward worker stderr or raw text.
        raw = json.loads(worker.stdout)
        if (type(raw) is not dict or raw.get("outcome") not in OUTCOMES
                or type(raw.get("requests_attempted")) is not int
                or raw["requests_attempted"] not in (0, 1)):
            raise ValueError("Invalid worker result")
        status = raw.get("http_status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("Invalid HTTP status")
        kind = raw.get("connection_error_kind")
        if kind is not None and kind not in CONNECTION_ERRORS:
            raise ValueError("Invalid connection classification")
        result = report(raw["outcome"], status=status, connection_error=kind,
                        attempted=raw["requests_attempted"])
    except subprocess.TimeoutExpired:
        # No worker result: it is unknown whether the POST had started.
        result = report("deadline_timeout")
    except KeyboardInterrupt:
        result = report("interrupted")
    except Exception:
        result = report("probe_error")
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return result


def main(argv=None, *, confirm=input):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--live", action="store_true", help="Explicitly opt in to one Google POST")
    cli.add_argument("--model", required=True, help="Explicit model ID; verify against official docs first")
    args = cli.parse_args(argv)
    if not args.live:
        cli.error("--live is required; no request made")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", args.model):
        cli.error("Invalid model ID syntax; no request made")
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key.strip() or not key.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in key):
        cli.error("A valid GEMINI_API_KEY environment value is required; no request made")
    print("One authenticated POST to Google; it may consume API quota. No retries.")
    print("15-second socket timeout and parent deadline; remote cancellation is not guaranteed.")
    try:
        answer = confirm("Type PROBE 1 to authorize this request: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "PROBE 1":
        print("Cancelled; no request made.")
        return 2
    result = supervised_request(args.model)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["outcome"] == "http_response" and 200 <= result["http_status"] < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
