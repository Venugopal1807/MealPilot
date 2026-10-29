"""Synthetic offline tests of the opt-in runner; network transport is forbidden."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import live_evaluation as live
from app.gemini import GeminiTransportUnavailable, GeminiRestClient, policy_reference
from app.intent import DeterministicIntentParser
from app.schemas import ParsedIntent


def reply(result):
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20, "totalTokenCount": 30}}


class FakeClient:
    def generate(self, **kwargs):
        # Test fake only; reference labels in the fixture never come from this.
        text = kwargs["payload"]["contents"][0]["parts"][0]["text"]
        return reply(DeterministicIntentParser().parse(policy_reference(text)))


class LiveRunnerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-never-log", "MEALPILOT_GEMINI_MODEL": "gemini-test", "MEALPILOT_LLM_TIMEOUT_SECONDS": "1"})
        environment.start(); self.addCleanup(environment.stop)
        network = patch.object(GeminiRestClient, "generate", side_effect=AssertionError("Network forbidden"))
        self.network = network.start(); self.addCleanup(network.stop)
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "report.json"

    def run_fake(self, client=None, limit=25, confirmation=None):
        stdout = io.StringIO()
        factory = Mock(return_value=client if client is not None else FakeClient())
        with contextlib.redirect_stdout(stdout):
            result = live.main(["--live", "--max-calls", str(limit), "--output", str(self.output)],
                               client_factory=factory, confirm=confirmation or (lambda prompt: "RUN " + prompt.split("RUN ")[1].split()[0]))
        report = json.loads(self.output.read_text()) if self.output.exists() else None
        self.assertNotIn("test-key-never-log", stdout.getvalue() + json.dumps(report))
        self.network.assert_not_called()
        return result, report, factory

    def test_reference_labels_reviewed_against_product_policy(self):
        cases = live.load_cases()
        self.assertEqual(len(cases), 25)
        for case in cases:
            with self.subTest(case=case["id"]):
                from app.intent import RESULT_ADAPTER
                result = RESULT_ADAPTER.validate_python(DeterministicIntentParser().parse(policy_reference(case["text"])))
                self.assertTrue(live.score(case, result)["exact_match"])
        # Independently written exact boundary goldens are retained in the fixture.
        self.assertEqual([c["hard_constraints"]["budget"] for c in cases[:4]], [220,219,180,160])

    def test_flags_key_and_limits_required_before_client_creation(self):
        for args in (["--max-calls","2"], ["--live"], ["--live","--max-calls","26"], ["--live","--max-calls","0"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                live.main([*args,"--output",str(self.output)], client_factory=Mock(side_effect=AssertionError("created")))
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            live.main(["--live","--max-calls","1","--output",str(self.output)], client_factory=Mock(side_effect=AssertionError("created")))
        self.network.assert_not_called()

    def test_precision_recall_and_omission_oracles_use_literal_labels(self):
        from app.schemas import HardConstraints, MealIntent, SoftPreferences
        case = next(c for c in live.load_cases() if c["id"] == "full_intent")
        wrong = ParsedIntent(status="parsed", intent=MealIntent(
            hard_constraints=HardConstraints(budget=221, required_ingredients=["paneer"]),
            soft_preferences=SoftPreferences(preferred_cuisine="indian",want_spicy=True),
            explicit_fields=["budget","required_ingredients","preferred_cuisine","want_spicy"]))
        scored = live.score(case, wrong)
        self.assertEqual(scored["hard_true_positive"], 1)
        self.assertEqual(scored["hard_predicted"], 2)
        self.assertEqual(scored["hard_expected"], 3)
        self.assertEqual(scored["omitted_or_changed_reference_atoms"], [["budget",220],["excluded","chicken"]])
        self.assertTrue(scored["schema_valid_semantically_incorrect"])

    def test_invalid_reference_labels_stop_before_confirmation_or_client_creation(self):
        original = json.loads(live.FIXTURE.read_text(encoding="utf-8"))
        mutations = (
            (0, "hard_constraints", "budget", True),
            (0, "soft_preferences", "budget", 999),
            (0, "hard_constraints", "required_ingredients", [""]),
            (16, "hard_constraints", "budget", -1),
            (19, "hard_constraints", "dietary_certification", "vegan"),
            (19, "soft_preferences", "want_spicy", "false"),
            (19, None, "expected_issue", "missing_budget"),
            (19, None, "clarification_conditions", ["Ask anyway"]),
            (16, None, "expected_issue", "invented_issue"),
            (16, None, "categories", []),
            (16, None, "unsupported_requirements", ["vegan"]),
        )
        fixture = self.output.parent / "invalid-fixture.json"
        for index, group, field, value in mutations:
            with self.subTest(index=index, field=field):
                document = deepcopy(original)
                target = document["cases"][index]
                if group:
                    target = target[group]
                target[field] = value
                fixture.write_text(json.dumps(document), encoding="utf-8")
                factory, confirm = Mock(), Mock()
                with patch.object(live, "FIXTURE", fixture), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    live.main(["--live", "--max-calls", "1", "--output", str(self.output)],
                              client_factory=factory, confirm=confirm)
                factory.assert_not_called()
                confirm.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_unknown_and_missing_reference_facts_are_not_defaulted(self):
        document = json.loads(live.FIXTURE.read_text(encoding="utf-8"))
        fixture = self.output.parent / "fixture.json"
        for index, group, field in ((0, "hard_constraints", "budget"),
                                    (19, "hard_constraints", "avoid_ingredients"),
                                    (16, "soft_preferences", "want_spicy")):
            with self.subTest(field=field):
                invalid = deepcopy(document)
                del invalid["cases"][index][group][field]
                fixture.write_text(json.dumps(invalid), encoding="utf-8")
                with patch.object(live, "FIXTURE", fixture), self.assertRaises(ValueError):
                    live.load_cases()

    def test_soft_preference_errors_are_visible_without_raw_model_text(self):
        case = next(c for c in live.load_cases() if c["id"] == "full_intent")
        wrong = live.expected_intent(case).model_dump()
        wrong["intent"]["soft_preferences"]["preferred_cuisine"] = "arbitrary private model text"
        wrong["intent"]["soft_preferences"]["want_spicy"] = False
        wrong["intent"]["explicit_fields"].remove("want_spicy")
        scored = live.score(case, ParsedIntent.model_validate(wrong))
        self.assertEqual(scored["changed_soft_preference_fields"], ["preferred_cuisine", "want_spicy"])
        self.assertEqual(scored["observed_outcome"], "parsed")
        self.assertFalse(scored["exact_match"])
        self.assertEqual(scored["hard_omitted_or_changed"], 0)
        self.assertNotIn("arbitrary private model text", json.dumps(scored))

    def test_incorrect_acceptance_and_schema_valid_wrong_rejection(self):
        from app.schemas import UnresolvedIntent, IntentIssue
        cases = live.load_cases()
        parsed = live.expected_intent(cases[0])
        rejected = UnresolvedIntent(status="clarification_required",issues=[IntentIssue(code="missing_budget",message="Test")])
        self.assertTrue(live.score(cases[0],rejected)["schema_valid_semantically_incorrect"])
        self.assertTrue(live.score(cases[0],rejected)["incorrect_rejection"])
        blocked = next(c for c in cases if c["id"] == "dietary")
        self.assertTrue(live.score(blocked,parsed)["incorrect_acceptance"])

    def test_confirmation_is_required_and_cancellation_has_no_calls(self):
        result, report, factory = self.run_fake(confirmation=lambda _: "yes")
        self.assertEqual(result, 2); self.assertIsNone(report); factory.assert_not_called()

    def test_full_fake_run_separates_policy_and_model_metrics(self):
        result, report, _ = self.run_fake()
        self.assertEqual(result, 0)
        self.assertEqual(report["summary"]["planned_calls"], 15)
        self.assertEqual(report["summary"]["attempted_calls"], 15)
        self.assertEqual(report["summary"]["exact_intent_matches"], live.fraction(15,15))
        self.assertEqual(report["summary"]["policy_only_matches"], live.fraction(10,10))
        self.assertEqual(report["summary"]["unsupported_model_accuracy"]["denominator"], 0)
        self.assertTrue(all(row["provider_calls"] == 0 for row in report["cases"]))
        self.assertEqual(report["cases"][0]["usage"]["totalTokenCount"], 30)

    def test_call_cap_and_not_run_ids(self):
        _, report, _ = self.run_fake(limit=2)
        self.assertEqual(report["summary"]["attempted_calls"], 2)
        self.assertEqual(len(report["not_run_ids"]), 13)

    def test_documented_output_path_works_without_existing_parent(self):
        self.output = self.output.parent / "new-build" / "report.json"
        result, report, _ = self.run_fake(limit=1)
        self.assertEqual(result, 0)
        self.assertEqual(report["summary"]["attempted_calls"], 1)

    def test_unavailable_usage_is_null_instead_of_zero_tokens(self):
        client = Mock()
        client.generate.return_value = reply(DeterministicIntentParser().parse("at most 220"))
        client.generate.return_value["usageMetadata"] = {"promptTokenCount": True, "totalTokenCount": -1}
        _, report, _ = self.run_fake(client, limit=1)
        self.assertIsNone(report["cases"][0]["usage"])
        self.assertEqual(report["summary"]["usage_reported_calls"], live.fraction(0, 1))
        for metric in report["summary"]["reported_token_totals"].values():
            self.assertEqual(metric, {"sum": None, "denominator_calls": 0})

    def test_report_write_failure_stops_calls_and_preserves_previous_cases(self):
        save = live.save_report
        saves = 0

        def fail_after_first_case(path, report):
            nonlocal saves
            saves += 1
            if saves >= 3:
                raise OSError("Simulated disk failure")
            save(path, report)

        client = Mock(wraps=FakeClient())
        with patch.object(live, "save_report", side_effect=fail_after_first_case), self.assertRaises(OSError):
            self.run_fake(client)
        self.assertEqual(client.generate.call_count, 2)
        previous = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(previous["summary"]["attempted_calls"], 1)
        self.assertEqual(len(previous["cases"]), 1)

    def test_semantic_errors_remain_visible_despite_application_rejection(self):
        client = Mock(); client.generate.return_value = reply(DeterministicIntentParser().parse("budget 999"))
        _, report, _ = self.run_fake(client, limit=1)
        row = report["cases"][0]
        self.assertTrue(row["model_score"]["schema_valid_semantically_incorrect"])
        self.assertEqual(row["model_score"]["hard_omitted_or_changed"], 1)
        self.assertEqual(row["model_score"]["hard_extra_or_changed"], 1)
        self.assertTrue(row["application_score"]["incorrect_rejection"])
        self.assertEqual(report["summary"]["hard_recall"], live.fraction(0,1))

    def test_auth_and_quota_stop_and_preserve_partial_report(self):
        for code in (401,403,402,429):
            if self.output.exists(): self.output.unlink()
            client = Mock(); client.generate.side_effect = GeminiTransportUnavailable(code)
            _, report, _ = self.run_fake(client)
            self.assertEqual(client.generate.call_count,1)
            self.assertEqual(report["summary"]["failed_calls"],1)
            self.assertEqual(report["summary"]["exact_intent_matches"]["denominator"],0)
            self.assertTrue(report["status"].startswith("stopped_"))

    def test_three_transport_failures_stop_without_retry(self):
        client = Mock(); client.generate.side_effect = TimeoutError("secret")
        _, report, _ = self.run_fake(client)
        self.assertEqual(client.generate.call_count,3)
        self.assertEqual(report["status"],"stopped_three_consecutive_transport_failures")
        self.assertEqual(len({r["id"] for r in report["cases"]}),3)

    def test_invalid_output_is_not_disguised_as_transport_failure(self):
        client = Mock(); client.generate.return_value = {"candidates": []}
        _, report, _ = self.run_fake(client,limit=1)
        self.assertEqual(report["summary"]["successful_calls"],1)
        self.assertEqual(report["summary"]["failed_calls"],0)
        self.assertEqual(report["summary"]["exact_intent_matches"], live.fraction(0,1))

    def test_inflight_interrupt_is_recorded(self):
        client = Mock(); client.generate.side_effect = KeyboardInterrupt
        _, report, _ = self.run_fake(client)
        self.assertEqual(report["summary"]["attempted_calls"],1)
        self.assertEqual(report["status"],"stopped_interrupted")

    def test_existing_output_cannot_be_overwritten(self):
        self.output.write_text('original')
        with self.assertRaises(FileExistsError): self.run_fake()
        self.assertEqual(self.output.read_text(),'original')

    def test_offline_evaluations_never_use_network(self):
        from evaluation import run_evaluation
        self.assertEqual(run_evaluation()["summary"]["passed"],51)
        self.assertEqual(run_evaluation(Path(__file__).parent / "fixtures/llm_cases.json")["summary"]["passed"],9)
        self.network.assert_not_called()
