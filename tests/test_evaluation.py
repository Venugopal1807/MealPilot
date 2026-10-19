import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluation import evaluate_scenario, load_scenarios, main, run_evaluation, run_fault_audit


class EvaluationBaselineTests(unittest.TestCase):
    def test_fixed_mock_scenarios_and_metrics(self):
        report = run_evaluation()
        self.assertEqual(report["summary"], {
            "total_scenarios": 51, "passed": 51, "failed": 0,
            "hard_constraint_violations": 0, "ranking_failures": 0,
            "provider_failures": 0, "validation_failures": 0, "unexpected_errors": 0,
            "intent_failures": 0, "parser_failures": 0, "parsed_intents": 6, "clarification_required": 9,
            "unsupported_intents": 4,
            "catalog_grounding_violations": 0, "response_contract_failures": 0,
            "provider_access_failures": 0,
        })
        self.assertTrue(all(case["passed"] for case in report["scenarios"]))
        self.assertEqual(report["fault_injection"]["undetected"], 0)
        self.assertEqual(report["application_version"], "0.1.3")
        self.assertEqual(report["execution_mode"], "in_process_route_functions")
        self.assertIn("evaluation.py", report["implementation_sha256"])
        self.assertTrue(all(len(digest) == 64 for digest in report["implementation_sha256"].values()))

    def test_reports_are_reproducible(self):
        self.assertEqual(run_evaluation(), run_evaluation())

    def test_oracle_detects_constraint_and_ranking_regressions(self):
        case = {
            "name": "regression", "request": {"budget": 100, "avoid_ingredients": ["chicken"], "required_ingredients": ["rice"]},
            "expected": [],
        }
        bad_result = {"recommendations": [{"dish": "Bad", "score": 999, "price": 101, "ingredients": ["Chicken"]}]}
        with patch("evaluation.get_recommendations", return_value=bad_result):
            result = evaluate_scenario(case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["hard_constraint_violations"], 3)
        self.assertIn("ranking", result["failures"])

    def test_cli_exit_status_and_report_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["--output", str(output)]), 0)
            self.assertEqual(json.loads(stdout.getvalue()), json.loads(output.read_text(encoding="utf-8")))
        with patch("evaluation.run_evaluation", return_value={"summary": {"failed": 1}}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 1)
        with patch("evaluation.run_evaluation", return_value={"summary": {"failed": 0}, "fault_injection": {"undetected": 1}}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main([]), 1)

    def test_invalid_scenario_files_are_rejected(self):
        invalid_cases = [[], {},
            [{"name": "case", "request": {}, "expected": [], "provider": {"failure": "unknown"}}],
            [{"name": "case", "request": {}, "expected": [], "parser": {"failure": []}}],
            [{"name": "case", "request": {}, "expected": [], "mode": []}],
            [{"name": "case", "request": {}, "expected": ["not a pair"]}],
        ]
        for cases in invalid_cases:
            with self.subTest(cases=cases), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cases.json"
                path.write_text(json.dumps(cases), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_scenarios(path)

    def test_text_oracle_uses_expected_constraints_not_parser_output(self):
        case = {
            "name": "dropped_exclusion", "mode": "text",
            "request": {"text": "budget 200 without chicken"},
            "expected_outcome": "parsed", "expected": [],
            "expected_request": {"budget": 200, "avoid_ingredients": ["chicken"], "required_ingredients": [], "preferred_cuisine": None, "want_spicy": False},
        }
        wrong = {
            "status": "parsed",
            "intent": {
                "hard_constraints": {"budget": 200, "avoid_ingredients": [], "required_ingredients": []},
                "soft_preferences": {"preferred_cuisine": None, "want_spicy": False},
                "explicit_fields": ["budget"],
            },
            "result": {"recommendations": [{"dish": "Bad", "score": 0, "price": 100, "ingredients": ["chicken"]}]},
        }
        with patch("evaluation.get_text_recommendations", return_value=wrong):
            result = evaluate_scenario(case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["hard_constraint_violations"], 1)
        self.assertIn("intent", result["failures"])

    def test_recommendations_for_unsupported_intent_are_a_failure(self):
        case = {
            "name": "unsupported", "mode": "text", "request": {"text": "vegan budget 200"},
            "expected_outcome": "unsupported", "expected_issue": "unsupported_dietary", "expected": [],
        }
        wrong = {
            "status": "parsed",
            "intent": {
                "hard_constraints": {"budget": 200, "avoid_ingredients": [], "required_ingredients": []},
                "soft_preferences": {"preferred_cuisine": None, "want_spicy": False},
                "explicit_fields": ["budget"],
            },
            "result": {"recommendations": [{"dish": "Unverified", "score": 0, "price": 100, "ingredients": ["rice"]}]},
        }
        with patch("evaluation.get_text_recommendations", return_value=wrong):
            result = evaluate_scenario(case)
        self.assertFalse(result["passed"])
        self.assertEqual(result["hard_constraint_violations"], 1)
        self.assertIn("intent", result["failures"])

    def test_parser_error_regressions_have_their_own_category(self):
        case = {
            "name": "parser_error", "mode": "text", "request": {"text": "budget 200"},
            "parser": {"failure": "timeout"}, "expected_status": 503,
        }
        result = evaluate_scenario(case)
        self.assertFalse(result["passed"])
        self.assertIn("parser", result["failures"])

    def test_every_seeded_fault_is_detected_by_its_specific_invariant(self):
        audit = run_fault_audit()
        self.assertEqual((audit["total"], audit["detected"], audit["undetected"]), (10, 10, 0))
        for case in audit["cases"]:
            with self.subTest(fault=case["fault"]):
                self.assertIn(case["required_detection"], case["observed_failures"])
                self.assertNotIn("unexpected_error", case["observed_failures"])

    def test_fault_audit_rejects_an_evaluator_that_always_passes(self):
        observation = {"passed": True, "failures": [], "expected": {"provider_calls": 1}, "actual": {"provider_calls": 1}}
        with patch("evaluation.evaluate_scenario", return_value=observation):
            audit = run_fault_audit()
        self.assertEqual(audit["detected"], 0)
        self.assertEqual(audit["undetected"], 10)

    def test_ties_with_identical_dish_names_use_full_candidate_goldens(self):
        cases = load_scenarios(Path(__file__).parent / "fixtures" / "recommendation_cases.json")
        case = next(row for row in cases if row["name"] == "identical_dish_names_tie")
        from app.main import get_recommendations

        def wrong(request, provider):
            body = get_recommendations(request, provider)
            body["recommendations"].reverse()
            return body

        with patch("evaluation.get_recommendations", side_effect=wrong):
            result = evaluate_scenario(case)
        self.assertFalse(result["passed"])
        self.assertIn("ranking", result["failures"])

    def test_invalid_response_is_not_misclassified_as_invalid_input(self):
        case = {"name": "bad_response", "mode": "text", "request": {"text": "budget 200"},
                "expected_outcome": "parsed", "expected": [],
                "expected_request": {"budget": 200, "avoid_ingredients": [], "required_ingredients": [], "preferred_cuisine": None, "want_spicy": False}}
        with patch("evaluation.get_text_recommendations", return_value={"status": "parsed", "intent": {}, "result": {}}):
            result = evaluate_scenario(case)
        self.assertEqual(result["actual"]["status"], 200)
        self.assertFalse(result["passed"])
        self.assertIn("response_contract", result["failures"])
