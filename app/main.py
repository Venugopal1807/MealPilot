from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.mock_mcp import search_restaurants
from app.optimizer import recommend_meals


app = FastAPI(title="MealPilot AI")


class MealRequest(BaseModel):
    budget: int = Field(gt=0)
    avoid_ingredients: list[str] = Field(default_factory=list)
    preferred_cuisine: str | None = None
    want_spicy: bool = False


@app.get("/health")
def health_check():
    return {"status": "ok", "project": "MealPilot AI"}


@app.post("/recommend")
def get_recommendations(request: MealRequest):
    meals = search_restaurants()

    recommendations = recommend_meals(
        meals=meals,
        budget=request.budget,
        avoid_ingredients=request.avoid_ingredients,
        preferred_cuisine=request.preferred_cuisine,
        want_spicy=request.want_spicy,
    )

    return {
        "count": len(recommendations),
        "recommendations": recommendations,
        "source": "mock_mcp",
    }