"""Offline, deterministic evaluation of the real request model and route logic.

This is a regression oracle, not a production recommendation dependency. HTTP
middleware and serialization are exercised separately by the API tests.
"""

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import app, MealRequest, get_recommendations, get_text_recommendations
from app.intent import DeterministicIntentParser, ParserUnavailable
from app.gemini import GeminiIntentParser
from app.mock_mcp import DATA_FILE
from app.providers import MockMealProvider, ProviderTimeout, ProviderUnavailable
from app.schemas import MealIntent, NaturalLanguageRequest, UnresolvedIntent


DEFAULT_SCENARIOS = Path(__file__).parent / "tests" / "fixtures" / "recommendation_cases.json"
EVIDENCE_FILES = (
    "app/gemini.py", "tests/test_gemini.py",
    "app/main.py", "app/intent.py", "app/schemas.py", "app/candidates.py",
    "app/optimizer.py", "app/providers.py", "app/mock_mcp.py", "evaluation.py",
    "tests/test_v013_regressions.py", "tests/test_evaluation.py", "tests/test_api.py",
    "tests/test_candidates.py", "tests/test_intent.py", "tests/test_optimizer.py",
)


class ScenarioProvider:
    name = "mock_mcp"

    def __init__(self, specification: dict | None):
        self.specification = specification
        self.calls = 0
        self.catalog = None
        self.completed = False

    def search_meals(self) -> list[dict]:
        self.calls += 1
        if self.specification is None:
            records = MockMealProvider().search_meals()
        else:
            failure = self.specification.get("failure")
            if failure == "unavailable":
                raise ProviderUnavailable("Simulated unavailable catalog")
            if failure == "timeout":
                raise ProviderTimeout("Simulated timeout")
            records = deepcopy(self.specification["meals"])
        # Snapshot before production code can mutate records or return aliases.
        self.catalog = deepcopy(records)
        self.completed = True
        return records


class ScenarioParser:
    def __init__(self, specification: dict | None):
        self.specification = specification

    def parse(self, text: str) -> object:
        if self.specification is None:
            return DeterministicIntentParser().parse(text)
        if "output" in self.specification:
            return deepcopy(self.specification["output"])
        failure = self.specification["failure"]
        if failure == "timeout":
            raise TimeoutError("Simulated parser timeout")
        if failure == "unavailable":
            raise ParserUnavailable("Simulated parser failure")
        return {"status": "parsed"}  # Deliberately malformed fixture.


class RecordedGeminiClient:
    """Explicit offline model fixture; never selects a network client."""
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": json.dumps(self.output)}],
        }}]}


