"""Extract explicit AED search bounds without trusting model-proposed numbers.

This is a conservative boundary parser, not a general intent classifier. It
returns no bounds when none are stated and flags conflicting/unsupported money
constraints instead of deciding which one the shopper intended.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re


@dataclass(frozen=True)
class PriceConstraints:
    bounds: dict[str, int | bool] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()
    ambiguous: bool = False
    reason: str | None = None


_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_AMOUNT = re.compile(
    rf"(?<![\w.,])(?:(?P<before>AED|Dhs?|dirhams?)\s*)?"
    rf"(?P<number>{_NUMBER})\s*(?P<scale>k|thousand|m|million)?"
    rf"(?:\s*(?P<after>AED|Dhs?|dirhams?))?(?!\w|\.\d|,\d)", re.I)
_SCALE = {"": 1, "k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000}
_MEASUREMENT_AFTER = re.compile(
    r"^\s*(?:km\b|kilomet(?:er|re)s?\b|miles?\b|cc\b|lit(?:er|re)s?\b|l/100|km/l|mpg\b|bhp\b|hp\b|cylinders?\b|seats?\b|years?\b|months?\b|days?\b|hours?\b|minutes?\b|%)", re.I)
_METRIC = re.compile(r"\b(price|budget|cost|spend|pay|afford|limit|ceiling|mileage|odometer|kilometres?|kilometers?|model year|year|engine|horsepower|seat|seats|appointment|time)\b", re.I)
_PRICE_METRICS = {"price", "budget", "cost", "spend", "pay", "afford", "limit", "ceiling"}
_FOREIGN_BEFORE = re.compile(r"(?:[$€£]|\b(?:USD|EUR|GBP|dollars?|euros?|pounds?))\s*$", re.I)
_FOREIGN_AFTER = re.compile(r"^\s*(?:USD|EUR|GBP|dollars?|euros?|pounds?)\b", re.I)
_BOUND_PATTERNS = (
    ("max", True, re.compile(r"\b(?:strictly\s+)?(?:under|below|less than|cheaper than)\s*$", re.I)),
    ("max", False, re.compile(r"\b(?:up to(?: and including)?|at most|no more than|not more than|maximum(?:\s+(?:price|budget))?(?:\s+(?:is|of))?|max(?:\s+(?:price|budget))?(?:\s+(?:is|of))?)\s*$", re.I)),
    ("min", True, re.compile(r"\b(?:strictly\s+)?(?:over|above|more than|greater than)\s*$", re.I)),
    ("min", False, re.compile(r"\b(?:at least|no less than|minimum(?:\s+(?:price|budget))?(?:\s+(?:is|of))?|min(?:\s+(?:price|budget))?(?:\s+(?:is|of))?|starting (?:at|from)|from)\s*$", re.I)),
)
_BUDGET = re.compile(r"\b(?:budget|price limit|spending limit|maximum spend|ceiling|cap|can (?:spend|afford|pay))\s*(?:(?:is|of|at|:|=)\s*)?$", re.I)
_RANGE_PREFIX = re.compile(r"\b(?:between|from|range(?:\s+(?:is|of))?)\s*$", re.I)


def _has_currency(match):
    return bool(match["before"] or match["after"])


def _amount(match, inherited_scale=""):
    scale = (match["scale"] or inherited_scale or "").casefold()
    try:
        number = Decimal(match["number"].replace(",", "")) * _SCALE[scale]
    except (InvalidOperation, KeyError):
        return None
    if number != number.to_integral_value() or number < 0 or number > 9_999_999_999:
        return None
    return int(number)


def _non_price_quantity(message, match):
    if _MEASUREMENT_AFTER.search(message[match.end():match.end() + 30]):
        return True
    if _has_currency(match):
        return False
    before = message[:match.start()]
    # Dates/times and engine quantities must not turn into budget constraints.
    if (before.endswith(":") and match.start() >= 2 and before[-2].isdigit()) or message[match.end():].startswith(":"):
        return True
    segment = re.split(r"[;!?\n,]|\b(?:and|but)\b", before, flags=re.I)[-1]
    markers = list(_METRIC.finditer(segment))
    if markers and markers[-1][1].casefold() not in _PRICE_METRICS:
        return True
    # A bare model year without a monetary cue is not a price, even near 'under'.
    number = _amount(match)
    return bool(number is not None and 1900 <= number <= 2100 and not match["scale"]
                and not any(marker[1].casefold() in _PRICE_METRICS for marker in markers))


def extract_price_constraints(message: str) -> PriceConstraints:
    """Return latest-message price bounds, literal evidence and ambiguity status.

    Search filters and caller state are intentionally absent: they must not bias
    extraction toward a number the model already invented.
    """
    matches = list(_AMOUNT.finditer(message))
    consumed = set()
    proposals = []  # (min|max, integer amount, exclusive, source text)
    ambiguous = []
    for i, left in enumerate(matches[:-1]):
        right = matches[i + 1]
        connector = message[left.end():right.start()].strip()
        if not re.fullmatch(r"(?:-|–|—|to|and)", connector, re.I):
            continue
        prefix = message[:left.start()]
        range_cue = _RANGE_PREFIX.search(prefix)
        explicit_money = _has_currency(left) or _has_currency(right)
        if not (range_cue or explicit_money or left["scale"] or right["scale"]):
            continue
        if _non_price_quantity(message, left) or _non_price_quantity(message, right):
            continue
        if _FOREIGN_BEFORE.search(prefix) or _FOREIGN_AFTER.search(message[right.end():]):
            ambiguous.append("An unsupported currency was supplied; an AED limit cannot be inferred.")
            consumed.update((i, i + 1))
            continue
        inherited = right["scale"] if not left["scale"] and _amount(left) is not None and _amount(left) < 1000 else ""
        lower, upper = _amount(left, inherited), _amount(right)
        evidence = message[range_cue.start() if range_cue else left.start():right.end()].strip()
        consumed.update((i, i + 1))
        if lower is None or upper is None or lower > upper:
            ambiguous.append("The supplied price range is invalid or cannot be represented in whole AED.")
        else:
            proposals.extend((("min", lower, False, evidence), ("max", upper, False, evidence)))

    for i, match in enumerate(matches):
        if i in consumed or _non_price_quantity(message, match):
            continue
        prefix = message[:match.start()]
        suffix = message[match.end():]
        foreign = _FOREIGN_BEFORE.search(prefix) or _FOREIGN_AFTER.search(suffix)
        # Remove a foreign currency marker only to recognize that this was a
        # monetary request; never convert it or treat the amount as AED.
        searchable_prefix = _FOREIGN_BEFORE.sub("", prefix) if foreign else prefix
        bound = next(((kind, exclusive, found) for kind, exclusive, pattern in _BOUND_PATTERNS
                      if (found := pattern.search(searchable_prefix))), None)
        budget = _BUDGET.search(searchable_prefix)
        if bound is None and budget is None:
            continue
        start = bound[2].start() if bound else budget.start()
        evidence = message[start:match.end()].strip()
        if foreign:
            ambiguous.append("An unsupported currency was supplied; an AED limit cannot be inferred.")
            continue
        if re.search(r"\b(?:not|don't|do not|isn't)\s*$", searchable_prefix[:start], re.I):
            ambiguous.append("A negated price condition needs clarification.")
            continue
        amount = _amount(match)
        if amount is None:
            ambiguous.append("The price cannot be represented as a nonnegative whole-AED amount.")
            continue
        kind, exclusive = (bound[0], bound[1]) if bound else ("max", False)
        proposals.append((kind, amount, exclusive, evidence))

    evidence = tuple(dict.fromkeys(item[3] for item in proposals))
    if ambiguous:
        return PriceConstraints(evidence=evidence, ambiguous=True, reason=" ".join(dict.fromkeys(ambiguous)))
    bounds = {}
    for kind in ("min", "max"):
        values = {(amount, exclusive) for side, amount, exclusive, _ in proposals if side == kind}
        amounts = {amount for amount, _ in values}
        if len(amounts) > 1:
            return PriceConstraints(evidence=evidence, ambiguous=True,
                                    reason=f"Different {kind}imum price conditions were supplied; clarify which applies.")
        if values:
            # An inclusive budget and a strict search at the same amount agree:
            # satisfying the strict condition satisfies both. Preserve that
            # intersection instead of asking which identical number applies.
            bounds[kind + "_price"] = amounts.pop()
            bounds[kind + "_exclusive"] = any(exclusive for _, exclusive in values)
    if "min_price" in bounds and "max_price" in bounds:
        if bounds["min_price"] > bounds["max_price"] or (bounds["min_price"] == bounds["max_price"]
            and (bounds["min_exclusive"] or bounds["max_exclusive"])):
            return PriceConstraints(evidence=evidence, ambiguous=True, reason="The price bounds describe an empty range.")
    return PriceConstraints(bounds=bounds, evidence=evidence)
