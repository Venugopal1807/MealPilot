import unittest

from app.candidates import InvalidCandidateData, normalize_candidates


def candidate(**changes):
    return {
        "restaurant": "Synthetic", "dish": "Bowl", "price": 100,
        "cuisine": "Indian", "spicy": False, "ingredients": ["rice"],
        **changes,
    }


class CandidateContractTests(unittest.TestCase):
    def test_missing_required_fields_are_rejected(self):
        for field in candidate():
            row = candidate()
            del row[field]
            with self.subTest(field=field), self.assertRaises(InvalidCandidateData):
                normalize_candidates([row])

    def test_invalid_field_values_are_rejected(self):
        changes = [
            {"price": value} for value in (True, False, 0, -1, 1.5, "100", None, float("nan"), float("inf"))
        ] + [
            {"ingredients": value} for value in (None, [], [""], [" "], [1], "rice", {"rice": True})
        ] + [
            {"spicy": value} for value in (1, "false", None)
        ] + [
            {field: value} for field in ("restaurant", "dish", "cuisine") for value in ("", " ", None, 1)
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(InvalidCandidateData):
                normalize_candidates([candidate(**change)])

    def test_invalid_container_and_records_are_rejected(self):
        for data in (None, {}, "catalog", [None], [[]], ["meal"]):
            with self.subTest(data=data), self.assertRaises(InvalidCandidateData):
                normalize_candidates(data)

    def test_unknown_provider_fields_are_not_exposed(self):
        row = candidate(access_token="synthetic-do-not-expose", score=999)
        result = normalize_candidates([row])[0]
        self.assertEqual(result, candidate())
        self.assertIsNot(result["ingredients"], row["ingredients"])

    def test_catalog_is_rejected_even_if_invalid_record_would_be_over_budget(self):
        with self.assertRaises(InvalidCandidateData):
            normalize_candidates([candidate(), candidate(price=999, ingredients=None)])

    def test_explicit_unknown_evidence_and_non_boolean_flags_are_rejected(self):
        for value in (False, None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(InvalidCandidateData):
                normalize_candidates([candidate(ingredients_complete=value)])
        for placeholder in (" UNKNOWN ", "N/A", "not available", "unspecified"):
            with self.subTest(placeholder=placeholder), self.assertRaises(InvalidCandidateData):
                normalize_candidates([candidate(ingredients=["rice", placeholder])])
        self.assertEqual(normalize_candidates([candidate(ingredients_complete=True)]), [candidate()])