def load_scenarios(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("Scenarios must be a nonempty list")
    names = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str):
            raise ValueError("Each scenario needs a name")
        if not case["name"].strip() or case["name"] in names:
            raise ValueError("Scenario names must be nonempty and unique")
        names.add(case["name"])
        if not isinstance(case.get("request"), dict):
            raise ValueError("Each scenario needs a request object")
        if not isinstance(case.get("mode", "structured"), str) or case.get("mode", "structured") not in {"structured", "text"}:
            raise ValueError("Unsupported scenario mode")
        status = case.get("expected_status", 200)
        if type(status) is not int or status not in {200, 422, 500, 502, 503, 504}:
            raise ValueError("Unsupported expected status")
        if status == 200 and not isinstance(case.get("expected"), list):
            raise ValueError("Successful scenarios need ordered expected results")
        if status == 200 and any(
            not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or type(pair[1]) is not int
            for pair in case["expected"]
        ):
            raise ValueError("Expected results must be dish/score pairs")
        if case.get("mode") == "text" and status == 200:
            if not isinstance(case.get("expected_outcome"), str) or case["expected_outcome"] not in {"parsed", "clarification_required", "unsupported"}:
                raise ValueError("Text scenarios need an expected outcome")
            if case["expected_outcome"] == "parsed" and not isinstance(case.get("expected_request"), dict):
                raise ValueError("Parsed scenarios need independently expected constraints")
            if case["expected_outcome"] == "parsed":
                if set(case["expected_request"]) != set(MealRequest.model_fields):
                    raise ValueError("Expected constraints must explicitly specify every request field")
                MealRequest.model_validate(case["expected_request"])
            if case["expected_outcome"] != "parsed" and not isinstance(case.get("expected_issue"), str):
                raise ValueError("Unresolved scenarios need an expected issue code")
        if "parser" in case:
            specification = case["parser"]
            if not isinstance(specification, dict) or set(specification) not in ({"failure"}, {"output"}, {"gemini_output"}):
                raise ValueError("Unsupported parser fixture")
            if "failure" in specification and (not isinstance(specification["failure"], str) or specification["failure"] not in {"unavailable", "timeout", "malformed"}):
                raise ValueError("Unsupported parser failure")
        if "expected_candidates" in case:
            candidates = case["expected_candidates"]
            if status != 200 or not isinstance(candidates, list) or any(catalog_key(row) is None for row in candidates):
                raise ValueError("Expected candidates must be valid catalog records")
            if [row["dish"] for row in candidates] != [pair[0] for pair in case["expected"]]:
                raise ValueError("Expected candidates and dish order disagree")
        if "provider" in case:
            provider = case["provider"]
            if not isinstance(provider, dict) or set(provider) not in ({"meals"}, {"failure"}):
                raise ValueError("Provider fixture must specify meals or failure")
            if "failure" in provider and (not isinstance(provider["failure"], str) or provider["failure"] not in {"unavailable", "timeout"}):
                raise ValueError("Unsupported simulated failure")
    return cases


CATALOG_FIELDS = ("restaurant", "dish", "price", "cuisine", "spicy", "ingredients")


def catalog_key(row: object) -> tuple | None:
    """Independent evidence check: never call production normalization here."""
    if not isinstance(row, dict):
        return None
    if any(not isinstance(row.get(field), str) or not row[field].strip() for field in ("restaurant", "dish", "cuisine")):
        return None
    if type(row.get("price")) is not int or row["price"] <= 0 or type(row.get("spicy")) is not bool:
        return None
    items = row.get("ingredients")
    if not isinstance(items, list) or not items or any(
        not isinstance(item, str) or not item.strip()
        or item.strip().casefold() in {"unknown", "n/a", "not available", "unspecified"}
        for item in items
    ):
        return None
    if "ingredients_complete" in row and row["ingredients_complete"] is not True:
        return None
    return (row["restaurant"], row["dish"], row["price"], row["cuisine"], row["spicy"], tuple(sorted(items)))


def grounding_violations(rows: list[dict], provider: ScenarioProvider) -> int:
    if not provider.completed or not isinstance(provider.catalog, list):
        return max(1, len(rows))
    keys = [catalog_key(row) for row in provider.catalog]
    if any(key is None for key in keys):
        return max(1, len(rows))  # Partial acceptance of a corrupt catalog is forbidden.
    remaining = Counter(keys)
    violations = 0
    for row in rows:
        key = catalog_key(row)
        if key is None or remaining[key] <= 0:
            violations += 1
        else:
            remaining[key] -= 1
    return violations


def inspect_response(body: object, source: str) -> tuple[list[dict], bool]:
    """Inspect function output without relying on the production response model."""
    if not isinstance(body, dict) or not isinstance(body.get("recommendations"), list):
        return [], False
    valid = set(body) == {"count", "recommendations", "source"}
    valid &= type(body.get("count")) is int and body["count"] == len(body["recommendations"])
    valid &= body.get("source") == source
    rows = []
    for row in body["recommendations"]:
        if not isinstance(row, dict):
            valid = False
            continue
        rows.append(row)
        reasons = row.get("reasons")
        valid &= set(row) == set(CATALOG_FIELDS) | {"score", "reasons"}
        valid &= catalog_key(row) is not None and type(row.get("score")) is int and row["score"] >= 0
        valid &= isinstance(reasons, list) and bool(reasons) and all(isinstance(reason, str) and bool(reason.strip()) for reason in reasons)
    return rows, bool(valid)


