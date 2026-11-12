"""Drafts are atomic with chat turns; only explicit confirmation consumes a slot."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from assistant_service import Assistant
from planner import ModelUnavailable
from storage import BookingError
from test_agent_workspace import NativePlanner, workspace, send, state
from test_storage import next_monday
from booking_helpers import appointment_rows, prepare, confirm


def test_proposal_does_not_consume_availability_until_confirmed(workspace):
    store, _, _, _ = workspace
    day = next_monday().isoformat()
    proposal = prepare(workspace)["booking_proposal"]
    assert proposal["starts_at"] in store.available_slots(20, day)
    assert appointment_rows(store) == []
    confirmed = confirm(workspace, proposal)
    assert proposal["starts_at"] not in store.available_slots(20, day)
    assert store.list_bookings("shopper") == [confirmed["booking"]]


def test_exception_after_preparation_rolls_back_draft_and_other_changes(workspace):
    store, planner, _, sid = workspace
    before = state(workspace)

    def fail(message, working, call):
        call("prepare_booking", car={"listing_id": 20}, day=next_monday().isoformat(),
             time="10:00", kind="test_drive")
        assert working["pending_booking"]
        assert store.state("shopper", sid)["pending_booking"] is None
        raise ModelUnavailable("Simulated interruption.")

    planner.callbacks.append(fail)
    with pytest.raises(ModelUnavailable):
        send(workspace, f"Prepare listing 20 on {next_monday()} at 10AM.")
    assert state(workspace) == before and appointment_rows(store) == []
    assert store.history("shopper", sid) == []


def test_cancelling_prepared_draft_in_same_turn_leaves_no_proposal(workspace):
    store, planner, _, _ = workspace
    planner.queue(("prepare_booking", {"car": {"listing_id": 20}, "day": str(next_monday()), "time": "10:00", "kind": "test_drive"}),
                  ("cancel_booking", {}))
    result = send(workspace, f"Prepare listing 20 on {next_monday()} at 10AM; actually cancel that draft.")
    assert result["booking"] is None and not result.get("booking_proposal")
    assert state(workspace)["pending_booking"] is None and appointment_rows(store) == []


def test_conflict_at_confirmation_rolls_back_without_claiming_success(workspace):
    store, planner, _, sid = workspace
    proposal = prepare(workspace)["booking_proposal"]
    other = store.create_session("competitor")["session_id"]
    store.commit_turn("competitor", other, "occupied", 0, "Reserve", {"reply": "Reserved"},
                      booking={key: proposal[key] for key in ("listing_id", "starts_at", "kind")})
    before = state(workspace)
    with pytest.raises(BookingError):
        confirm(workspace, proposal, request_id="lost-race")
    assert state(workspace) == before
    assert store.replay("shopper", sid, "lost-race") is None
    assert store.list_bookings("shopper") == []
    planner.queue(("prepare_booking", {"time": "11:00", "kind": "test_drive"}))
    edited = send(workspace, "Change it to 11AM.")["booking_proposal"]
    result = confirm(workspace, edited)
    assert result["booking"]["starts_at"].endswith("T11:00:00+04:00")
    assert len(appointment_rows(store)) == 2


def test_simultaneous_identical_confirmation_retries_create_one_receipt(workspace):
    store, _, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(workspace, proposal, request_id="same"), range(2)))
    assert results[0] == results[1]
    assert store.list_bookings("shopper") == [results[0]["booking"]]
    assert len(appointment_rows(store)) == 1


def test_competing_shoppers_cannot_confirm_same_car_and_slot(workspace):
    store, _, _, _ = workspace
    first = prepare(workspace)["booking_proposal"]
    second_planner = NativePlanner()
    second_sid = store.create_session("other")["session_id"]
    second_assistant = Assistant(store, second_planner)
    second_planner.queue(("prepare_booking", {"car": {"listing_id": 20}, "day": str(next_monday()),
                                             "time": "10:00", "kind": "test_drive"}))
    second = second_assistant.respond("other", second_sid, uuid4().hex,
        f"Prepare listing 20 on {next_monday()} at 10AM.")["booking_proposal"]

    def reserve(item):
        assistant, user, sid, proposal = item
        try:
            return assistant.confirm_booking(user, sid, uuid4().hex, proposal["proposal_id"])
        except BookingError:
            return None

    items = [(workspace[2], "shopper", workspace[3], first), (second_assistant, "other", second_sid, second)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, items))
    assert sum(result is not None for result in results) == 1
    assert len(appointment_rows(store)) == 1
