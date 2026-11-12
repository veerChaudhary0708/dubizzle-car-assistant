"""Explicit personal memory is staged even when the agent omits a save tool."""

import pytest

from planner import ModelUnavailable
from test_agent_workspace import send, state, workspace


def test_compound_preferences_are_visible_before_search_and_survive_a_new_chat(workspace):
    store, planner, _, sid = workspace

    def search_only(message, working, call):
        assert working["preferences"]["max_budget_aed"] == 25000
        assert working["preferences"]["body_style"].lower() == "convertible"
        # Automatic extraction joins the staged workspace, not a separate write.
        assert store.state("shopper", sid)["preferences"] == {}
        found = call("search_inventory", filters={"make": "Ford", "model": "Mustang"})
        assert found["preferences"]["max_budget_aed"] == 25000
        return "The Mustang matches your current search."

    planner.callbacks.append(search_only)
    send(workspace, "My budget is AED 25,000. I prefer convertibles. Show me a Mustang.")
    restored = store.state("shopper", store.create_session("shopper")["session_id"])
    assert restored["preferences"]["max_budget_aed"] == 25000
    assert restored["preferences"]["body_style"].lower() == "convertible"
    assert restored["shown_ids"] == [] and restored["selected_id"] is None
    assert len(store.history("shopper", sid)) == 1


def test_natural_acknowledgment_without_tools_persists_once_and_replays(workspace):
    store, planner, _, sid = workspace

    def acknowledge(message, working, call):
        assert working["preferences"]["make"].lower() == "ford"
        return "I will remember that you prefer Ford."

    planner.callbacks.append(acknowledge)
    message = "I prefer Ford."
    first = send(workspace, message, request_id="automatic-memory-once")
    second = send(workspace, message, request_id="automatic-memory-once")
    assert second == first
    assert state(workspace)["preferences"]["make"].lower() == "ford"
    assert len(store.history("shopper", sid)) == 1


def test_budget_correction_preserves_original_for_same_turn_recall(workspace):
    _, planner, _, _ = workspace
    planner.queue(reply="I have saved your budget.")
    send(workspace, "My budget is AED 25,000.")

    def correct_and_recall(message, working, call):
        assert working["preferences"]["max_budget_aed"] == 30000
        assert working["_original_preferences"]["max_budget_aed"] == 25000
        observation = call("recall_shopper", subject="previous_budget", scope="all")
        assert "25,000" in observation["reply"]
        return "Your budget is now AED30,000; it was AED25,000 before."

    planner.callbacks.append(correct_and_recall)
    send(workspace, "My budget is now AED 30,000. What was my previous budget?")
    assert state(workspace)["preferences"]["max_budget_aed"] == 30000


def test_model_failure_discards_automatic_memory_without_a_save_tool(workspace):
    store, planner, _, sid = workspace
    before = state(workspace)

    def unavailable(message, working, call):
        assert working["preferences"]["max_budget_aed"] == 25000
        assert working["preferences"]["body_style"].lower() == "convertible"
        raise ModelUnavailable("The response could not be completed.")

    planner.callbacks.append(unavailable)
    with pytest.raises(ModelUnavailable):
        send(workspace, "My budget is AED25,000. I prefer convertibles.", request_id="auto-memory-failure")
    assert state(workspace) == before
    assert store.history("shopper", sid) == []
    assert store.replay("shopper", sid, "auto-memory-failure") is None


def test_later_unsupported_model_proposal_cannot_replace_explicit_values(workspace):
    _, planner, _, _ = workspace

    def unsupported_update(message, working, call):
        assert working["preferences"]["max_budget_aed"] == 25000
        assert working["preferences"]["make"].lower() == "ford"
        observation = call("update_preferences", preferences=[
            {"key": "max_budget_aed", "value": "30000", "evidence": "My budget is AED25,000"},
            {"key": "make", "value": "Toyota", "evidence": "I prefer Ford over Toyota"},
        ])
        assert observation["accepted_preferences"] == {}
        assert working["preferences"]["max_budget_aed"] == 25000
        assert working["preferences"]["make"].lower() == "ford"
        return "I have saved your explicit preferences."

    planner.callbacks.append(unsupported_update)
    send(workspace, "My budget is AED25,000. I prefer Ford over Toyota.")
    assert state(workspace)["preferences"]["max_budget_aed"] == 25000
    assert state(workspace)["preferences"]["make"].lower() == "ford"


def test_third_party_search_does_not_automatically_create_personal_preferences(workspace):
    _, planner, _, _ = workspace

    def search_for_brother(message, working, call):
        assert working["preferences"] == {}
        call("search_inventory", filters={"make": "Toyota", "max_price": 40000}, subject="other")
        return "Here is what I found for your brother."

    planner.callbacks.append(search_for_brother)
    send(workspace, "My brother's budget is AED40,000. He prefers Toyota. Find cars for him.")
    assert state(workspace)["preferences"] == {}
