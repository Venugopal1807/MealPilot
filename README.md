# MealPilot AI

A local FastAPI meal recommender backed by the existing mock catalog in
`data/restaurants.json`. No Swiggy credentials or network access are needed.

## Run the web demo

From the repository root, with the existing application dependencies available:

```powershell
$env:MEALPILOT_INTENT_BACKEND = "deterministic"
.\.venv\Scripts\python.exe -B -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765/**. The static HTML/CSS/JavaScript frontend is served
by FastAPI, with no frontend build, framework, CDN, or additional dependencies.
It submits `{"text": "..."}` to the existing `/recommend/text` route. Try
`Indian meal at most 220 with paneer without chicken`, an ambiguous request such
as `without chicken and spicy budget 220`, or `budget 100` for no matches.

The page shows returned intent, ingredients, catalog prices, scores and reasons.
Clarification, unsupported requests, empty results, validation errors, loading
and service failures have distinct states. It does not store requests in browser
storage. Prices remain integer catalog units; no currency or checkout totals are
inferred. All meals are mock data, with no availability, dietary-safety or ordering
claim. The server's existing parser configuration still applies: use the command
above for the local deterministic demo. There is no browser Gemini selector.

Frontend assets live in `app/static/`; `/docs` retains the existing API explorer.
Run `python -B -m unittest discover -s tests -p test_web_demo.py -v` for the focused
asset/API checks, or the full offline suite documented below. Use the same Python
environment that runs the application.

Optional browser regression checks: with the deterministic server running,
`node tests/web_demo_smoke.cjs` uses an already installed Playwright and Edge.
If Playwright is installed elsewhere, set `MEALPILOT_PLAYWRIGHT_MODULE` to that
module's path. It is not an application dependency. The checks use the real local
API for normal requests and injected browser responses for failures/XSS, block
external page requests, and save desktop/mobile screenshots under ignored `build/`.

The refined UI uses a warm orange palette with centralized CSS tokens for colors,
surfaces, borders, radii, spacing and motion. It uses a local Poppins font if
available, otherwise a system fallback; no font download is required. Request
states have text labels as well as distinct styling, reduced-motion preferences
disable animation, and the input has an associated validation-error message.
The reference videos inform visual hierarchy only: ordering, payment, tracking,
accounts and dietary-safety concepts shown in them are not demo capabilities.

The verified environment is Windows with Python 3.14, FastAPI 0.141.1, Pydantic
2.13.4, and Uvicorn 0.52.4. Existing direct dependencies are pinned in
`requirements.txt`; transitive dependencies are not fully locked. If your chosen
environment is missing dependencies, install them with
`py -3.14 -m pip install -r requirements.txt`. No installation is needed when they
are already available. From the repository root, run:

```powershell
py -3.14 -B -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

`GET /health` returns `{"status": "ok", "project": "MealPilot AI"}`.
`POST /recommend` accepts a positive integer `budget`, optional
`avoid_ingredients`, `required_ingredients`, `preferred_cuisine`, and `want_spicy`.
Budget, exact ingredient exclusions, and all required ingredients are hard
constraints. Ingredient and cuisine comparisons strip surrounding
whitespace and use Unicode case folding. Cuisine and spicy preferences are soft
ranking signals worth two and one points respectively. Results sort by descending
score, ascending price, then meal fields for reproducible ties. Ingredient matching
does not infer synonyms, allergens, cross-contamination, or unlisted ingredients.
Conflicting required/excluded ingredients yield no matches. Dietary labels,
nutrition, availability, taxes, delivery fees, and serviceability are not present
in the catalog and are not inferred. Prices and budgets use the same existing
integer catalog units; the budget is an item-price limit, not a checkout quote.

Requests use strict native Python/Pydantic types: booleans, fractional numbers,
and strings are not accepted as integer budgets. Blank ingredient names and
unknown fields return 422. This intentionally fixes silent coercion and ignored
mandatory constraints; for example `dietary_restrictions` is rejected until the
catalog can support it. Existing valid requests and successful response fields
(`count`, `recommendations`, `source`) are preserved. Recommendation records retain
the six catalog fields plus `score` and `reasons`; the default source is `mock_mcp`.

```json
{"budget": 220, "required_ingredients": ["paneer"], "avoid_ingredients": ["chicken"]}
```

