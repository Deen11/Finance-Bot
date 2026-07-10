"""Deterministic rules used when the AI parser needs a safety net."""

import math
import re
from typing import Optional


VALID_CATEGORIES = {
    "Food",
    "Transport",
    "Shopping",
    "Family",
    "Savings",
    "Subscriptions",
    "Income",
    "Other",
}

_AMOUNT_NUMBER = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PREFIXED_AMOUNT_RE = re.compile(
    rf"(?:S\$|SGD|\$)\s*(?P<amount>{_AMOUNT_NUMBER})",
    re.I,
)
_SUFFIXED_AMOUNT_RE = re.compile(
    rf"(?P<amount>{_AMOUNT_NUMBER})\s*SGD\b",
    re.I,
)
_BARE_AMOUNT_RE = re.compile(
    rf"(?<![\w.])(?P<amount>{_AMOUNT_NUMBER})(?![\w.])",
    re.I,
)

_EXPLICIT_CATEGORY_PATTERNS = {
    "Food": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+(?:the\s+)?food\b",
    "Transport": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+transport\b",
    "Shopping": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+shopping\b",
    "Family": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+family\b",
    "Savings": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+savings?\b",
    "Subscriptions": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+subscriptions?\b",
    "Income": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+income\b",
    "Other": r"\b(?:under|as|category|categorise(?:d)?\s+as|categorize(?:d)?\s+as)\s+other\b",
}

_FAMILY_RE = re.compile(r"\b(?:mom|mum|mother|parents?|family|dad|father)\b", re.I)
_SAVINGS_RE = re.compile(r"\b(?:savings?|maribank|mari\s+bank|fixed\s+deposit)\b", re.I)
_SUBSCRIPTIONS_RE = re.compile(
    r"\b(?:netflix|spotify|apple\s+music|youtube\s+premium|icloud|subscription|prime\s+video|disney\+?)\b",
    re.I,
)
_TRANSPORT_RE = re.compile(
    r"\b(?:bus|mrt|grab|gojek|taxi|simplygo|ez[\s-]?link|transport|uber|petrol|parking|erp)\b",
    re.I,
)
_SHOPPING_RE = re.compile(
    r"\b(?:clothes?|shoes?|electronics?|games?|beauty|gadgets?|gifts?|shopping|earphones?|headphones?|phone|watch|ipad|laptop|guardian|watsons|water\s+(?:heater|filter|bottle|dispenser))\b",
    re.I,
)
_FOOD_RE = re.compile(
    r"\b(?:food|meal|breakfast|brunch|lunch|dinner|supper|hawker|kopitiam|restaurant|cafe|"
    r"snacks?|groceries|chicken\s+rice|nasi\s+lemak|prata|rice|noodles?|pasta|pizza|burgers?|"
    r"sandwich|sushi|ramen|curry|dessert|cake|ice\s+cream|mcdonald'?s?|kfc|burger\s+king|"
    r"subway|foodpanda|grabfood|deliveroo|starbucks|toast\s+box|ya\s+kun)\b",
    re.I,
)
_BEVERAGE_RE = re.compile(
    r"\b(?:drinks?|beverages?|water|mineral\s+water|vitamin\s+(?:c\s+)?water|juice|coffee|tea|"
    r"kopi|teh|milo|boba|bubble\s+tea|soft\s+drink|soda|coke|pepsi|beer|wine)\b",
    re.I,
)
_NON_FOOD_WATER_RE = re.compile(r"\b(?:water\s+bill|utilities?)\b", re.I)


def parse_positive_amount(value: object) -> Optional[float]:
    """Return a finite positive amount, or ``None`` for unsafe values."""
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, str):
        cleaned = value.strip()
        cleaned = re.sub(r"^(?:S\$|SGD|\$)\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*SGD$", "", cleaned, flags=re.I)
        cleaned = cleaned.replace(",", "").strip()
    else:
        cleaned = value

    try:
        amount = float(cleaned)
    except (TypeError, ValueError, OverflowError):
        return None

    if not math.isfinite(amount) or amount <= 0:
        return None
    return amount


def extract_transaction_amount(text: str) -> Optional[float]:
    """Extract an amount, prioritizing explicit SGD/currency notation."""
    for pattern in (_PREFIXED_AMOUNT_RE, _SUFFIXED_AMOUNT_RE, _BARE_AMOUNT_RE):
        match = pattern.search(text)
        if match:
            # If the selected monetary token is invalid (for example "$-5"),
            # reject the message instead of silently turning it into a positive value.
            return parse_positive_amount(match.group("amount"))
    return None


def explicit_category(text: str) -> Optional[str]:
    """Return a category the user explicitly requested, if present."""
    for category, pattern in _EXPLICIT_CATEGORY_PATTERNS.items():
        if re.search(pattern, text, re.I):
            return category
    return None


def infer_category(text: str, trans_type: str = "expense") -> str:
    """Infer a category locally when the model is unavailable or uncertain."""
    requested = explicit_category(text)
    if requested:
        return requested

    if trans_type.lower() == "income":
        return "Income"
    if _FAMILY_RE.search(text):
        return "Family"
    if _SAVINGS_RE.search(text):
        return "Savings"
    if _SUBSCRIPTIONS_RE.search(text):
        return "Subscriptions"
    if _TRANSPORT_RE.search(text):
        return "Transport"
    if _SHOPPING_RE.search(text):
        return "Shopping"
    if _FOOD_RE.search(text):
        return "Food"
    if _BEVERAGE_RE.search(text) and not _NON_FOOD_WATER_RE.search(text):
        return "Food"
    return "Other"


def canonical_category(value: object) -> str:
    """Normalize model output to one of the categories supported by the sheet."""
    candidate = str(value or "Other").strip().title()
    return candidate if candidate in VALID_CATEGORIES else "Other"


def resolve_category(
    model_category: object,
    *,
    source_text: str = "",
    description: str = "",
    trans_type: str = "expense",
) -> str:
    """Combine an explicit request, model result, and deterministic fallback."""
    requested = explicit_category(source_text)
    if requested is not None:
        return requested
    if trans_type.lower() == "income":
        return "Income"

    category = canonical_category(model_category)
    if category == "Other":
        category = infer_category(f"{source_text} {description}".strip(), trans_type)
    return category


def format_parse_prompt(template: str, *, today: str, message: str) -> str:
    """Fill the AI prompt in one pass so escaped JSON braces stay intact."""
    return template.format(today=today, message=message)
