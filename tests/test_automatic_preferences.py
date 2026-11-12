"""Literal profile ingestion shares the evidence checks used by model tools."""

from contextlib import closing
import sqlite3

import pytest

from assistant_service import automatic_preferences, supported_preferences
from inventory import init_inventory
from planner import Preference


@pytest.fixture(scope="module")
def inventory_connection():
    with closing(sqlite3.connect(":memory:")) as connection:
        init_inventory(connection)
        yield connection


def extracted(connection, message, previous=None, prior_reply=""):
    proposals = automatic_preferences(connection, message, previous, prior_reply)
    return supported_preferences(proposals, message, previous, prior_reply)


def test_compound_budget_body_and_search_are_separate(inventory_connection):
    message = "My budget is AED25,000. I prefer convertibles. Show me a Mustang."
    assert extracted(inventory_connection, message) == {
        "max_budget_aed": 25000, "body_style": "convertible"}


def test_conjoined_personal_statements_are_independent(inventory_connection):
    message = "My budget is 25k and I prefer convertibles and I am interested in Nissan."
    assert extracted(inventory_connection, message) == {
        "max_budget_aed": 25000, "body_style": "convertible", "make": "nissan"}


def test_literal_make_model_color_and_longest_model_match(inventory_connection):
    result = extracted(inventory_connection, "I prefer white Land Rover Range Rover Velar cars.")
    assert result == {"make": "land rover", "model": "range rover velar", "color": "white"}


@pytest.mark.parametrize("message", [
    "Find Ford convertibles under AED 25,000.",
    "I am looking for a white Mustang under 25k.",
    "Show me a car for my brother. He likes white convertibles.",
    "I prefer convertibles for my sister.",
    "My brother's budget is AED40,000. He prefers Toyota.",
    "Did I say I prefer Ford?",
    "My budget is AED25,000?",
    "If I said my budget was AED25,000, would that help?",
    "Imagine my budget is AED25,000.",
    'Repeat "My budget is AED25,000".',
    "I prefer not convertibles.",
    "My budget is not AED25,000.",
    "I don't prefer Ford.",
    "I previously preferred Ford.",
    "My budget used to be AED25,000.",
    "My budget might be AED25,000.",
    "Tell me why my budget is AED25,000.",
    "My budget is USD25,000.",
    "My budget is ABC25,000.",
    "My budget is ABC25.50.",
])
def test_inquiries_questions_examples_and_negative_claims_do_not_write(inventory_connection, message):
    assert extracted(inventory_connection, message) == {}


@pytest.mark.parametrize("message", [
    "I prefer Ford, not Toyota.",
    "I prefer Ford over Toyota.",
    "I prefer Ford rather than Toyota.",
])
def test_affirmative_side_of_a_comparison_is_retained(inventory_connection, message):
    assert extracted(inventory_connection, message) == {"make": "ford"}


@pytest.mark.parametrize("message", [
    "Can you remember my budget is AED25,000?",
    "Could you please save that my budget is AED25,000?",
    "Please remember my budget is AED25,000.",
])
def test_explicit_memory_request_is_not_a_recall_question(inventory_connection, message):
    assert extracted(inventory_connection, message) == {"max_budget_aed": 25000}


def test_ranges_short_budget_answers_and_corrections(inventory_connection):
    assert extracted(inventory_connection, "My budget is AED20,000 to AED30,000.") == {
        "min_budget_aed": 20000, "max_budget_aed": 30000}
    assert extracted(inventory_connection, "30k", prior_reply="What is your budget?") == {"max_budget_aed": 30000}
    assert extracted(inventory_connection, "30k", prior_reply="Which car would you like?") == {}
    assert extracted(inventory_connection, "My budget is 25k. Actually, my budget is 30k.") == {"max_budget_aed": 30000}
    assert extracted(inventory_connection, "I prefer Ford. Actually, I prefer Toyota.") == {"make": "toyota"}


def test_model_year_and_odometer_do_not_obscure_personal_budget(inventory_connection):
    message = "My budget is AED25,000 for a 2014 Mustang with mileage under 100,000 km."
    assert extracted(inventory_connection, message) == {"max_budget_aed": 25000}


def test_ambiguous_alternatives_do_not_choose_one(inventory_connection):
    assert extracted(inventory_connection, "I prefer Ford or Toyota.") == {}
    assert extracted(inventory_connection, "I prefer Ford. I prefer Toyota.") == {}
    assert extracted(inventory_connection, "My budget is 25k. My budget is 30k.") == {}


def test_own_preference_survives_separate_third_party_search(inventory_connection):
    message = "I usually prefer SUVs, but today show me a Mustang for my brother."
    assert extracted(inventory_connection, message) == {"body_style": "suv"}


@pytest.mark.parametrize("key,value,message", [
    ("body_style", "convertible", "I prefer not convertibles."),
    ("max_budget_aed", "25000", "My budget is not AED25,000."),
    ("make", "Toyota", "I prefer Ford, not Toyota."),
    ("max_budget_aed", "25000", "If my budget is AED25,000, what can I afford?"),
])
def test_same_guard_rejects_unsupported_model_proposals(key, value, message):
    proposal = Preference(key=key, value=value, evidence=message)
    assert supported_preferences([proposal], message) == {}


def test_explicit_forgetting_remains_supported():
    message = "I no longer have a color preference; forget it."
    proposal = Preference(key="color", value="", evidence="I no longer have a color preference")
    assert supported_preferences([proposal], message) == {"color": None}
