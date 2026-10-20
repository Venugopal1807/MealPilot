"""A conservative local parser for a documented subset of English meal requests.

Every non-filler token must be consumed. Partial understanding never authorizes
recommendations; unknown clauses require clarification. This parser asserts no
catalog facts and makes no provider calls.
"""

import re
from typing import Protocol

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.schemas import IntentResult, NaturalLanguageRequest, ParsedIntent, UnresolvedIntent


class ParserUnavailable(Exception):
    """The intent parser could not process the request."""


class InvalidParserOutput(ValueError):
    """Parser output failed the shared intent contract."""


class IntentParser(Protocol):
    def parse(self, text: str) -> object:
        """Return intent or an unresolved result, never meal recommendations."""
        ...


RESULT_ADAPTER = TypeAdapter(IntentResult)
BUDGET = re.compile(
    r"\b(?P<kind>no more than|at most|up to|less than|under|below|maximum budget|"
    r"max budget|maximum|budget(?: of| is)?|max)\s*(?:inr\s*|rs\.?\s*|₹\s*)?"
    r"(?P<amount>[+-]?\d+(?:[.,]\d+)*)(?:\s*(?:rupees|inr|rs))?(?!\w)"
)
INGREDIENT = r"(?:soy sauce|vegetables|chicken|paneer|spices|chilli|rice)"
INGREDIENTS = re.compile(
    r"\b(?P<kind>without|excluding|exclude|avoid|no|with|including|include|require|must contain)\s+"
    r"(?P<items>" + INGREDIENT + r"(?:\s*(?:,|and)\s*" + INGREDIENT + r")*)(?![\w-])"
)
CUISINE = re.compile(r"\b(?:hyderabadi|indian|andhra|chinese)\b")
FILLERS = {
    "i", "want", "would", "like", "find", "show", "me", "a", "an", "the",
    "some", "please", "meal", "meals", "food", "and", "cuisine",
}


def unresolved(status: str, code: str, message: str) -> dict:
    return {"status": status, "issues": [{"code": code, "message": message}]}


def unsupported_intent(text: str) -> dict | None:
    """Application capability policy, also enforced before any injected parser."""
    unsupported = [
        (r"\b(?:allerg\w*|certified|safe|cross[- ]contamination)\b", "unsupported_safety",
         "The catalog cannot establish allergy safety or certification."),
        (r"\b(?:vegan|vegetarian|veg|non-veg|halal|kosher|pescatarian|gluten[- ]free|dairy[- ]free|lactose[- ]free)\b",
         "unsupported_dietary", "The catalog has no verified dietary labels; specify supported ingredient constraints instead."),
        (r"\b(?:high[- ]protein|protein|calories?|nutriti\w*|low[- ]carb|keto|healthy)\b",
         "unsupported_nutrition", "The catalog has no verified nutrition or health attributes."),
        (r"\b(?:not spicy|non[- ]spicy|no spicy|mild|only spicy|must be spicy|spicy only)\b",
         "unsupported_mandatory", "Spiciness is currently a soft preference, not a mandatory filter."),
        (r"\b(?:only|must be|strictly)\s+(?:indian|andhra|hyderabadi|chinese)\b|"
         r"\b(?:indian|andhra|hyderabadi|chinese)\s+only\b",
         "unsupported_mandatory", "Cuisine is currently a soft preference, not a mandatory filter."),
    ]
    for pattern, code, message in unsupported:
        if re.search(pattern, text):
            return unresolved("unsupported", code, message)
    return None