def count_constraint_violations(rows: list[dict], request: dict) -> int:
    """Independent oracle: do not call the production eligibility implementation."""
    excluded = {item.strip().casefold() for item in request.get("avoid_ingredients", [])}
    required = {item.strip().casefold() for item in request.get("required_ingredients", [])}
    violations = 0
    for row in rows:
        price = row.get("price")
        ingredients = row.get("ingredients")
        if type(price) is not int or price <= 0 or price > request["budget"]:
            violations += 1
        if not isinstance(ingredients, list) or not ingredients or any(
            not isinstance(item, str) or not item.strip() for item in ingredients
        ):
            violations += 1
            continue
        actual = {item.strip().casefold() for item in ingredients}
        violations += bool(excluded.intersection(actual))
        violations += not required.issubset(actual)
    return violations


def evaluate_scenario(case: dict) -> dict:
    expected = {"status": case.get("expected_status", 200)}
    if expected["status"] == 200:
        expected["recommendations"] = case["expected"]
    if "expected_detail" in case:
        expected["detail"] = case["expected_detail"]
    is_text = case.get("mode") == "text"
    if is_text and expected["status"] == 200:
        expected["outcome"] = case["expected_outcome"]
        if "expected_request" in case:
            expected["intent_request"] = case["expected_request"]
        if "expected_issue" in case:
            expected["issue"] = case["expected_issue"]
    if "expected_candidates" in case:
        expected["candidates"] = case["expected_candidates"]
    expected["provider_calls"] = int(not (
        expected["status"] == 422
        or (is_text and case.get("expected_outcome") in {"clarification_required", "unsupported"})
        or (is_text and "parser" in case and expected["status"] != 200)
    ))
    rows = []
    unexpected_error = False
    response_valid = True
    returned_recommendations = False
    provider = ScenarioProvider(case.get("provider"))
    try:
        model = NaturalLanguageRequest if is_text else MealRequest
        request = model.model_validate(case["request"])
    except ValidationError:
        actual = {"status": 422}
    else:
        try:
            if is_text:
                specification = case.get("parser", {})
                parser = (GeminiIntentParser(client=RecordedGeminiClient(specification["gemini_output"]),
                                            api_key="offline-fixture", model="gemini-offline")
                          if "gemini_output" in specification else ScenarioParser(case.get("parser")))
                body = get_text_recommendations(request, parser, provider)
            else:
                body = get_recommendations(request, provider)
        except HTTPException as error:
            actual = {"status": error.status_code, "detail": error.detail}
        except Exception:
            actual = {"status": 500, "detail": "Internal server error"}
            unexpected_error = True
        else:
            actual = {"status": 200, "recommendations": []}
            if is_text and isinstance(body, UnresolvedIntent):
                actual.update(outcome=body.status, issue=body.issues[0].code)
            else:
                recommendation_body = body
                if is_text:
                    response_valid = isinstance(body, dict) and set(body) == {"status", "intent", "result"} and body.get("status") == "parsed"
                    try:
                        intent = MealIntent.model_validate(body.get("intent") if isinstance(body, dict) else None)
                    except ValidationError:
                        response_valid = False  # A bad response is not a client 422.
                    else:
                        actual.update(outcome=body.get("status"), intent_request=intent.to_request().model_dump())
                    recommendation_body = body.get("result") if isinstance(body, dict) else None
                rows, valid = inspect_response(recommendation_body, provider.name)
                response_valid &= valid
                returned_recommendations = True
                actual["recommendations"] = [
                    [row.get("dish") if isinstance(row.get("dish"), str) else "<invalid>",
                     row.get("score") if type(row.get("score")) is int else None]
                    for row in rows
                ]
    actual["provider_calls"] = provider.calls
    if "expected_candidates" in case:
        actual["candidates"] = [
            {field: row[field] for field in CATALOG_FIELDS} if catalog_key(row) is not None else {"invalid": True}
            for row in rows
        ]
    grounded = grounding_violations(rows, provider) if returned_recommendations else 0

    # Use the fixture's expected constraints, never the parser's possibly wrong
    # output, as the independent hard-constraint oracle for language scenarios.
    oracle_request = case.get("expected_request", {}) if is_text else case["request"]
    violations = 0
    if rows and actual["status"] == 200:
        if is_text and case.get("expected_outcome") != "parsed":
            violations = len(rows)  # Any recommendation for unresolved intent is unsafe.
        else:
            violations = count_constraint_violations(rows, oracle_request)
    failures = []
    if actual["status"] != expected["status"]:
        failures.append("status")
    if actual["provider_calls"] != expected["provider_calls"]:
        failures.append("provider_access")
    if not response_valid:
        failures.append("response_contract")
    if grounded:
        failures.append("catalog_grounding")
    if expected["status"] == actual["status"] == 200 and actual["recommendations"] != expected["recommendations"]:
        failures.append("ranking")
    if "candidates" in expected and actual["candidates"] != expected["candidates"] and "ranking" not in failures:
        failures.append("ranking")
    if "detail" in expected and actual.get("detail") != expected["detail"]:
        failures.append("detail")
    if is_text and any(actual.get(field) != expected[field] for field in ("outcome", "intent_request", "issue") if field in expected):
        failures.append("intent")
    if violations:
        failures.append("hard_constraints")
    if unexpected_error:
        failures.append("unexpected_error")
    if failures and is_text and ("parser" in case or str(actual.get("detail", "")).startswith("Intent parser")):
        failures.append("parser")
    return {
        "name": case["name"], "expected": expected, "actual": actual,
        "passed": not failures, "failures": failures,
        "hard_constraint_violations": violations,
        "catalog_grounding_violations": grounded,
    }


