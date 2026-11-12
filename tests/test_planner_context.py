"""Guard the context boundary that separates someone else's car search."""
from copy import deepcopy
import json
from types import SimpleNamespace

from google.genai import types
import pytest

from planner import GeminiPlanner


@pytest.fixture
def state():
    return {
        "shown_ids": [20, 12], "selected_id": 20,
        "preferences": {"max_budget_aed": "30000", "needs": "automatic with child-seat space"},
        "lead": {"budget_max_aed": 30000, "needs": "automatic with child-seat space"},
        "active_search": {"filters": {"make": "Ford", "max_price": 30000}, "order": "lowest_price", "bookable_only": True, "next_offset": 5},
        "pending_booking": {"listing_id": 20, "kind": "test_drive", "day": "2030-11-11"},
        "recent_turns": [{"message": "I need an automatic car with child-seat space.",
                          "reply": "I saved those needs.", "cars": []}],
    }


def capture_request(monkeypatch, message, state):
    # The planner contract is tested without a client, credential, or network call.
    planner = object.__new__(GeminiPlanner)
    planner.tool = types.Tool()
    captured = {}

    def generate(contents, config):
        captured.update(json.loads(contents))
        return SimpleNamespace(function_calls=[SimpleNamespace(
            name="search_inventory", args={"filters": {"make": "Mazda", "max_price": 40000}}
        )])

    monkeypatch.setattr(planner, "_generate", generate)
    name, action = planner.plan(message, state)
    assert name == "search_inventory" and action.filters.make == "Mazda"
    assert captured["shopper_message"] == message
    return captured["context"]


@pytest.mark.parametrize("person", ["brother", "colleague"])
def test_third_party_search_drops_personal_constraints_but_keeps_reference_ids(monkeypatch, state, person):
    original = deepcopy(state)
    context = capture_request(monkeypatch, f"For my {person}, show me a Mazda under AED 40,000.", state)
    assert context["preferences"] == {}
    assert context["lead"] == {}
    assert context["active_search"] is None
    assert context["recent_turns"] == []
    assert context["separate_shopper_request"] is True
    assert context["shown_ids"] == [20, 12] and context["selected_id"] == 20
    assert state == original  # Isolation changes model input, never persisted memory.


def test_ordinary_followup_keeps_personal_search_and_pending_booking_context(monkeypatch, state):
    original = deepcopy(state)
    context = capture_request(monkeypatch, "Actually, what about a Mazda for me instead?", state)
    assert context["preferences"] == state["preferences"]
    assert context["lead"] == state["lead"]
    assert context["active_search"] == state["active_search"]
    assert context["pending_booking"] == state["pending_booking"]
    assert context["recent_turns"][0]["shopper"] == state["recent_turns"][0]["message"]
    assert context["shown_ids"] == [20, 12] and context["selected_id"] == 20
    assert not context.get("separate_shopper_request")
    assert state == original
