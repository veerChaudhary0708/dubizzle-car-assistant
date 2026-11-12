"""Standalone confirmation commands bind only to an exposed current proposal."""

from datetime import timedelta

import pytest

from booking import AFFIRMATIVE
from booking_helpers import appointment_rows, prepare
from storage import RequestConflict
from test_agent_workspace import workspace, send, state
from test_storage import next_monday


@pytest.mark.parametrize("message", [
    "yes confirm it",
    "confirm the booking",
    "yes, please confirm",
    "Yes, please confirm it!",
    "Please confirm my appointment.",
    "yes, confirm the test drive, please",
])
def test_standalone_confirmation_commits_exact_proposal_without_model(workspace, monkeypatch, message):
    store, planner, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Accepted confirmation must not call the model.")

    monkeypatch.setattr(planner, "run_turn", forbidden)
    result = send(workspace, message, request_id="confirm-language-once")
    receipt = result["booking"]
    assert receipt["status"] == "confirmed"
    assert (receipt["listing_id"], receipt["starts_at"], receipt["kind"]) == (
        proposal["listing_id"], proposal["starts_at"], proposal["kind"])
    assert result["confirmed_proposal_id"] == proposal["proposal_id"]
    assert state(workspace)["pending_booking"] is None
    assert send(workspace, message, request_id="confirm-language-once") == result
    assert len(appointment_rows(store)) == 1
    with pytest.raises(RequestConflict):
        send(workspace, "a different confirmation message", request_id="confirm-language-once")


@pytest.mark.parametrize("message", [
    "yes confirm it tomorrow",
    "yes, but make it 11AM",
    "confirm the booking for another car",
    "confirm it as a viewing instead",
    "yes, but first what is its mileage?",
    "don't confirm it",
    "do not confirm the booking",
    "should I confirm the booking?",
    "confirm the booking?",
    "yes confirm it and cancel it",
    'He said "confirm the booking".',
])
def test_questions_negation_and_modifiers_are_not_standalone_acceptance(message):
    assert AFFIRMATIVE.fullmatch(message) is None


@pytest.mark.parametrize("change", ["time", "date", "car", "kind"])
def test_confirmation_with_changed_details_prepares_new_review_without_reserving(workspace, change):
    store, planner, _, _ = workspace
    original = prepare(workspace)["booking_proposal"]
    args = {"kind": None}
    if change == "time":
        args["time"] = "11:00"
        message = "yes confirm it, but make it 11AM instead"
    elif change == "date":
        args["day"] = (next_monday() + timedelta(days=1)).isoformat()
        message = f"confirm the booking on {args['day']} instead"
    elif change == "car":
        args["car"] = {"listing_id": 1}
        message = "yes, please confirm it for listing 1 instead"
    else:
        args["kind"] = "viewing"
        message = "confirm it as a viewing instead"
    planner.queue(("prepare_booking", args), reply="Review the updated appointment before confirming.")
    result = send(workspace, message)
    revised = result["booking_proposal"]
    assert result["booking"] is None and appointment_rows(store) == []
    assert revised["proposal_id"] != original["proposal_id"]
    assert revised["status"] == "awaiting_confirmation"


def test_confirmation_command_without_immediately_exposed_proposal_stays_conversational(workspace):
    store, planner, _, _ = workspace
    planner.queue(reply="There is no appointment proposal to confirm yet.")
    fresh = send(workspace, "confirm the booking")
    assert fresh["booking"] is None
    proposal = prepare(workspace)["booking_proposal"]
    planner.queue(reply="The odometer reads 164,000 km.")
    send(workspace, "What is its mileage?")
    planner.queue(reply="You can confirm the exact current proposal using its confirmation button.")
    later = send(workspace, "yes confirm it")
    assert later["booking"] is None and appointment_rows(store) == []
    assert state(workspace)["pending_booking"]["proposal_id"] == proposal["proposal_id"]