`app/providers.py` defines the provider boundary. The default adapter delegates to
the unchanged mock implementation; future vendor adapters must return MealPilot
meal records rather than expose vendor payloads to the optimizer. FastAPI's
`get_meal_provider` dependency supports replacement in local tests.
`app/candidates.py` checks the entire catalog before eligibility filtering. Each
record requires nonblank restaurant/dish/cuisine, a positive integer price, a
boolean spicy flag, and a nonempty list of nonblank ingredient names. Unknown
provider fields are discarded; ingredient lists are copied to prevent mutation
of provider data. Invalid records reject the entire catalog, rather than
silently accepting unknown facts or returning a partial recommendation list.
Ingredient placeholders `unknown`, `n/a`, `not available`, and `unspecified`
also invalidate the catalog. The optional internal `ingredients_complete` field,
when present, must be boolean `true`; false, null, and coerced values are rejected.
It is consumed as evidence metadata and is not added to successful API responses.
Legacy records without the flag retain provider-reported exact ingredient
matching. Plausible, unmarked omissions in a list cannot be discovered by this
validator; the application does not certify the completeness of real food data.

| Outcome | HTTP status |
| --- | --- |
| Valid catalog, including no matches | 200 |
| Invalid request | 422 |
| Structurally invalid/incomplete catalog or malformed parser output | 502 |
| Catalog read/JSON decoding failure or unavailable parser | 503 |
| Provider or parser reports a timeout | 504 |
| Unexpected programming exception | 500 |

Error responses use `detail` and do not expose provider exceptions. Every HTTP
response includes a generated `X-Request-ID`. The `mealpilot` logger emits JSON
records with request IDs and statuses; unexpected failures include only exception
type, never exception text, request bodies, or tokens. Configure that logger at
INFO to collect completion records. No automatic retries or network deadline
enforcement are implemented: timeout tests simulate a provider-reported timeout.

## Natural-language boundary

`POST /intent` accepts `{"text": "..."}` and returns a validated intent only.
`POST /recommend/text` accepts the same body and, for a parsed intent, returns
`{"status": "parsed", "intent": {...}, "result": {...}}`, where `result` is the
unchanged structured recommendation response. `POST /recommend` remains available
with its existing fields and behavior. Text must be a nonblank string of at most
2000 characters; extra request fields and malformed JSON return 422.

The flow is: bounded text request -> application capability guard -> injected
`IntentParser` -> strictly validated `MealIntent` -> existing `MealRequest` ->
existing mock provider and deterministic optimizer. The default deterministic mode needs no LLM, remote parser, credentials, or new dependencies. The parser never searches for meals
or ranks candidates. Nested adapter-supplied models are revalidated too.
Schema validity alone does not establish fidelity to the user request. Accepted
adapter output must also match the supported local grammar's explicit meaning,
using the optimizer's case/whitespace/set matching semantics. Changed budgets,
dropped exclusions, or invented requirements return 502 before provider search.
Unknown grammar requires clarification even if an injected adapter proposes a
schema-valid intent. Gemini mode adds the independently reviewed paraphrases
documented below; a model cannot expand the acceptance policy implicitly.

For example, `spicy Indian meal at most 220 with paneer without chicken` produces:

```json
{
  "status": "parsed",
  "intent": {
    "hard_constraints": {
      "budget": 220,
      "avoid_ingredients": ["chicken"],
      "required_ingredients": ["paneer"]
    },
    "soft_preferences": {"preferred_cuisine": "indian", "want_spicy": true},
    "explicit_fields": [
      "avoid_ingredients", "budget", "preferred_cuisine",
      "required_ingredients", "want_spicy"
    ]
  }
}
```

Only explicit input is extracted. The parser makes no inferred preferences.
Absent cuisine/spice preferences retain their existing neutral defaults, and
`explicit_fields` identifies supplied constraints/preferences. Ingredient lists
are case-folded, deduplicated, and sorted for equivalent intent output.

The deterministic implementation deliberately supports a limited grammar:

- One positive integer maximum: `budget 220`, `budget of 220`, `budget is 220`,
  `maximum budget 220`, `max budget 220`, `maximum 220`, `max 220`, `at most 220`,
  `up to 220`, or `no more than 220`. `under`, `below`, and `less than` are strict
  bounds: `under 220` maps to maximum 219 in the existing integer price units.
  Optional INR/rs/rupee markers do not convert currencies or establish live prices.
  Decimals, separators, ranges, and multiple budgets require clarification.
- Required ingredients after `with`, `include`, `including`, `require`, or
  `must contain`; exclusions after `without`, `exclude`, `excluding`, `avoid`,
  or `no`. Lists use `and` or commas. Supported literal names are chicken, rice,
  paneer, spices, chilli, vegetables, and soy sauce. These are grammar vocabulary,
  not a claim of provider availability. Unknown names require clarification;
  the structured endpoint remains available for other explicit ingredient names.
