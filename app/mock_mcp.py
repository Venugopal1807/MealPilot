import json
from pathlib import Path


DATA_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "restaurants.json"
)


def search_restaurants() -> list[dict]:
    """Simulate a Swiggy restaurant search tool."""

    with DATA_FILE.open("r", encoding="utf-8") as file:
        restaurants = json.load(file)

    return restaurants