"""A durable draft becomes an appointment only after one explicit confirmation."""

from datetime import timedelta
from uuid import uuid4

import pytest

from assistant_service import Assistant
from storage import BookingError, RequestConflict, SessionNotFound, Store
from test_agent_workspace import workspace, send, state
from test_storage import next_monday
from booking_helpers import appointment_rows, prepare, confirm


def test_missing_details_survive_yes_mixed_question_and_restart(workspace):
    store, planner, _, sid = workspace
    planner.queue(("prepare_booking", {"kind": "test_drive"}))
    first = send(workspace, "Book a test drive.")
    assert first["booking"] is None and not first.get("booking_proposal")
    assert state(workspace)["pending_booking"]["kind"] == "test_drive"
    planner.queue(("prepare_booking", {"kind": "test_drive"}))
    send(workspace, "yes")
    assert appointment_rows(store) == []
    planner.queue(reply="A V6 has six cylinders arranged in two banks.")
    answer = send(workspace, "What does V6 mean?")
    assert "six cylinders" in answer["reply"]
    assert state(workspace)["pending_booking"]["kind"] == "test_drive"

    reopened = Store(store.db_path.parent)
    resumed = (reopened, planner, Assistant(reopened, planner), sid)
    day = next_monday().isoformat()
    planner.queue(("prepare_booking", {"car": {"model": "Mustang"}, "day": day, "time": "10:00", "kind": "test_drive"}))
    prepared = send(resumed, f"The 2014 Ford Mustang, {day} at 10AM.")
    proposal = prepared["booking_proposal"]
    assert proposal["status"] == "awaiting_confirmation"
    assert proposal["listing_id"] == 20 and proposal["kind"] == "test_drive"
    assert appointment_rows(reopened) == []
    result = confirm(resumed, proposal)
    assert result["booking"]["starts_at"] == day + "T10:00:00+04:00"
    assert reopened.list_bookings("shopper") == [result["booking"]]


@pytest.mark.parametrize("path", ["search_then_prepare", "prepare_then_reads", "availability_then_search"])
def test_tool_order_produces_same_proposal_and_never_reserves_without_confirmation(workspace, path):
    store, planner, _, _ = workspace
    planner.queue(("prepare_booking", {"kind": "test_drive"}))
    send(workspace, "I'd like a test drive.")
    day = next_monday().isoformat()

    def execute_sequence(message, working, call):
        arguments = {"car": {"model": "Mustang"}, "day": day, "time": "10:00", "kind": "test_drive"}
        if path == "search_then_prepare":
            call("search_inventory", filters={"make": "Ford", "model": "Mustang"})
            call("prepare_booking", **arguments)
        elif path == "prepare_then_reads":
            call("prepare_booking", **arguments)
            call("available_viewings", car={"model": "Mustang"}, day=day)
            call("read_listing", car={"model": "Mustang"}, attribute="price")
        else:
            call("available_viewings", car={"model": "Mustang"}, day=day)
            call("search_inventory", filters={"model": "Mustang"}, display=False)
            call("prepare_booking", **arguments)
        assert appointment_rows(store) == []
        return "Please review the appointment before confirming."

    planner.callbacks.append(execute_sequence)
    proposed = send(workspace, f"The 2014 Mustang, {day} at 10AM.")
    proposal = proposed["booking_proposal"]
    assert {key: proposal[key] for key in ("listing_id", "kind", "starts_at", "status")} == {
        "listing_id": 20, "kind": "test_drive", "starts_at": day + "T10:00:00+04:00",
        "status": "awaiting_confirmation"}
    assert proposal["proposal_id"] and proposal["car_label"] and proposed["booking"] is None
    assert appointment_rows(store) == []
    result = confirm(workspace, proposal)
    assert store.list_bookings("shopper") == [result["booking"]]


def test_immediate_exact_yes_confirms_without_calling_model_and_replays(workspace, monkeypatch):
    store, planner, _, _ = workspace
    proposed = prepare(workspace)
    proposal = proposed["booking_proposal"]

    def forbidden(*args, **kwargs):
        raise AssertionError("Confirmation must not invoke Gemini.")

    monkeypatch.setattr(planner, "run_turn", forbidden)
    result = send(workspace, "yes", request_id="confirm-chat-once")
    assert result["booking"]["status"] == "confirmed"
    assert result["booking"]["starts_at"] == proposal["starts_at"]
    assert len(appointment_rows(store)) == 1
    assert send(workspace, "yes", request_id="confirm-chat-once") == result


def test_fresh_or_nonexact_yes_does_not_reserve(workspace):
    store, planner, _, _ = workspace
    planner.queue(reply="What would you like to do?")
    result = send(workspace, "yes")
    assert result["booking"] is None
    prepare(workspace)
    planner.queue(reply="The listed odometer is164,000km.")
    answer = send(workspace, "Yes, but first what is its mileage?")
    assert answer["booking"] is None and appointment_rows(store) == []


def test_unrelated_followup_preserves_proposal_for_explicit_button_confirmation(workspace):
    store, planner, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]
    planner.queue(("read_listing", {"car": {"listing_id": 20}, "attribute": "mileage"}),
                  reply="Its odometer reads164,000km.")
    result = send(workspace, "Before deciding, what is its mileage?")
    assert result["booking"] is None
    confirmed = confirm(workspace, proposal)
    assert confirmed["booking"]["starts_at"] == proposal["starts_at"]
    assert len(appointment_rows(store)) == 1


def test_edited_date_invalidates_old_proposal_and_confirms_only_new_snapshot(workspace):
    store, planner, _, _ = workspace
    old = prepare(workspace)["booking_proposal"]
    day = (next_monday() + timedelta(days=1)).isoformat()
    planner.queue(("prepare_booking", {"day": day, "time": "11:00", "kind": "test_drive"}))
    changed = send(workspace, f"Change it to {day} at 11AM.")["booking_proposal"]
    assert changed["proposal_id"] != old["proposal_id"]
    before = state(workspace)
    with pytest.raises(BookingError):
        confirm(workspace, old)
    assert state(workspace) == before and appointment_rows(store) == []
    result = confirm(workspace, changed)
    assert result["booking"]["starts_at"] == day + "T11:00:00+04:00"


def test_cancelled_proposal_and_historical_button_cannot_create_appointment(workspace):
    store, planner, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]
    planner.queue(("cancel_booking", {}))
    send(workspace, "Cancel this draft.")
    assert state(workspace)["pending_booking"] is None
    with pytest.raises(BookingError):
        confirm(workspace, proposal)
    assert appointment_rows(store) == []


def test_confirmation_is_owned_and_new_chat_cannot_use_old_proposal(workspace):
    store, _, assistant, sid = workspace
    proposal = prepare(workspace)["booking_proposal"]
    with pytest.raises(SessionNotFound):
        assistant.confirm_booking("another-shopper", sid, uuid4().hex, proposal["proposal_id"])
    fresh = store.create_session("shopper")["session_id"]
    with pytest.raises(BookingError):
        assistant.confirm_booking("shopper", fresh, uuid4().hex, proposal["proposal_id"])
    assert appointment_rows(store) == []


def test_confirm_replay_is_exact_and_different_request_cannot_double_book(workspace):
    store, _, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]
    result = confirm(workspace, proposal, request_id="same-confirmation")
    assert confirm(workspace, proposal, request_id="same-confirmation") == result
    with pytest.raises(RequestConflict):
        confirm(workspace, "different-proposal", request_id="same-confirmation")
    with pytest.raises(BookingError):
        confirm(workspace, proposal, request_id="another-confirmation")
    assert appointment_rows(store) == [(20, proposal["starts_at"])]
