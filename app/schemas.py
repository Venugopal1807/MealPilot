"""Shared request and intent schemas, independent of HTTP and commerce."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", revalidate_instances="always")


class HardConstraints(StrictModel):
    budget: int = Field(gt=0)
    avoid_ingredients: list[str] = Field(default_factory=list)
    required_ingredients: list[str] = Field(default_factory=list)

    @field_validator("avoid_ingredients", "required_ingredients")
    @classmethod
    def reject_blank_ingredients(cls, ingredients: list[str]) -> list[str]:
        if any(not ingredient.strip() for ingredient in ingredients):
            raise ValueError("Ingredient names must not be blank")
        return ingredients


class SoftPreferences(StrictModel):
    preferred_cuisine: str | None = None
    want_spicy: bool = False


class MealRequest(SoftPreferences, HardConstraints):
    """The existing structured API contract, shared with the intent boundary."""


IntentField = Literal[
    "budget", "avoid_ingredients", "required_ingredients", "preferred_cuisine", "want_spicy",
]


class MealIntent(StrictModel):
    hard_constraints: HardConstraints
    soft_preferences: SoftPreferences
    explicit_fields: list[IntentField]

    @model_validator(mode="after")
    def require_explicit_provenance(self):
        populated = {"budget"}
        request = self.to_request()
        for name in ("avoid_ingredients", "required_ingredients", "preferred_cuisine", "want_spicy"):
            if getattr(request, name):
                populated.add(name)
        if len(set(self.explicit_fields)) != len(self.explicit_fields) or set(self.explicit_fields) != populated:
            raise ValueError("Explicit fields must identify each supplied constraint or preference")
        return self

    def to_request(self) -> MealRequest:
        return MealRequest.model_validate({
            **self.hard_constraints.model_dump(), **self.soft_preferences.model_dump(),
        })

    def semantic_key(self) -> tuple:
        """Compare meaning using the optimizer's documented matching semantics."""
        request = self.to_request()
        return (
            request.budget,
            frozenset(item.strip().casefold() for item in request.avoid_ingredients),
            frozenset(item.strip().casefold() for item in request.required_ingredients),
            (request.preferred_cuisine or "").strip().casefold(),
            request.want_spicy,
        )


class NaturalLanguageRequest(StrictModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("Text must not be blank")
        return text


class IntentIssue(StrictModel):
    code: Literal[
        "unsupported_dietary", "unsupported_nutrition", "unsupported_safety",
        "unsupported_mandatory", "missing_budget", "ambiguous_budget", "invalid_budget",
        "ambiguous_preference", "unrecognized_text",
    ]
    message: str = Field(min_length=1)


class ParsedIntent(StrictModel):
    status: Literal["parsed"]
    intent: MealIntent


class UnresolvedIntent(StrictModel):
    status: Literal["clarification_required", "unsupported"]
    issues: list[IntentIssue] = Field(min_length=1)


IntentResult = Annotated[ParsedIntent | UnresolvedIntent, Field(discriminator="status")]
