from app.candidates import normalize_candidates


def recommend_meals(
    meals: list[dict],
    budget: int,
    avoid_ingredients: list[str],
    preferred_cuisine: str | None = None,
    want_spicy: bool = False,
    required_ingredients: list[str] | None = None,
) -> list[dict]:
    """Filter meals by hard constraints, then rank the matches."""

    if type(budget) is not int or budget <= 0:
        raise ValueError("Budget must be a positive integer")
    candidates = normalize_candidates(meals)
    excluded = {
        ingredient.strip().casefold()
        for ingredient in avoid_ingredients
        if ingredient.strip()
    }
    required = {
        ingredient.strip().casefold()
        for ingredient in required_ingredients or []
        if ingredient.strip()
    }
    cuisine = preferred_cuisine.strip().casefold() if preferred_cuisine else ""

    matches = []

    for meal in candidates:
        # Hard constraint 1: stay within budget.
        if meal["price"] > budget:
            continue

        # Hard constraint 2: reject excluded ingredients.
        meal_ingredients = {
            ingredient.strip().casefold()
            for ingredient in meal["ingredients"]
        }

        if excluded.intersection(meal_ingredients):
            continue
        if not required.issubset(meal_ingredients):
            continue

        # Rank the meals that pass both hard constraints.
        score = 0
        reasons = []
        if required:
            reasons.append("Contains all required ingredients")

        if cuisine:
            if meal["cuisine"].strip().casefold() == cuisine:
                score += 2
                reasons.append("Matches your preferred cuisine")

        if want_spicy and meal["spicy"]:
            score += 1
            reasons.append("Matches your spicy-food preference")

        result = meal.copy()
        result["score"] = score
        result["reasons"] = reasons or ["Meets your hard constraints"]

        matches.append(result)

    # Break ties using domain fields so provider ordering cannot change rankings.
    matches.sort(key=lambda meal: (
        -meal["score"],
        meal["price"],
        meal["restaurant"].strip().casefold(),
        meal["dish"].strip().casefold(),
        meal["cuisine"].strip().casefold(),
        meal["spicy"],
        tuple(sorted(meal["ingredients"])),
        meal["restaurant"],
        meal["dish"],
        meal["cuisine"],
        tuple(meal["ingredients"]),
    ))

    return matches