class DeterministicIntentParser:
    def parse(self, text: str) -> dict:
        # The same bounded input contract also applies to direct parser callers.
        NaturalLanguageRequest(text=text)
        text = " ".join(text.casefold().split())
        blocked = unsupported_intent(text)
        if blocked:
            return blocked

        if re.search(r"^(?:and|or)\b|\b(?:and|or)\s*[,.!?;]*$", text):
            return unresolved("clarification_required", "ambiguous_preference", "Complete the unfinished conjunction before confirming constraints.")

        if re.search(r"\b(?:maybe|perhaps|prefer|preferably)\b[^,;.!?]*\b(?:with|without|no|including|excluding|exclude|avoid|include|require)\b", text):
            return unresolved("clarification_required", "ambiguous_preference", "Confirm whether ingredient wording is mandatory or only a preference.")

        budgets = list(BUDGET.finditer(text))
        if not budgets:
            return unresolved("clarification_required", "missing_budget", "Provide an explicit maximum integer budget, such as 'at most 220'.")
        if len(budgets) != 1:
            return unresolved("clarification_required", "ambiguous_budget", "Provide one maximum budget rather than multiple amounts.")
        budget_match = budgets[0]
        amount = budget_match["amount"]
        if not re.fullmatch(r"\d+", amount):
            return unresolved("clarification_required", "invalid_budget", "Use a positive whole-number budget without separators.")
        budget = int(amount)
        if budget_match["kind"] in {"under", "below", "less than"}:
            budget -= 1
        if budget <= 0:
            return unresolved("clarification_required", "invalid_budget", "The resulting maximum integer budget must be positive.")

        # Mask consumed spans without altering positions used by later matches.
        remaining = list(text)

        def consume(match):
            remaining[match.start():match.end()] = " " * (match.end() - match.start())

        consume(budget_match)
        required, excluded = set(), set()
        for match in INGREDIENTS.finditer("".join(remaining)):
            target = excluded if match["kind"] in {"without", "excluding", "exclude", "avoid", "no"} else required
            if target is excluded and re.match(
                r"\s*(?:,\s*(?:and\s+)?|and\s+)(?:spicy|indian|andhra|hyderabadi|chinese|cuisine)\b",
                text[match.end():],
            ):
                return unresolved("clarification_required", "ambiguous_preference", "Clarify the exclusion scope. Use a separate clause such as '; prefer spicy' for a positive preference.")
            target.update(re.split(r"\s*(?:,|\band\b)\s*", match["items"]))
            consume(match)

        for match in re.finditer(r"\bprefer\s+(?=(?:hyderabadi|indian|andhra|chinese|spicy)\b)", "".join(remaining)):
            consume(match)
        cuisines = list(CUISINE.finditer("".join(remaining)))
        if len({match[0] for match in cuisines}) > 1:
            return unresolved("clarification_required", "ambiguous_preference", "Choose one cuisine preference.")
        cuisine = cuisines[0][0] if cuisines else None
        for match in cuisines:
            consume(match)
        spicy_matches = list(re.finditer(r"\bspicy\b", "".join(remaining)))
        for match in spicy_matches:
            consume(match)
        residue = re.sub(r"[,.!?;]", " ", "".join(remaining))
        if any(token not in FILLERS for token in residue.split()):
            return unresolved("clarification_required", "unrecognized_text", "Some wording is unsupported or ambiguous. Restate it using explicit budget, ingredient, cuisine, or spicy clauses.")

        fields = ["budget"]
        if required:
            fields.append("required_ingredients")
        if excluded:
            fields.append("avoid_ingredients")
        if cuisine:
            fields.append("preferred_cuisine")
        if spicy_matches:
            fields.append("want_spicy")
        return {
            "status": "parsed",
            "intent": {
                "hard_constraints": {"budget": budget, "required_ingredients": sorted(required), "avoid_ingredients": sorted(excluded)},
                "soft_preferences": {"preferred_cuisine": cuisine, "want_spicy": bool(spicy_matches)},
                "explicit_fields": sorted(fields),
            },
        }


def parse_intent(text: str, parser: IntentParser) -> IntentResult:
    NaturalLanguageRequest(text=text)
    blocked = unsupported_intent(" ".join(text.casefold().split()))
    if blocked:
        return RESULT_ADAPTER.validate_python(blocked, strict=True)
    try:
        # The supported local grammar is an independent acceptance policy, not
        # a license for an injected parser to drop or invent user constraints.
        reference = RESULT_ADAPTER.validate_python(DeterministicIntentParser().parse(text), strict=True)
        if isinstance(reference, UnresolvedIntent):
            return reference
        raw = reference.model_dump() if type(parser) is DeterministicIntentParser else parser.parse(text)
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()
        # Reconstruct even model instances so mutated or unvalidated adapters
        # cannot bypass schema validation at this boundary.
        result = RESULT_ADAPTER.validate_python(raw, strict=True)
    except ValidationError as error:
        raise InvalidParserOutput("Invalid intent parser output") from error
    if isinstance(result, ParsedIntent) and result.intent.semantic_key() != reference.intent.semantic_key():
        raise InvalidParserOutput("Parser output contradicts the explicit request")
    return result


def get_intent_parser() -> IntentParser:
    return DeterministicIntentParser()
