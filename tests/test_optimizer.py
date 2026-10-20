import copy
import itertools
import unittest

from app.optimizer import recommend_meals
from app.mock_mcp import search_restaurants


def meal(dish="Bowl", restaurant="Kitchen", **changes):
    return {
        "restaurant": restaurant, "dish": dish, "price": 100,
        "cuisine": "Indian", "spicy": False, "ingredients": ["rice"],
        **changes,
    }


class RecommendationRulesTests(unittest.TestCase):
    def test_budget_is_inclusive_and_cannot_be_overridden_by_preferences(self):
        allowed = meal(price=100)
        expensive = meal("Expensive", price=101, spicy=True)
        result = recommend_meals([expensive, allowed], 100, [], "Indian", True)
        self.assertEqual([row["dish"] for row in result], ["Bowl"])

    def test_exclusions_normalize_both_sides_and_use_unicode_casefold(self):
        rows = [meal(ingredients=[" Chicken "]), meal("Other", ingredients=["STRASSE"])]
        self.assertEqual(recommend_meals(rows, 100, [" CHICKEN ", "straße"]), [])

    def test_blank_exclusions_in_direct_domain_calls_are_ignored(self):
        self.assertEqual(len(recommend_meals([meal()], 100, [" "])), 1)

    def test_cuisine_normalization_and_spicy_preference_are_soft(self):
        rows = [meal("Plain", cuisine=" Indian "), meal("Spicy", cuisine="Chinese", spicy=True)]
        result = recommend_meals(rows, 100, [], " INDIAN ", True)
        self.assertEqual([(row["dish"], row["score"]) for row in result], [("Plain", 2), ("Spicy", 1)])

    def test_ties_are_independent_of_provider_order(self):
        rows = [meal("Z"), meal("A"), meal("A", restaurant="Another")]
        expected = ["Another", "Kitchen", "Kitchen"]
        for ordering in itertools.permutations(rows):
            with self.subTest(ordering=ordering):
                result = recommend_meals(list(ordering), 100, [])
                self.assertEqual([row["restaurant"] for row in result], expected)
                self.assertEqual([row["dish"] for row in result], ["A", "A", "Z"])

    def test_same_names_with_different_domain_fields_have_stable_order(self):
        rows = [meal(ingredients=["rice"]), meal(ingredients=["beans"])]
        self.assertEqual(recommend_meals(rows, 100, []), recommend_meals(rows[::-1], 100, []))

    def test_does_not_mutate_provider_records(self):
        rows = [meal()]
        original = copy.deepcopy(rows)
        result = recommend_meals(rows, 100, [])
        self.assertEqual(rows, original)
        self.assertIsNot(result[0], rows[0])
        self.assertEqual(result[0]["reasons"], ["Meets your hard constraints"])

    def test_empty_catalog(self):
        self.assertEqual(recommend_meals([], 100, []), [])

    def test_required_ingredients_are_all_mandatory_and_normalized(self):
        rows = [meal("Rice", ingredients=["rice"]), meal("Paneer", ingredients=[" Rice ", "Paneer"])]
        result = recommend_meals(rows, 100, [], required_ingredients=[" RICE ", "PANEER"])
        self.assertEqual([row["dish"] for row in result], ["Paneer"])
        self.assertIn("Contains all required ingredients", result[0]["reasons"])

    def test_conflicting_ingredient_constraints_have_no_matches(self):
        self.assertEqual(recommend_meals([meal()], 100, ["rice"], required_ingredients=["rice"]), [])

    def test_direct_domain_budget_is_validated(self):
        for budget in (0, -1, True, 100.5, "100"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                recommend_meals([meal()], budget, [])

    def test_recommendation_ingredients_do_not_alias_provider_records(self):
        rows = [meal()]
        result = recommend_meals(rows, 100, [])
        result[0]["ingredients"].append("chicken")
        self.assertEqual(rows[0]["ingredients"], ["rice"])

    def test_catalog_permutations_preserve_rank_and_hard_constraints(self):
        rows = search_restaurants()
        checked = 0
        for ordering in itertools.permutations(rows):
            result = recommend_meals(list(ordering), 220, ["CHICKEN"], "Indian", True, ["rice"])
            self.assertEqual([(row["dish"], row["score"]) for row in result], [("Paneer Rice Bowl", 3), ("Vegetable Fried Rice", 0)])
            for row in result:
                self.assertLessEqual(row["price"], 220)
                self.assertNotIn("chicken", {item.casefold() for item in row["ingredients"]})
                self.assertIn("rice", {item.casefold() for item in row["ingredients"]})
            checked += 1
        self.assertEqual(checked, 120)

    def test_tightening_hard_constraints_cannot_add_candidates(self):
        rows = search_restaurants()
        identities = lambda result: {(row["restaurant"], row["dish"]) for row in result}
        previous = set()
        for budget in (159, 160, 179, 180, 219, 220, 239, 240, 279, 280):
            current = identities(recommend_meals(rows, budget, []))
            self.assertTrue(previous.issubset(current))
            previous = current
        excluded = identities(recommend_meals(rows, 280, ["chicken"]))
        required = identities(recommend_meals(rows, 280, ["chicken"], required_ingredients=["paneer"]))
        self.assertTrue(required.issubset(excluded))
        self.assertTrue(excluded.issubset(previous))