def run_evaluation(scenarios: Path = DEFAULT_SCENARIOS) -> dict:
    results = [evaluate_scenario(case) for case in load_scenarios(scenarios)]
    failed = [result for result in results if not result["passed"]]
    return {
        "application_version": app.version,
        "execution_mode": "in_process_route_functions",
        "implementation_sha256": {
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in EVIDENCE_FILES
        },
        "catalog_sha256": hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(),
        "scenarios_sha256": hashlib.sha256(scenarios.read_bytes()).hexdigest(),
        "summary": {
            "total_scenarios": len(results), "passed": len(results) - len(failed),
            "failed": len(failed),
            "hard_constraint_violations": sum(row["hard_constraint_violations"] for row in results),
            "catalog_grounding_violations": sum(row["catalog_grounding_violations"] for row in results),
            "response_contract_failures": sum("response_contract" in row["failures"] for row in results),
            "provider_access_failures": sum("provider_access" in row["failures"] for row in results),
            "ranking_failures": sum("ranking" in row["failures"] for row in results),
            "provider_failures": sum(max(row["expected"]["status"], row["actual"]["status"]) >= 500 and "parser" not in row["failures"] for row in failed),
            "validation_failures": sum(422 in (row["expected"]["status"], row["actual"]["status"]) for row in failed),
            "unexpected_errors": sum("unexpected_error" in row["failures"] for row in results),
            "intent_failures": sum("intent" in row["failures"] for row in results),
            "parser_failures": sum("parser" in row["failures"] for row in results),
            "parsed_intents": sum(row["actual"].get("outcome") == "parsed" for row in results),
            "clarification_required": sum(row["actual"].get("outcome") == "clarification_required" for row in results),
            "unsupported_intents": sum(row["actual"].get("outcome") == "unsupported" for row in results),
        },
        "scenarios": results,
        "fault_injection": run_fault_audit(),
    }


