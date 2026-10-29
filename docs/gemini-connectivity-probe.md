# Single-request Gemini connectivity probe

Prepared offline; no authenticated request has been run with this probe.
Public official Google documentation was reviewed on 2026-10-29 in the preceding
documentation-only review. The endpoint, header authentication, minimal text body,
and stable `gemini-3.8-flash` identifier are documented. Account access, credentials,
quota, connectivity and live schema acceptance remain unverified. Preparing or
testing this diagnostic offline does not authorize an authenticated request.

## Findings from the existing implementation

- `app/gemini.py` sends `GEMINI_API_KEY` in the `x-goog-api-key` header, with JSON
  content type. The key is not placed in the URL.
- The URL is `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`.
- The meal adapter adds a system instruction, user content, the compatibility-
  adjusted shared intent schema under `generationConfig.responseFormat.text`
  (`mimeType: APPLICATION_JSON`) and a 2048 output-token limit. Its strict
  Pydantic validation and independent meaning checks remain mandatory.
- `GeminiRestClient` collapses HTTP 408/504 into the same sanitized timeout
  exception used for socket timeouts. Other HTTP codes are preserved internally
  in `GeminiTransportUnavailable.status`; connection errors without an HTTP code
  become that exception with no status. The existing benchmark report omits
  those HTTP status details.
- The probe uses the explicit `--model` value; it does not load
  `MEALPILOT_GEMINI_MODEL` automatically. Pass the intended configured identifier
  explicitly. The local regular expression proves syntax, not account access.
- The previous report records timeout 60 seconds, one attempted request, zero
  successful responses, and 63923.498 ms of client-call duration. Socket timeout
  settings do not impose a DNS or overall deadline.
- The user's separate network observations establish intermittent DNS slowness,
  successful TCP/TLS, a responding root URL and an unauthenticated HTTP 403.
  These observations do not establish authenticated generation access.

## Diagnostic boundaries

`gemini_connectivity_probe.py` uses the existing adapter's header-authentication
method and `NoRedirect` handler. It makes at most one opener call and keeps
urllib's normal proxy discovery and TLS verification. It does not invoke the
intent parser, optimizer, recommendation provider or benchmark.
It imports only `NoRedirect` from the adapter and never invokes the new schema
helper. The structured-output change therefore does not affect its request body.

The minimal request body:

```json
{"contents": [{"parts": [{"text": "Reply with OK."}]}]}
```

The CLI requires `--live`, an explicit `--model` and exact interactive confirmation
`PROBE 1`. Credentials come from the process environment, never CLI arguments.
`GEMINI_API_KEY` must already be configured through the local secrets process.
There is no worker CLI option that bypasses confirmation. After confirmation,
the parent starts one internal worker in the repository directory. Importing the
module does not start a worker or make a request; tests replace worker execution
and HTTP transport with fakes and prohibit socket creation.
The timeout is fixed at 15 seconds, independent of the benchmark's environment
timeout. A parent process supervises the single worker with a 15-second wait
limit, covering slow DNS as well as socket waits; process startup/termination
and scheduling can add overhead. A local timeout does not guarantee remote
cancellation or prevent quota consumption by work already accepted by Google.

The probe reports only HTTP status, a fixed error category, elapsed time and
attempt metadata. HTTP 408/504 remain HTTP responses. Socket timeouts are
`socket_timeout`; the parent wait limit is `deadline_timeout`; other connection
errors are categorized as DNS, TLS, connection or other. A killed worker has
unknown request-attempt count (`null`), with a maximum of one. No headers, keys,
exception messages or raw response bodies are printed. Response bodies are not
read. HTTP success establishes receipt of response headers, not meal-adapter
schema compatibility or model accuracy. There are no retries or redirects.

Command from `D:\dev1\mealpilot-ai`, only after separate authorization:

```powershell
.\.venv\Scripts\python.exe -B -m gemini_connectivity_probe --live --model gemini-3.8-flash
```

Type exactly `PROBE 1` at the prompt to authorize the single request. Any other
answer, EOF, or interruption at confirmation cancels without a worker. The
request may consume API quota. There is no automatic model substitution or
model-list request. The probe remains a connectivity diagnostic, not a benchmark.

Official sources verified during the documentation-only review:

- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/generate-content/get-started
- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output

## Interpreting results

| Result | Establishes | Does not establish |
| --- | --- | --- |
| `http_response`, 2xx | Success status headers received for this minimal authenticated request. | Response-body contents, completed body delivery, intent-schema acceptance, accuracy or reliability. |
| `http_response`, other status | An HTTP response was received; 3xx is not followed and 408/504 remain HTTP outcomes. | Exact rejection cause without a body; status alone may come from an intermediary. |
| `socket_timeout` | A local socket operation timed out. | Whether Google accepted, processed or charged for the request. |
| `deadline_timeout` | The parent worker wait exceeded 15 seconds and the worker was terminated. | Whether a POST started or remote work stopped; attempt count is unknown. |
| `connection_error`, `dns`/`tls`/`connection`/`other` | The local failure category. | Authenticated model access or a definitive remote root cause. |
| `probe_error` | A local diagnostic or worker-output failure occurred. | Model health, access or request success. |
| `interrupted` | Supervised execution was interrupted locally. | Remote cancellation or whether the request started. |

Exit code is 0 only for a 2xx HTTP outcome, 1 for other execution outcomes, and
2 for invalid arguments/configuration or cancelled confirmation. No result
validates MealPilot's structured-output adapter end to end.

## Offline verification

```powershell
py -3.14 -B -m unittest discover -s tests -p test_gemini_connectivity_probe.py -v
```

Tests inject fake HTTP responses and exceptions, mock worker
execution, and prohibit socket creation. They cover exact opt-in/confirmation,
one-request construction, absence of response-body reads, HTTP versus timeout
classification, DNS/TLS/connection failures, parent deadlines, output
sanitization, redirect refusal, import safety and rejection of the former
confirmation-bypassing worker flag. They are not live connectivity evidence.
