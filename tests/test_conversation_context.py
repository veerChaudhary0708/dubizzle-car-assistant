"""Conversation transport preserves the question the shopper is answering."""

import copy
import json

from conversation_context import dialogue_contents


def test_complete_question_and_native_roles_survive_long_assistant_reply():
    full_reply = "Detailed car information. " * 80 + "Would you like a test drive?"
    context = {"selected_id": 20, "recent_turns": [
        {"shopper": "Tell me about the Mustang.", "reply": full_reply},
        {"shopper": "What does V6 mean?", "reply": "It describes a six-cylinder engine arrangement."},
    ]}
    original = copy.deepcopy(context)
    contents = dialogue_contents("Yes, please.", context)
    assert [item.role for item in contents] == ["user", "model", "user", "model", "user"]
    assert contents[1].parts[0].text == full_reply
    assert contents[3].parts[0].text == context["recent_turns"][1]["reply"]
    latest = json.loads(contents[-1].parts[0].text)
    assert latest["shopper_message"] == "Yes, please."
    assert latest["context"]["selected_id"] == 20
    assert "recent_turns" not in latest["context"]
    assert context == original


def test_historical_booking_claim_remains_distinct_from_recorded_outcome():
    context = {"bookings": [], "recent_turns": [{
        "shopper": "Book a test drive.", "reply": "I booked it.",
        "booking_outcome": {"status": "rejected"}, "booking_receipt": None,
    }]}
    contents = dialogue_contents("Where is the receipt?", context)
    assert contents[1].parts[0].text == "I booked it."
    latest = json.loads(contents[-1].parts[0].text)["context"]
    assert latest["bookings"] == []
    assert latest["recent_action_records"][0]["booking_outcome"]["status"] == "rejected"
    assert latest["recent_action_records"][0]["booking_receipt"] is None


def test_empty_or_scoped_history_cannot_reintroduce_old_conversation():
    contents = dialogue_contents("Help find a car for my brother.", {"preferences": {}, "recent_turns": []})
    assert len(contents) == 1
    latest = json.loads(contents[0].parts[0].text)
    assert latest["context"]["preferences"] == {}
    assert latest["context"]["recent_action_records"] == []