def run_fault_audit() -> dict:
    """Seed named defects and require the relevant invariant to detect each one.

    Only this module's local route references are patched; API handlers and mock
    files are untouched. An unrelated exception never counts as detection.
    """
    base = {
        "name": "audit", "request": {"budget": 200},
        "expected": [["Vegetable Fried Rice", 0], ["Paneer Rice Bowl", 0]],
    }
    exclusion = {**base, "request": {"budget": 300, "avoid_ingredients": ["chicken"]}}
    unresolved = {
        "name": "audit_unresolved", "mode": "text", "request": {"text": "vegan budget 200"},
        "expected_outcome": "unsupported", "expected_issue": "unsupported_dietary", "expected": [],
    }
    timeout = {"name": "audit_timeout", "request": {"budget": 200}, "provider": {"failure": "timeout"}, "expected_status": 504}
    row = {"restaurant": "Synthetic", "dish": "Bowl", "price": 100, "cuisine": "Indian", "spicy": False, "ingredients": ["rice"]}
    corrupt = {
        "name": "audit_partial", "request": {"budget": 200},
        "provider": {"meals": [row, {"dish": "Missing mandatory fields"}]}, "expected_status": 502,
    }
    specifications = [
        ("over_budget", base, "hard_constraints"),
        ("excluded_ingredient", exclusion, "hard_constraints"),
        ("fabricated_price", base, "catalog_grounding"),
        ("duplicate_candidate", base, "catalog_grounding"),
        ("incorrect_count", base, "response_contract"),
        ("reversed_ranking", base, "ranking"),
        ("search_for_unresolved", unresolved, "provider_access"),
        ("retry_after_timeout", timeout, "provider_access"),
        ("skip_incomplete_record", corrupt, "catalog_grounding"),
        ("malformed_response", base, "response_contract"),
    ]
    original_recommend = get_recommendations
    original_text = get_text_recommendations
    results = []
    for name, case, invariant in specifications:
        def faulty_recommend(request, provider):
            if name == "retry_after_timeout":
                try:
                    return original_recommend(request, provider)
                except HTTPException:
                    return original_recommend(request, provider)
            if name == "skip_incomplete_record":
                records = provider.search_meals()
                return {"count": 1, "source": provider.name, "recommendations": [{**records[0], "score": 0, "reasons": ["Meets your hard constraints"]}]}
            body = original_recommend(request, provider)
            rows = body["recommendations"]
            if name in {"over_budget", "excluded_ingredient"}:
                rows.append({**deepcopy(provider.catalog[0]), "score": 0, "reasons": ["Meets your hard constraints"]})
                body["count"] = len(rows)
            elif name == "fabricated_price":
                rows[0]["price"] = 1
            elif name == "duplicate_candidate":
                rows.append(deepcopy(rows[0]))
                body["count"] = len(rows)
            elif name == "incorrect_count":
                body["count"] = 999
            elif name == "reversed_ranking":
                rows.reverse()
            elif name == "malformed_response":
                body["recommendations"] = [None]
            return body

        def faulty_text(request, parser, provider):
            provider.search_meals()
            return original_text(request, parser, provider)

        with patch(__name__ + ".get_recommendations", side_effect=faulty_recommend):
            if name == "search_for_unresolved":
                with patch(__name__ + ".get_text_recommendations", side_effect=faulty_text):
                    observation = evaluate_scenario(case)
            else:
                observation = evaluate_scenario(case)
        detected = not observation["passed"] and invariant in observation["failures"]
        results.append({
            "fault": name, "required_detection": invariant, "detected": detected,
            "observed_failures": observation["failures"],
            "expected_provider_calls": observation["expected"]["provider_calls"],
            "actual_provider_calls": observation["actual"]["provider_calls"],
        })
    detected = sum(result["detected"] for result in results)
    return {"total": len(results), "detected": detected, "undetected": len(results) - detected, "cases": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--output", type=Path, help="Also write the JSON report to this file")
    arguments = parser.parse_args(argv)
    try:
        report = run_evaluation(arguments.scenarios)
    except (OSError, ValueError) as error:
        parser.error(f"Cannot evaluate scenario file: {type(error).__name__}")
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.write_text(output, encoding="utf-8")
    print(output, end="")
    return int(report["summary"]["failed"] > 0 or report.get("fault_injection", {}).get("undetected", 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())
