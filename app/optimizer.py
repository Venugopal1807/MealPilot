def recommend_meals(
    meals: list[dict],
    budget: int,
    avoid_ingredients: list[str],
    preferred_cuisine: str | None = None,
    want_spicy: bool = False,
) -> list[dict]:
    """Filter meals by hard constraints, then rank the matches."""

    excluded = {
        ingredient.strip().lower()
        for ingredient in avoid_ingredients
    }

    matches = []

    for meal in meals:
        # Hard constraint 1: stay within budget.
        if meal["price"] > budget:
            continue

        # Hard constraint 2: reject excluded ingredients.
        meal_ingredients = {
            ingredient.lower()
            for ingredient in meal["ingredients"]
        }

        if excluded.intersection(meal_ingredients):
            continue

        # Rank the meals that pass both hard constraints.
        score = 0
        reasons = []

        if preferred_cuisine:
            if meal["cuisine"].lower() == preferred_cuisine.lower():
                score += 2
                reasons.append("Matches your preferred cuisine")

        if want_spicy and meal["spicy"]:
            score += 1
            reasons.append("Matches your spicy-food preference")

        result = meal.copy()
        result["score"] = score
        result["reasons"] = reasons or ["Meets your hard constraints"]

        matches.append(result)

    # Highest score first. Lower price breaks ties.
    matches.sort(key=lambda meal: (-meal["score"], meal["price"]))

    return matches