- One soft cuisine preference: Indian, Hyderabadi, Andhra, or Chinese; a soft
  `spicy` preference. `prefer Indian` and `prefer spicy` are supported. Mandatory
  cuisine or spice restrictions, including `not spicy`, are unsupported because
  the current optimizer does not enforce them as hard filters.

Case, whitespace, clause order, and ordinary sentence punctuation are normalized.
A small documented-in-code set of polite/request filler words is allowed. Every
other word must be consumed. Unknown, conditional, disjunctive, or softened
ingredient wording does not become a confirmed hard constraint.
For example, `without chicken and spicy budget 220` requires clarification rather
than turning the scoped word `spicy` into a positive preference. Use
`without chicken; prefer spicy; budget 220` to state the positive preference
separately. Unfinished conjunctions also require clarification.

Unresolved requests return HTTP 200 with `status: clarification_required` or
`status: unsupported`, plus stable issue codes and an explanation. They contain
no accepted intent or recommendations and never call provider search. For
example, `vegetarian food budget 200` returns `unsupported_dietary`; it is not
translated into an exclusion list. Known unsupported dietary/nutritional/safety
capabilities are guarded before even an injected parser can drop them. Unrecognized
wording still stops the local parser rather than silently discarding a requirement.
No synonym inference, dietary certification, allergy safety, nutritional scoring,
delivery guarantees, quantity allocation, or currency conversion is implemented.

## Local verification and evaluation baseline

```powershell
py -3.14 -B -m unittest discover -s tests -v
```

The suite uses the standard library test runner and the existing application
dependencies; no pytest, HTTP client, credentials, server process, or live MCP
tools are required. API tests invoke the ASGI app in process.

The 51 named scenarios in `tests/fixtures/recommendation_cases.json` retain all
34 v0.1.2 expectations unchanged and add 17 counterexample/failure scenarios.
They use the checked-in mock catalog and synthetic provider/parser fixtures.
See `docs/v0.1.3-verification.md` for the red-to-green evidence and its limits.
Run the standalone evaluation with:

```powershell
py -3.14 -B -m evaluation --output evaluation_report.json
```

The runner invokes the actual request model and route logic in process. HTTP
middleware and response serialization are covered separately by API tests.
Reports contain each scenario's expected/actual status and ordered dish/score
pairs, totals, passing/failing scenarios, and these failure metrics:

- Hard-constraint violations: returned-candidate violations of budget, required
  ingredients, exclusions, or usable ingredient metadata, checked independently
  from production eligibility code.
- Ranking failures: successful scenarios with incorrect ordered dishes or scores.
- Provider/validation failures: failed scenarios involving a provider error status
  or 422 respectively. Expected simulated errors that behave correctly pass;
  they are not counted as failures. These categories can overlap.
- Unexpected errors: unclassified exceptions in route evaluation.
- Intent failures: incorrect parsed constraints, issue codes, or outcome types.
  Parser failure regressions are counted separately from provider failures.
  Parsed, clarification, and unsupported outcomes are reported separately;
  an expected clarification/unsupported outcome is a passing safety result.
- Catalog grounding violations: returned facts absent from the provider snapshot,
  duplicate candidates beyond source multiplicity, or partial acceptance of an
  invalid catalog. Snapshots are copied before recommendation logic executes.
- Response contract failures: malformed records, invalid count/source, or bad
  intent response structure. Invalid responses are not mislabeled as client 422s.
- Provider access failures: observed calls differ from policy. Invalid requests,
  unresolved intent, and parser errors require zero searches. Resolved requests,
  including a failed provider read, require exactly one attempt; no silent retry.

Each scenario records expected and observed provider calls. Optional full
`expected_candidates` goldens distinguish ranking ties with identical dish names.
The runner remains an in-process route evaluation, not an HTTP status recorder:
a malformed returned object is recorded as a failed response contract even when
the route function returned normally. API tests separately exercise actual HTTP
serialization and its sanitized 500 response.

Natural-language eligibility checks use the fixture's independently expected
constraints, not the parser output. A parser that drops an exclusion cannot
make its own incorrect recommendation appear valid. Any recommendation for an
expected unresolved intent is also a hard-constraint violation.

