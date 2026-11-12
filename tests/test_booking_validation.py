"""Regressions for booking input precision and mixed conversational actions."""

import sqlite3

import pytest

from storage import Store
from test_api import ask, new_session
from test_conversation_quality import future_monday, quality_api, search_mustang


def appointment_count(client):
    with sqlite3.connect(client.app.state.store.db_path) as connection:
        return connection.execute("SELECT count(*) FROM appointments").fetchone()[0]


@pytest.mark.parametrize("clock", ["10:00:59", "10:30:00.125"])
def test_nonzero_seconds_in_datetime_are_rejected_without_rounding(quality_api, clock):
    client, planner, directory = quality_api
    sid = new_session(client)
    day = future_monday().isoformat()
    planner.next_action("prepare_booking", car={"listing_id": 20}, starts_at=f"{day}T{clock}", kind="test_drive")
    result = ask(client, sid, f"Book a test drive of listing 20 on {day} at {clock} Dubai time.")
    assert result.status_code == 200, result.text
    assert result.json()["booking"] is None
    assert "half-hour" in result.json()["reply"]
    pending = Store(directory).state("shopper", sid)["pending_booking"]
    assert pending["day"] == day and pending["kind"] == "test_drive"
    assert not pending.get("time")
    assert appointment_count(client) == 0

    planner.next_action("prepare_booking", time="10:00", kind="test_drive")
    corrected = ask(client, sid, "Make that exactly 10 am.")
    assert corrected.status_code == 200, corrected.text
    proposal = corrected.json()["booking_proposal"]
    assert proposal["starts_at"] == f"{day}T10:00:00+04:00"
    assert appointment_count(client) == 0
    client.app.state.assistant.confirm_booking("shopper", sid, "confirm-corrected", proposal["proposal_id"])
    assert appointment_count(client) == 1


def test_date_only_datetime_requests_missing_time_without_booking_midnight(quality_api):
    client, planner, directory = quality_api
    sid = new_session(client)
    day = future_monday().isoformat()
    planner.next_action("prepare_booking", car={"listing_id": 20}, starts_at=day, kind="test_drive")
    result = ask(client, sid, f"Book a test drive of listing 20 on {day}.")
    assert result.status_code == 200, result.text
    assert result.json()["booking"] is None
    assert "Which time" in result.json()["reply"]
    pending = Store(directory).state("shopper", sid)["pending_booking"]
    assert pending["day"] == day and not pending.get("time")
    assert pending["kind"] == "test_drive" and pending["status"] == "collecting"
    assert appointment_count(client) == 0


def test_availability_inquiry_and_date_followup_remain_read_only(quality_api):
    client, planner, directory = quality_api
    sid = new_session(client)
    planner.next_action("available_viewings", car={"listing_id": 20})
    question = ask(client, sid, "Which viewing times are available for listing 20?")
    assert question.status_code == 200, question.text
    assert question.json()["booking"] is None
    assert "Which date" in question.json()["reply"]
    pending = Store(directory).state("shopper", sid)["pending_booking"]
    assert pending is None

    day = future_monday().isoformat()
    planner.next_action("available_viewings", car={"listing_id": 20}, day=day)
    slots = ask(client, sid, f"Monday {day}.")
    assert slots.status_code == 200, slots.text
    assert slots.json()["booking"] is None
    assert "08:00" in slots.json()["reply"] and "19:30" in slots.json()["reply"]
    pending = Store(directory).state("shopper", sid)["pending_booking"]
    assert pending is None
    assert appointment_count(client) == 0
    assert len(client.app.state.store.available_slots(20, day)) == 24


@pytest.mark.parametrize("tool", ["prepare_booking", "explain_vehicle"])
def test_explicit_budget_with_other_in_scope_action_is_saved_and_recall_never_writes(quality_api, tool):
    client, planner, directory = quality_api
    sid = new_session(client)
    args = {"preferences": [
        {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is AED 25,000"}]}
    if tool == "prepare_booking":
        args.update(car={"listing_id": 20}, day=future_monday().isoformat(), kind="test_drive")
        message = "My budget is AED 25,000; book a test drive of listing 20 next Monday."
    else:
        message = "My budget is AED 25,000; explain what V6 means."
    planner.next_action(tool, **args)
    result = ask(client, sid, message)
    assert result.status_code == 200, result.text
    state = Store(directory).state("shopper", sid)
    assert state["preferences"] == {"max_budget_aed": 25000}
    assert state["lead"]["budget_max_aed"] == 25000
    assert appointment_count(client) == 0

    # Even if the model proposes an update alongside a recall action, recall is read-only.
    planner.next_action("recall_shopper", subject="budget", preferences=[
        {"key": "max_budget_aed", "value": "50000", "evidence": "My budget is AED 50,000"}])
    recalled = ask(client, sid, 'Did I say "My budget is AED 50,000"? What budget did I give you?')
    assert recalled.status_code == 200, recalled.text
    assert "25,000" in recalled.json()["reply"]
    assert Store(directory).state("shopper", sid)["preferences"] == {"max_budget_aed": 25000}
    assert len(client.app.state.store.preference_history("shopper", "max_budget_aed")) == 1


def test_comparison_discards_unmentioned_id_and_uses_the_selected_car(quality_api):
    client, planner, _ = quality_api
    sid = new_session(client)
    search_mustang(client, planner, sid)
    # The model copied an unrelated ID, while the shopper only named listing 12.
    planner.next_action("compare_listings", cars=[{"listing_id": 53}, {"listing_id": 12}])
    result = ask(client, sid, "Compare this car with listing 12.")
    assert result.status_code == 200, result.text
    reply = result.json()["reply"]
    assert "listing 20" in reply and "listing 12" in reply
    assert "listing 53" not in reply and "Explorer" not in reply
    assert "21,500" in reply and "35,000" in reply
