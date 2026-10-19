"""Meal catalog boundary; provider payloads must be mapped before reaching it."""

from typing import Protocol

from app.mock_mcp import search_restaurants
from app.candidates import MealCandidate


class ProviderUnavailable(Exception):
    """The provider could not supply a meal catalog."""


class ProviderTimeout(ProviderUnavailable):
    """A read operation exceeded its provider deadline."""


class MealProvider(Protocol):
    name: str

    def search_meals(self) -> list[MealCandidate]:
        """Return MealPilot meal records, independent of vendor tool schemas."""
        ...


class MockMealProvider:
    name = "mock_mcp"

    def search_meals(self) -> list[MealCandidate]:
        try:
            return search_restaurants()
        except TimeoutError as error:
            raise ProviderTimeout("Meal catalog request timed out") from error
        except (OSError, ValueError) as error:
            raise ProviderUnavailable("Meal catalog is unavailable") from error


def get_meal_provider() -> MealProvider:
    """Keep the local mock as the default; never select a live provider implicitly."""
    return MockMealProvider()