The report includes SHA-256 hashes of catalog and scenario bytes, and excludes
timestamps, durations, and random request IDs so repeated runs reproduce the
same output. Application version, execution mode, and implementation/test source
hashes identify the actual working tree used. Exit code is 0 for all passing scenarios, 1 for failed scenarios,
or undetected seeded faults, and 2 for invalid scenario files.

Every evaluation also runs a deterministic fault-injection audit. Its ten
deliberately incorrect implementations must be rejected by the specific relevant
invariant, not an unrelated exception. `fault_injection` in the report lists the
defects, required checks, observed failures, and provider-call traces. Expected
audit failures are separate from scenario failures. A test disables evaluation
checks entirely and verifies that the audit then reports all ten faults missed.
This is a small, explicit audit using standard-library mocks, not a mutation
testing framework or a claim of exhaustive correctness.

Review catalog changes and expected-result changes together. This baseline checks
local recommendation behavior, not live provider availability or commerce flows.

## Current limits and next milestone

The server is a local, unauthenticated recommendation API bound to loopback in
the command above. Natural-language interpretation supports only the grammar
above; general language understanding, dietary certification, allergen safety,
and commerce actions are not implemented. There are no cart,
payment, order, or booking routes. Catalog prices are mock facts, not live quotes.

See `docs/swiggy-integration.md` for the future adapter contract, official sources,
and unresolved authentication guidance. Access and credentials remain pending.
The next milestone is an explicitly authorized real-model evaluation of the optional
Gemini adapter and a review of broader semantic coverage. A real
commerce provider requires a separately authorized integration task.

## Optional Gemini parser (v0.2.0)

Flow: text -> independent capability guard -> configured parser -> shared strict
intent schema and independent meaning comparison -> mock catalog -> complete
catalog validation -> deterministic eligibility and ranking -> response.
`/intent`, `/recommend/text`, and `/recommend` keep their response contracts.

Default `MEALPILOT_INTENT_BACKEND=deterministic` uses the original local grammar.
Only `gemini` enables external interpretation; unknown backends fail closed.
Gemini uses the official generateContent REST API via Python's standard library;
requirements.txt is unchanged, and no package installation is required.
The request uses a JSON schema derived from ParsedIntent, but local validation
and semantic checks remain authoritative. No tool execution is offered to the model.

Set process environment variables in PowerShell before starting the server:

```powershell
$env:MEALPILOT_INTENT_BACKEND = "gemini"
$env:GEMINI_API_KEY = "<your API key>"
$env:MEALPILOT_GEMINI_MODEL = "<available Gemini model supporting structured output>"
$env:MEALPILOT_LLM_TIMEOUT_SECONDS = "10"
.\.venv\Scripts\python.exe -B -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Select the model explicitly using your API account's availability; there is no
implicit model default or fallback. A consumer AI subscription does not establish
API credentials, API credits, or quota. `.env.example` contains safe examples;
.env files are not automatically loaded. In Gemini mode supported user text is
sent to Google. Keep keys out of source, shell history, logs and shared reports.
To return to local mode, set MEALPILOT_INTENT_BACKEND to deterministic.

Semantic coverage is deliberately conservative. Gemini mode additionally accepts
whole-phrase alternatives: `my spending limit is`, `I can spend at most`,
`do not spend more than`, `don't spend more than` for budget; `do not include`,
`don't include`, `leave out`, `keep out` for exclusions; `it must contain`,
`make sure it contains` for required ingredients; `I would prefer` for preferences.
All remaining wording must pass the original grammar's full-token coverage check.
This is a limited English expansion, not general language understanding. Unknown
clauses, ambiguous scope, optional requirements and unsupported mandatory claims
stop before model/provider access. A model's explicit_fields cannot certify meaning:
the complete semantic key must agree with independent interpretation, including
strict budget boundaries, ingredient sets and preferences. Contradictory required
and excluded ingredients are retained; deterministic eligibility returns no matches.

Missing configuration, rate limits, authentication/network failures and refusals
produce sanitized 503 responses. Invalid/truncated JSON, missing response fields,
schema errors or changed meaning produce 502. Transport timeouts produce 504.
There are no automatic retries and no silent local fallback. The transport passes
a socket inactivity timeout to urllib, disables redirects to protect the key,
and bounds response bytes. This is not a total wall-clock deadline, DNS deadline,
or proof of remote cancellation. Slow trickle responses may exceed the timeout
in total duration. No client-disconnect cancellation guarantee is made.

### Offline verification

