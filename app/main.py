import json
import logging
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.candidates import InvalidCandidateData
from app.intent import IntentParser, InvalidParserOutput, ParserUnavailable, get_intent_parser, parse_intent
from app.optimizer import recommend_meals
from app.providers import MealProvider, ProviderTimeout, ProviderUnavailable, get_meal_provider
from app.schemas import IntentResult, MealRequest, NaturalLanguageRequest, ParsedIntent, UnresolvedIntent


app = FastAPI(title="MealPilot AI", version="0.1.3")
logger = logging.getLogger("mealpilot")


@app.middleware("http")
async def correlate_requests(request: Request, call_next):
    request_id = str(uuid4())
    try:
        response = await call_next(request)
    except Exception as error:
        # Deliberately omit exception messages, payloads, and tracebacks: future
        # adapters may include sensitive external data in their exceptions.
        logger.error(json.dumps({
            "event": "unexpected_error", "request_id": request_id,
            "error_type": type(error).__name__,
        }))
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
    response.headers["X-Request-ID"] = request_id
    logger.info(json.dumps({
        "event": "request_completed", "request_id": request_id,
        "status": response.status_code,
    }))
    return response


class MealRecommendation(BaseModel):
    restaurant: str
    dish: str
    price: int
    cuisine: str
    spicy: bool
    ingredients: list[str]
    score: int
    reasons: list[str]


class RecommendationResponse(BaseModel):
    count: int
    recommendations: list[MealRecommendation]
    source: str


class TextRecommendationResponse(ParsedIntent):
    result: RecommendationResponse


@app.get("/health")
def health_check():
    return {"status": "ok", "project": "MealPilot AI"}


@app.post("/recommend", response_model=RecommendationResponse)
def get_recommendations(
    request: MealRequest,
    provider: MealProvider = Depends(get_meal_provider),
):
    try:
        meals = provider.search_meals()
        recommendations = recommend_meals(
            meals=meals,
            budget=request.budget,
            avoid_ingredients=request.avoid_ingredients,
            required_ingredients=request.required_ingredients,
            preferred_cuisine=request.preferred_cuisine,
            want_spicy=request.want_spicy,
        )
    except (ProviderTimeout, TimeoutError):
        raise HTTPException(status_code=504, detail="Meal provider timed out") from None
    except ProviderUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Meal recommendations are temporarily unavailable",
        ) from None
    except InvalidCandidateData:
        raise HTTPException(status_code=502, detail="Meal provider returned an invalid catalog") from None

    return {
        "count": len(recommendations),
        "recommendations": recommendations,
        "source": provider.name,
    }


@app.post("/intent", response_model=IntentResult)
def get_meal_intent(
    request: NaturalLanguageRequest,
    parser: IntentParser = Depends(get_intent_parser),
):
    try:
        return parse_intent(request.text, parser)
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Intent parser timed out") from None
    except ParserUnavailable:
        raise HTTPException(status_code=503, detail="Intent parser is temporarily unavailable") from None
    except InvalidParserOutput:
        raise HTTPException(status_code=502, detail="Intent parser returned invalid output") from None


@app.post("/recommend/text", response_model=TextRecommendationResponse | UnresolvedIntent)
def get_text_recommendations(
    request: NaturalLanguageRequest,
    parser: IntentParser = Depends(get_intent_parser),
    provider: MealProvider = Depends(get_meal_provider),
):
    parsed = get_meal_intent(request, parser)
    if isinstance(parsed, UnresolvedIntent):
        return parsed
    return {
        **parsed.model_dump(),
        "result": get_recommendations(parsed.intent.to_request(), provider),
    }
