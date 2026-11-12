"""Model prose and preparation tools cannot produce a committed appointment."""

from planner import NATIVE_TOOLS
from test_agent_workspace import workspace, send, state
from test_storage import next_monday
from booking_helpers import appointment_rows, prepare, confirm


def test_native_model_has_preparation_but_no_reservation_tool():
    assert "prepare_booking" in NATIVE_TOOLS
    assert "book_viewing" not in NATIVE_TOOLS and "confirm_booking" not in NATIVE_TOOLS


def test_preparation_with_false_confirmation_prose_still_requires_user_confirmation(workspace):
    store, planner, _, _ = workspace
    planner.queue(("prepare_booking", {"car": {"listing_id": 20}, "day": str(next_monday()),
                                      "time": "10:00", "kind": "test_drive"}),
                  reply="Your appointment is booked and confirmed.")
    result = send(workspace, f"Prepare a Mustang test drive for listing 20 on {next_monday()} at 10AM.")
    assert result["booking"] is None
    assert result["booking_proposal"]["status"] == "awaiting_confirmation"
    assert appointment_rows(store) == []
    assert store.list_bookings("shopper") == []


def test_prose_without_a_tool_cannot_create_a_receipt(workspace):
    store, planner, _, _ = workspace
    planner.queue(reply="Your appointment is confirmed.")
    result = send(workspace, f"Arrange a Mustang test drive on {next_monday()} at 10AM.")
    assert result["booking"] is None and appointment_rows(store) == []
    assert store.list_bookings("shopper") == []


def test_availability_is_read_only_even_when_a_proposal_already_exists(workspace):
    store, planner, _, _ = workspace
    proposal = prepare(workspace)["booking_proposal"]
    before = state(workspace)["pending_booking"]
    planner.queue(("available_viewings", {"car": {"listing_id": 20}, "day": str(next_monday())}))
    result = send(workspace, "Which other times are available?")
    assert result["booking"] is None and appointment_rows(store) == []
    assert state(workspace)["pending_booking"] == before
    assert confirm(workspace, proposal)["booking"]["status"] == "confirmed"


def test_availability_without_a_draft_does_not_create_one(workspace):
    store, planner, _, _ = workspace
    planner.queue(("available_viewings", {"car": {"listing_id": 20}, "day": str(next_monday())}))
    result = send(workspace, f"Which slots are available for listing 20 on {next_monday()}?")
    assert result["booking"] is None and not result.get("booking_proposal")
    assert state(workspace)["pending_booking"] is None and appointment_rows(store) == []


def test_confirmed_receipts_remain_authoritative_across_a_new_chat(workspace):
    store, _, _, _ = workspace
    receipt = confirm(workspace, prepare(workspace)["booking_proposal"])["booking"]
    fresh = store.create_session("shopper")
    assert fresh["bookings"] == [receipt] and fresh["pending_booking"] is None
    assert store.create_session("different-shopper")["bookings"] == []