No credentials or SDK are needed. Tests force the default parser locally and
inject fake Gemini clients; the timeout test uses only a loopback HTTP server.
The evaluator never constructs a live client from environment configuration.

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_gemini.py -v
.\.venv\Scripts\python.exe -B -m evaluation --output evaluation_report.json
.\.venv\Scripts\python.exe -B -m evaluation --scenarios tests/fixtures/llm_cases.json --output llm_evaluation_report.json
 git diff --check
```

The same commands work with `py -3.14` when that selected interpreter has the
application dependencies installed. Original 51 regression expectations remain
unchanged. The separate nine LLM fixtures use recorded synthetic outputs through
the real adapter and preserve independent constraint, grounding and provider-call
oracles. Fault injection still requires all ten intended detecting invariants.
These are adapter contract results, not real-model accuracy measurements. No real
model evaluation or model call was performed; access, model/schema compatibility,
real quotas and live reliability remain unverified. A live evaluation must be an
explicit separate task with reviewed prompts and independent expected constraints.

Official contracts consulted for this adapter:
[Google REST generateContent](https://ai.google.dev/api/generate-content),
[structured output](https://ai.google.dev/gemini-api/docs/structured-output),
[API keys](https://ai.google.dev/gemini-api/docs/api-key),
[errors](https://ai.google.dev/gemini-api/docs/troubleshooting),
[Google SDK reference](https://googleapis.github.io/python-genai/), and
[urllib timeouts](https://docs.python.org/3/library/urllib.request.html).

## Controlled real-model benchmark (v0.2.1)

`live_evaluation.py` is separate from normal evaluation. No real-model results
exist from implementation: all harness tests use fake clients. The 25 synthetic
reference cases in `tests/fixtures/live_gemini_cases.json` have agent-authored
literal product-rule labels, with hard constraints, soft preferences,
clarification conditions and unsupported requirements separated. They were not
harvested from benchmark model responses or parser output. The requested
human-authored reference set remains pending independent human review; automated
policy agreement does not establish human authorship or approval. All reference
fields, including unresolved-case facts and issue codes, are strictly validated
before confirmation or client creation. A case-by-case review table is in the
verification document.

Run all offline checks without credentials:

```powershell
py -3.14 -B -m unittest discover -s tests -v
py -3.14 -B -m evaluation --output evaluation_report.json
py -3.14 -B -m evaluation --scenarios tests/fixtures/llm_cases.json --output llm_evaluation_report.json
git diff --check
```

Only after reviewing the labels and deciding to spend API quota, configure
`GEMINI_API_KEY` through your local secrets process, explicitly select
`MEALPILOT_GEMINI_MODEL`, and optionally set `MEALPILOT_LLM_TIMEOUT_SECONDS`.
Then the following **live command sends synthetic prompts to Google**:

```powershell
py -3.14 -B -m live_evaluation --live --max-calls 25 --output build/gemini-live-report.json
```

Both `--live` and a maximum-call budget of 1..25 are mandatory. The runner displays
the plan before any request and requires an exact confirmation (`RUN 15` for the
current fixture). There is no confirmation bypass. Fifteen prompts currently pass
the application gate; ten are evaluated locally without a model call. A smaller
budget limits eligible requests in fixture order. An existing output file is
refused; missing parent directories are created after confirmation. No automatic
retries or meal-provider/commerce calls occur. Authentication
or quota/rate-limit failure stops immediately; three consecutive attempted
transport failures stop the run. Completed cases and caught interruptions are
saved incrementally, with unrun case IDs listed.

Reports distinguish model interpretations before semantic rejection from the
validated application outcome. They retain exact-match counts, hard-constraint
precision/recall, omitted/changed reference facts, incorrect acceptance/rejection,
schema-valid semantic errors, and separate policy/clarification/unsupported metrics.
Every ratio includes its denominator; transport failures are excluded from model
accuracy and reported separately. Policy-blocked cases never count as evidence
of Gemini understanding. Client latency and trustworthy reported token counts
include coverage information; cost is unavailable. Reports omit credentials,
headers, raw responses and exception messages. A small guarded benchmark cannot
establish broad natural-language accuracy or production reliability.
Missing token usage has a null total and a zero coverage denominator, rather
than an apparent zero-token result. Per-case scores identify the observed outcome
and changed soft-preference field names without persisting arbitrary model text.

The adapter stays on generateContent; no protocol migration is included. Model
availability/schema compatibility and live behavior remain unverified. See
[v0.2.1 verification](docs/v0.2.1-verification.md) for the actual offline results,
metric definitions, official compatibility references and an example report
explicitly marked as simulated. No live run should occur implicitly in tests,
normal deterministic evaluation or implementation work.
