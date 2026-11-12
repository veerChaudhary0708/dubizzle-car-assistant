"""Explicit search prices are extracted independently of proposed model filters."""
import pytest

from constraints import extract_price_constraints


@pytest.mark.parametrize("message,expected", [
    ("Find a Mustang under AED 20,000.", {"max_price": 20000, "max_exclusive": True}),
    ("Find cars below 20k dirhams.", {"max_price": 20000, "max_exclusive": True}),
    ("Show cars up to AED 20,000.", {"max_price": 20000, "max_exclusive": False}),
    ("Maximum price is AED 30,000.", {"max_price": 30000, "max_exclusive": False}),
    ("My budget is AED 25,000.", {"max_price": 25000, "max_exclusive": False}),
    ("My budget is AED 25,000. I prefer convertibles. Find a Ford Mustang under AED 25,000.",
     {"max_price": 25000, "max_exclusive": True}),
    ("Find a Mustang under AED 25,000. My budget is AED 25,000.",
     {"max_price": 25000, "max_exclusive": True}),
    ("Prices at least AED 15,000 and strictly above AED 15,000.",
     {"min_price": 15000, "min_exclusive": True}),
    ("Find a car for at least AED 15,000.", {"min_price": 15000, "min_exclusive": False}),
    ("Only prices over AED 15k.", {"min_price": 15000, "min_exclusive": True}),
    ("Find cars between AED 20k and 30k.", {"min_price": 20000, "min_exclusive": False, "max_price": 30000, "max_exclusive": False}),
    ("My budget is 20–30k AED.", {"min_price": 20000, "min_exclusive": False, "max_price": 30000, "max_exclusive": False}),
    ("Find cars from AED 20,000 to AED 30,000.", {"min_price": 20000, "min_exclusive": False, "max_price": 30000, "max_exclusive": False}),
    ("Under AED 20.5k please.", {"max_price": 20500, "max_exclusive": True}),
    ("Price under 1.2 million dirhams.", {"max_price": 1200000, "max_exclusive": True}),
    ("2014 Mustang, under 100,000 km, price below AED 20,000.", {"max_price": 20000, "max_exclusive": True}),
    ("Cars after model year 2015, mileage below 80k, budget is AED 35k.", {"max_price": 35000, "max_exclusive": False}),
])
def test_explicit_bounds_preserve_value_unit_and_comparator(message, expected):
    result = extract_price_constraints(message)
    assert result.ambiguous is False and result.bounds == expected
    assert result.evidence and all(quote in message for quote in result.evidence)


@pytest.mark.parametrize("message", [
    "Show a 2014 Ford Mustang V6 with 164,000 km.",
    "Cars with mileage under 20,000 km.",
    "Odometer below 25k.",
    "Cars between model years 2014 and 2018.",
    "Find cars with engines under 2000 cc.",
    "Book a viewing at 10:30 tomorrow.",
    "The listing costs AED 21,500; what is its mileage?",
    "What cars are available?",
])
def test_non_price_quantities_and_facts_do_not_create_search_bounds(message):
    result = extract_price_constraints(message)
    assert result.bounds == {} and result.ambiguous is False


@pytest.mark.parametrize("message", [
    "Find cars under USD 20k.",
    "My budget is $20,000.",
    "Show cars not under AED 20,000.",
    "Find cars under AED 20,000 or up to AED 30,000.",
    "My budget is AED 25,000. Find a Mustang under AED 30,000.",
    "Prices at least AED 25,000 and under AED 25,000.",
    "Find cars between AED 30,000 and AED 20,000.",
    "Cars above AED 30,000 and below AED 20,000.",
])
def test_conflicting_or_unsupported_constraints_are_not_guessed(message):
    result = extract_price_constraints(message)
    assert result.ambiguous is True and result.bounds == {} and result.reason
