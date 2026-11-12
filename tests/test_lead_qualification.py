"""Collect a lead through natural follow-ups without copying unrelated memory."""

import csv

import pytest

from assistant_service import supported_preferences
from planner import Preference, _agent_context
from test_agent_workspace import send, state, workspace


def test_current_inquiry_completeness_does_not_borrow_personal_preferences(workspace):
    current = state(workspace)
    current["preferences"] = {"max_budget_aed": 50000, "needs": "seven seats"}
    current["lead"] = {"budget_max_aed": None, "needs": "Nissan"}
    context = _agent_context("What options are available?", current)
    assert context["preferences"]["max_budget_aed"] == 50000
    assert context["lead_collection"]["missing_fields"] == ["price_range"]
    assert context["lead_collection"]["budget_recorded"] is False
    assert context["lead_collection"]["needs_recorded"] is True


def test_new_third_party_search_cannot_inherit_complete_personal_lead(workspace):
    current = state(workspace)
    current["preferences"] = {"max_budget_aed": 50000, "needs": "seven seats"}
    current["lead"] = {"budget_max_aed": 50000, "needs": "seven seats"}
    context = _agent_context("Find a car for my brother.", current)
    assert context["preferences"] == {} and context["lead"] == {}
    assert context["lead_collection"]["missing_fields"] == ["price_range", "needs"]
    assert not context["lead_collection"]["complete"]
    assert current["lead"]["budget_max_aed"] == 50000


def test_budget_and_literal_short_needs_answers_are_committed_to_csv_without_booking(workspace):
    store, planner, _, sid = workspace

    def browse(message, working, call):
        observation = call("search_inventory", filters={})
        assert observation["lead_collection"]["missing_fields"] == ["price_range", "needs"]
        return observation["lead_collection"]["next_question"]

    planner.callbacks.append(browse)
    send(workspace, "What cars are available?")

    def budget(message, working, call):
        context = _agent_context(message, working)
        assert working["lead"]["budget_min_aed"] == 20000
        assert working["lead"]["budget_max_aed"] == 30000
        assert context["lead_collection"]["missing_fields"] == ["needs"]
        return context["lead_collection"]["next_question"]

    planner.callbacks.append(budget)
    send(workspace, "Between AED 20,000 and AED 30,000")
    requirements = "Automatic with room for two child seats"

    def needs(message, working, call):
        observation = call("update_preferences", preferences=[
            {"key": "needs", "value": requirements, "evidence": requirements},
        ])
        assert observation["accepted_preferences"] == {"needs": requirements}
        assert observation["lead_collection"]["complete"] is True
        assert observation["lead_collection"]["next_question"] is None
        # Collection participates in the same atomic turn, never a separate write.
        assert store.state("shopper", sid)["lead"]["needs"] is None
        return "I have recorded those requirements."

    planner.callbacks.append(needs)
    send(workspace, requirements)
    with store.leads_path.open(encoding="utf-8", newline="") as source:
        row = next(row for row in csv.DictReader(source) if row["session_id"] == sid)
    assert row["budget_min_aed"] == "20000" and row["budget_max_aed"] == "30000"
    assert row["needs"] == requirements
    assert state(workspace)["bookings"] == []


@pytest.mark.parametrize(("message", "value", "prior_reply"), [
    ("Automatic with child-seat space", "Automatic with child-seat space", "What's its mileage?"),
    ("Automatic", "Automatic and seven seats", "What features do you need?"),
    ("Automatic?", "Automatic?", "What features do you need?"),
    ("Not sure", "Not sure", "What features do you need?"),
    ("No preference", "No preference", "What features do you need?"),
    ("Imagine automatic with seven seats", "Imagine automatic with seven seats", "What features do you need?"),
    ("Automatic for my brother", "Automatic for my brother", "What features do you need?"),
])
def test_needs_answer_must_be_literal_asserted_and_directly_requested(message, value, prior_reply):
    change = Preference(key="needs", value=value, evidence=message)
    assert supported_preferences([change], message, prior_reply=prior_reply) == {}


def test_third_party_followup_does_not_save_short_answer_as_personal_preference(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Toyota"}, "subject": "other"}),
                  reply="What other features does your brother need?")
    send(workspace, "Find a Toyota for my brother.")

    def needs_for_other(message, working, call):
        observation = call("update_preferences", preferences=[
            {"key": "needs", "value": message, "evidence": message},
        ])
        assert observation["accepted_preferences"] == {}
        return "We can keep looking for your brother."

    planner.callbacks.append(needs_for_other)
    send(workspace, "Automatic with child-seat space")
    assert state(workspace)["preferences"] == {}
