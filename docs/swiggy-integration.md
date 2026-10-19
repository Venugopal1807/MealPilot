# Future Swiggy adapter boundary

No Swiggy transport, authentication, or commerce calls are implemented. Public
documentation was consulted on 2026-10-18; credentials and access remain pending.
The `.md` fetches were unavailable through the documentation fetch tool, so the
corresponding rendered pages were read.

## Official references and direct developer flow

- [Authentication](https://mcp.swiggy.com/builders/docs/start/authenticate.md)
  ([rendered](https://mcp.swiggy.com/builders/docs/start/authenticate/)) documents
  OAuth 2.1, PKCE S256, dynamic client registration, authorization-code exchange,
  bearer tokens, and rotating refresh tokens. Callback details and secure storage
  must be configured before implementing this flow. Use returned expiry values,
  rather than hardcoding token lifetimes. Never record tokens in diagnostics.
- [Tool reference](https://mcp.swiggy.com/builders/docs/reference/) groups servers
  separately. Select the intended server and verify its endpoint and tool schemas
  before adding a call; the existing mock names are not a live transport contract.
- [Error reference](https://mcp.swiggy.com/builders/docs/reference/errors/)
  describes tool failures and transport signals. Do not invent symbolic error
  fields or assume transport success means tool success.
- [Production guidance](https://mcp.swiggy.com/builders/docs/build/ship-to-production/)
  distinguishes retryable reads from order operations and calls for correlation
  IDs. Uncertain side effects require reconciliation before another attempt.

Documentation discrepancy: authentication recommends refreshing after 401 and
reauthorizing if refresh fails. The production and error references instead say
to rerun OAuth. Authentication also discusses expired access tokens alongside a
warning that expired tokens cannot be refreshed. Verify the current refresh
contract and token roles before implementing; no resolution is assumed here.

## MealPilot responsibilities

Implement any future adapter behind `MealProvider.search_meals()`. Map external
records into the six `MealCandidate` fields. `normalize_candidates()` checks that
contract before the optimizer uses it. Do not infer missing ingredients or prices;
the current contract rejects incomplete records. Discovery tools alone must not
be assumed to provide every fact required for recommendation eligibility.

The API owns request schemas and status translation. The domain owns budget,
ingredient eligibility, scoring, and ranking. Vendor payloads, tokens, transport,
and token-refresh state stay inside the future adapter. No OAuth placeholders or
unimplemented live-provider selector are included in this milestone.

Before implementing a real adapter, test these behaviors using recorded synthetic
fixtures and an injected transport/clock:

- Classify transport failures, tool-call failures, authentication failures, rate
  limits, deadlines, and incompatible schemas separately. Map failures to explicit
  internal exceptions; preserve the distinction between empty results and errors.
- Configure actual bounded timeouts, cancellation, and retry budgets. Honor
  documented server backoff signals. Reverify per-operation retry semantics; the
  current application performs no automatic retries.
- Refresh/re-authorize using the verified contract and latest rotated credentials.
  Do not blindly replay an operation merely because authorization was renewed.
- Carry a correlation ID through the API, adapter, and sanitized structured logs.
  The current API generates `X-Request-ID`; propagation to an external transport
  still needs implementation. Avoid raw payloads, tokens, and personal data in logs.
- Test missing/changed fields and uncertain outcomes explicitly. Generic search
  retries must never become a retry wrapper for spending or other side effects.

Future cart/order/payment/booking actions need separate application-level user
approval of concrete items and totals, plus operation-specific reconciliation.
No generic provider method in this milestone can execute those actions. A local
UUID is a correlation identifier, not evidence of remote idempotency support.
