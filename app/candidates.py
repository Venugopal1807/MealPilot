"""MealPilot's provider-independent catalog contract and integrity checks."""

from typing import NotRequired, TypedDict


class MealCandidate(TypedDict):
    restaurant: str
    dish: str
    price: int
    cuisine: str
    spicy: bool
    ingredients: list[str]
    ingredients_complete: NotRequired[bool]


class InvalidCandidateData(ValueError):
    """A catalog cannot establish the facts needed for safe eligibility checks."""


def normalize_candidates(records: object) -> list[MealCandidate]:
    """Validate the whole catalog and copy only the internal, documented fields.

    Reject incomplete catalogs instead of silently substituting favorable values
    or returning a partial result. Ingredient lists are asserted provider facts,
    not a guarantee of allergen safety or absence of cross-contamination.
    """
    if not isinstance(records, list):
        raise InvalidCandidateData("Catalog must be a list")

    candidates: list[MealCandidate] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise InvalidCandidateData(f"Candidate {index} must be an object")
        for field in ("restaurant", "dish", "cuisine"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise InvalidCandidateData(f"Candidate {index} has invalid {field}")
        price = record.get("price")
        if type(price) is not int or price <= 0:
            raise InvalidCandidateData(f"Candidate {index} has invalid price")
        if type(record.get("spicy")) is not bool:
            raise InvalidCandidateData(f"Candidate {index} has invalid spicy flag")
        ingredients = record.get("ingredients")
        if "ingredients_complete" in record and record["ingredients_complete"] is not True:
            raise InvalidCandidateData(f"Candidate {index} explicitly lacks complete ingredient evidence")
        if not isinstance(ingredients, list) or not ingredients or any(
            not isinstance(item, str) or not item.strip()
            or item.strip().casefold() in {"unknown", "n/a", "not available", "unspecified"}
            for item in ingredients
        ):
            raise InvalidCandidateData(f"Candidate {index} has unknown or invalid ingredients")
        candidates.append(MealCandidate(
            restaurant=record["restaurant"], dish=record["dish"], price=price,
            cuisine=record["cuisine"], spicy=record["spicy"],
            ingredients=list(ingredients),
        ))
    return candidates
