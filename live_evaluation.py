"""Explicitly confirmed synthetic Gemini benchmark; never used by normal evaluation."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from app.gemini import GeminiIntentParser, GeminiRestClient, GeminiTransportUnavailable, policy_reference
from app.intent import DeterministicIntentParser, InvalidParserOutput, ParserUnavailable, parse_intent
from app.schemas import (HardConstraints, IntentIssue, MealRequest, NaturalLanguageRequest,
                         ParsedIntent, SoftPreferences, StrictModel)

FIXTURE = Path(__file__).parent / "tests/fixtures/live_gemini_cases.json"
SOURCE_FILES = ("live_evaluation.py", "app/gemini.py", "app/intent.py", "app/schemas.py")


class ReferenceHardConstraints(HardConstraints):
    # A missing/ambiguous budget stays unknown, never a guessed default.
    budget: int | None = Field(..., gt=0)
    required_ingredients: list[str] = Field(...)
    avoid_ingredients: list[str] = Field(...)


class ReferenceSoftPreferences(SoftPreferences):
    preferred_cuisine: str | None = Field(...)
    want_spicy: bool = Field(...)


class ReferenceCase(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    text: str
    categories: list[str] = Field(min_length=1)
    expected_outcome: Literal["parsed", "clarification_required", "unsupported"]
    hard_constraints: ReferenceHardConstraints
    soft_preferences: ReferenceSoftPreferences
    clarification_conditions: list[str]
    unsupported_requirements: list[str]
    expected_issue: str | None = Field(...)

    @model_validator(mode="after")
    def validate_reference(self):
        NaturalLanguageRequest(text=self.text)
        for labels in (self.categories, self.clarification_conditions, self.unsupported_requirements):
            if any(not label.strip() for label in labels):
                raise ValueError("Blank reference label")
        if self.expected_outcome == "parsed":
            expected_intent(self.model_dump())
            if self.expected_issue or self.clarification_conditions or self.unsupported_requirements:
                raise ValueError("Conflicting parsed reference labels")
        else:
            issue = IntentIssue(code=self.expected_issue, message="Reference label")
            unsupported = self.expected_outcome == "unsupported"
            if unsupported != issue.code.startswith("unsupported_"):
                raise ValueError("Issue code conflicts with reference outcome")
            if unsupported:
                if not self.unsupported_requirements or self.clarification_conditions:
                    raise ValueError("Conflicting unsupported reference labels")
            elif not self.clarification_conditions or self.unsupported_requirements:
                raise ValueError("Conflicting clarification reference labels")
        return self


class ReferenceFixture(StrictModel):
    label_provenance: str = Field(min_length=1)
    cases: list[ReferenceCase] = Field(min_length=20, max_length=30)


def expected_intent(case):
    """Construct from explicit reference labels, never the parser under test."""
    request = MealRequest.model_validate({**case["hard_constraints"], **case["soft_preferences"]})
    fields = [name for name, value in request.model_dump().items() if value]
    return ParsedIntent.model_validate({"status": "parsed", "intent": {
        "hard_constraints": case["hard_constraints"], "soft_preferences": case["soft_preferences"],
        "explicit_fields": sorted(fields),
    }})


def load_cases():
    document = ReferenceFixture.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    cases = [case.model_dump() for case in document.cases]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate benchmark IDs")
    return cases


def hard_atoms(hard):
    return ({("budget", hard.budget)}
            | {("required", v.strip().casefold()) for v in hard.required_ingredients}
            | {("excluded", v.strip().casefold()) for v in hard.avoid_ingredients})


def score(case, result):
    expected_parsed = case["expected_outcome"] == "parsed"
    parsed = isinstance(result, ParsedIntent)
    exact = (parsed and expected_parsed and
             result.intent.semantic_key() == expected_intent(case).intent.semantic_key())
    if not expected_parsed:
        exact = (result is not None and not parsed and result.status == case["expected_outcome"]
                 and result.issues[0].code == case["expected_issue"])
    gold = hard_atoms(expected_intent(case).intent.hard_constraints) if expected_parsed else set()
    predicted = hard_atoms(result.intent.hard_constraints) if parsed else set()
    preference_changes = []
    if expected_parsed and parsed:
        reference_key = expected_intent(case).intent.semantic_key()
        result_key = result.intent.semantic_key()
        preference_changes = [name for name, index in (("preferred_cuisine", 3), ("want_spicy", 4))
                              if reference_key[index] != result_key[index]]
    return {"exact_match": bool(exact), "schema_valid": result is not None,
            "observed_outcome": result.status if result is not None else None,
            "changed_soft_preference_fields": preference_changes,
            "schema_valid_semantically_incorrect": result is not None and not exact,
            "incorrect_acceptance": parsed and not expected_parsed,
            "incorrect_rejection": expected_parsed and not parsed,
            "hard_true_positive": len(gold & predicted) if expected_parsed else 0,
            "hard_predicted": len(predicted) if expected_parsed else 0,
            "hard_expected": len(gold),
            "omitted_or_changed_reference_atoms": sorted([list(atom) for atom in gold - predicted]),
            # Persist only known reference atoms and counts, never arbitrary model strings.
            "hard_omitted_or_changed": len(gold - predicted),
            "hard_extra_or_changed": len(predicted - gold) if expected_parsed else 0}


class ObservedClient:
    def __init__(self, delegate):
        self.delegate = delegate
        self.attempted = False
        self.received = False
        self.envelope = None
        self.latency_ms = None

    def generate(self, **kwargs):
        self.attempted = True
        start = time.perf_counter()
        try:
            self.envelope = self.delegate.generate(**kwargs)
            self.received = True
            return self.envelope
        except InvalidParserOutput:
            # Malformed HTTP response is an accuracy/output failure, not transport loss.
            self.received = True
            raise
        finally:
            self.latency_ms = round((time.perf_counter() - start) * 1000, 3)


def fraction(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def summarize(rows, planned):
    model = [r for r in rows if r["attempted"] and r["received"]]
    policy = [r for r in rows if not r["attempted"]]
    attempted = sum(r["attempted"] for r in rows)
    metrics = {
        "planned_calls": planned, "attempted_calls": attempted,
        "successful_calls": len(model), "failed_calls": attempted - len(model),
        "exact_intent_matches": fraction(sum(r["model_score"]["exact_match"] for r in model), len(model)),
        "hard_precision": fraction(sum(r["model_score"]["hard_true_positive"] for r in model),
                                   sum(r["model_score"]["hard_predicted"] for r in model)),
        "hard_recall": fraction(sum(r["model_score"]["hard_true_positive"] for r in model),
                                sum(r["model_score"]["hard_expected"] for r in model)),
        "policy_only_matches": fraction(sum(r["application_score"]["exact_match"] for r in policy), len(policy)),
        "application_matches": fraction(sum(r["application_score"]["exact_match"] for r in rows
                                             if not r["attempted"] or r["received"]), len(model) + len(policy)),
    }
    for field in ("incorrect_acceptance", "incorrect_rejection", "schema_valid_semantically_incorrect"):
        denominator = sum(r["model_score"]["schema_valid"] for r in model) if field.startswith("schema_valid") else len(model)
        metrics[field] = fraction(sum(r["model_score"][field] for r in model), denominator)
    metrics["hard_omitted_or_changed"] = fraction(sum(r["model_score"]["hard_omitted_or_changed"] for r in model),
                                               sum(r["model_score"]["hard_expected"] for r in model))
    for outcome in ("clarification_required", "unsupported"):
        selected = [r for r in rows if r["expected_outcome"] == outcome and (not r["attempted"] or r["received"])]
        metrics[outcome + "_application_accuracy"] = fraction(sum(r["application_score"]["exact_match"] for r in selected), len(selected))
        selected_model = [r for r in model if r["expected_outcome"] == outcome]
        metrics[outcome + "_model_accuracy"] = fraction(sum(r["model_score"]["exact_match"] for r in selected_model), len(selected_model))
    usage_rows = [r for r in model if r["usage"]]
    metrics["usage_reported_calls"] = fraction(len(usage_rows), len(model))
    metrics["reported_token_totals"] = {
        name: {"sum": (sum(r["usage"][name] for r in usage_rows if name in r["usage"])
                       if any(name in r["usage"] for r in usage_rows) else None),
               "denominator_calls": sum(name in r["usage"] for r in usage_rows)}
        for name in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
    }
    return metrics


def save_report(path, report):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv=None, *, client_factory=GeminiRestClient, confirm=input):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--live", action="store_true", help="Explicit authorization for this invocation only")
    cli.add_argument("--max-calls", type=int, required=True, help="Required request budget, 1..25")
    cli.add_argument("--output", type=Path, required=True, help="New report path; existing files are refused")
    args = cli.parse_args(argv)
    if not args.live or not 1 <= args.max_calls <= 25:
        cli.error("--live and an explicit --max-calls from 1 to 25 are required")
    key = os.environ.get("GEMINI_API_KEY", "")
    model = os.environ.get("MEALPILOT_GEMINI_MODEL", "")
    try:
        timeout = float(os.environ.get("MEALPILOT_LLM_TIMEOUT_SECONDS", "10"))
    except ValueError:
        cli.error("Invalid timeout configuration")
    if not key.strip() or not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", model) or not math.isfinite(timeout) or not 0.01 <= timeout <= 60:
        cli.error("API key, explicit model and valid timeout are required")
    try:
        cases = load_cases()
    except (OSError, ValueError):
        cli.error("Invalid synthetic benchmark fixture; no requests made")
    eligible = sum(DeterministicIntentParser().parse(policy_reference(c["text"]))["status"] == "parsed" for c in cases)
    planned = min(args.max_calls, eligible)
    print(f"Synthetic cases: {len(cases)}. Planned Gemini requests: {planned}. Maximum calls: {args.max_calls}.")
    print("The application will block other cases before model access. No retries. No commerce or meal-provider calls.")
    try:
        answer = confirm(f"Type RUN {planned} to confirm these requests: ")
    except EOFError:
        return 2
    if answer != f"RUN {planned}":
        print("Cancelled; no requests made.")
        return 2
    # Refuse overwriting any source, fixture or previous report before creating a client.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")
    report = {"harness_version": "0.2.1", "mode": "explicit_live_benchmark", "status": "running", "model": model,
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "configuration": {"timeout_seconds": timeout, "timeout_kind": "socket_inactivity",
                                "max_calls": args.max_calls, "retries": 0, "protocol": "generateContent/v1beta"},
              "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
              "source_sha256": {f: hashlib.sha256((Path(__file__).parent / f).read_bytes()).hexdigest() for f in SOURCE_FILES},
              "cost": "unavailable", "cases": [], "notes": ["Policy-blocked cases are not model accuracy evidence.",
              "No raw responses, headers, credentials or exception messages are persisted."]}
    rows = report["cases"]
    def persist():
        report["summary"] = summarize(rows, planned)
        report["not_run_ids"] = [c["id"] for c in cases if c["id"] not in {r["id"] for r in rows}]
        save_report(args.output, report)
    persist()
    failures = 0
    try:
        delegate = client_factory()
        for case in cases:
            eligible_case = DeterministicIntentParser().parse(policy_reference(case["text"]))["status"] == "parsed"
            if eligible_case and sum(r["attempted"] for r in rows) >= args.max_calls:
                continue
            observer = ObservedClient(delegate)
            parser = GeminiIntentParser(client=observer, api_key=key, model=model, timeout=timeout)
            application, error_kind, stop = None, None, None
            try:
                application = parse_intent(case["text"], parser)
            except GeminiTransportUnavailable as error:
                error_kind = "transport"
                if error.status in (401, 403):
                    stop = "authentication"
                elif error.status in (402, 429):
                    stop = "quota_or_rate_limit"
            except TimeoutError:
                error_kind = "timeout"
            except InvalidParserOutput:
                error_kind = "invalid_or_semantically_rejected_output"
            except ParserUnavailable:
                error_kind = "refusal_or_configuration"
            except KeyboardInterrupt:
                error_kind, stop = "interrupted", "interrupted"
            except Exception:
                error_kind, stop = "unexpected_failure", "unexpected_failure"
            raw = None
            if observer.received:
                try:
                    raw = GeminiIntentParser.decode_response(observer.envelope)
                except (ParserUnavailable, InvalidParserOutput):
                    pass
            usage = observer.envelope.get("usageMetadata", {}) if isinstance(observer.envelope, dict) else {}
            usage = {k: usage[k] for k in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount")
                     if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            rows.append({"id": case["id"], "expected_outcome": case["expected_outcome"],
                         "attempted": observer.attempted, "received": observer.received,
                         "latency_ms": observer.latency_ms, "usage": usage or None,
                         "failure_kind": error_kind, "application_score": score(case, application),
                         "model_score": score(case, raw) if observer.received else None,
                         "provider_calls": 0})
            if observer.attempted:
                failures = failures + 1 if not observer.received else 0
            if failures >= 3:
                stop = "three_consecutive_transport_failures"
            report["status"] = "stopped_" + stop if stop else "running"
            persist()
            if stop:
                break
        else:
            report["status"] = "completed"
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except Exception:
        report["status"] = "stopped_unexpected_failure"
    finally:
        persist()
    print(f"Report saved. Status: {report['status']}. Attempted: {report['summary']['attempted_calls']}.")
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
