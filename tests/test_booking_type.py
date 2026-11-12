"""Appointment type is explicit; missing model fields cannot silently select viewing."""

from google.genai import types
from pydantic import ValidationError
import pytest

from planner import Book
from test_agent_loop import FakeSDK, response, tool, state as agent_state
from test_agent_workspace import workspace, send, state
from test_storage import next_monday
from booking_helpers import appointment_rows, confirm


def test_prepare_arguments_require_type_field_but_allow_explicit_unknown():
    with pytest.raises(ValidationError) as error:
        Book.model_validate({"car": {"listing_id": 20}, "time": "10:00"})
    assert any(item["loc"] == ("kind",) and item["type"] == "missing" for item in error.value.errors())
    assert Book.model_validate({"kind": None}).kind is None


def test_native_tool_retries_omitted_type_instead_of_executing_default_viewing(monkeypatch, agent_state):
    fields = {"car": {"listing_id": 20}, "day": "2030-11-11", "time": "10:00"}
    sdk = FakeSDK(monkeypatch, [
        response(tool("prepare_booking", fields, "missing-kind")),
        response(tool("prepare_booking", {**fields, "kind": "test_drive"}, "corrected-kind")),
        response(types.Part(text="Please confirm the proposed test drive.")),
    ])
    executed = []

    def execute(name, action):
        executed.append((name, action.kind))
        return {"status": "awaiting_confirmation", "booking_proposal": {"kind": action.kind}}

    sdk.planner.run_turn("Book a test drive for listing 20 on 2030-11-11 at 10AM.", agent_state, execute)
    rejected = sdk.requests[1][0][-1].parts[0].function_response.response
    assert rejected["status"] == "invalid_arguments"
    assert {"field": "kind", "type": "missing"} in rejected["issues"]
    assert executed == [("prepare_booking", "test_drive")]


def test_unknown_type_cannot_be_proposed_or_reserved_until_user_supplies_it(workspace):
    store, planner, _, _ = workspace
    day = next_monday().isoformat()
    planner.queue(("prepare_booking", {"car": {"listing_id": 20}, "day": day,
                                       "time": "10:00", "kind": None}))
    result = send(workspace, f"Arrange an appointment for listing 20 on {day} at 10AM.")
    assert result["booking"] is None and result["booking_proposal"] is None
    draft = state(workspace)["pending_booking"]
    assert draft.get("kind") is None and draft["status"] == "collecting"
    assert draft["listing_id"] == 20 and draft["day"] == day and draft["time"] == "10:00"
    assert appointment_rows(store) == []

    planner.queue(("prepare_booking", {"kind": "test_drive"}))
    proposed = send(workspace, "A test drive, please.")["booking_proposal"]
    assert proposed["kind"] == "test_drive"
    assert proposed["starts_at"] == day + "T10:00:00+04:00"
    assert appointment_rows(store) == []
    assert confirm(workspace, proposed)["booking"]["kind"] == "test_drive"


def test_explicit_null_on_continuation_preserves_known_test_drive_type(workspace):
    store, planner, _, _ = workspace
    planner.queue(("prepare_booking", {"car": {"listing_id": 20}, "kind": "test_drive"}))
    send(workspace, "I'd like to test drive listing 20.")
    day = next_monday().isoformat()
    planner.queue(("prepare_booking", {"day": day, "time": "10:00", "kind": None}))
    proposal = send(workspace, f"On {day} at 10AM.")["booking_proposal"]
    assert proposal["kind"] == "test_drive"
    assert appointment_rows(store) == []
