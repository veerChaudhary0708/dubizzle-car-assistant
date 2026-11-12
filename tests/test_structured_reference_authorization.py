"""IDs supplied by pending state or memory remain usable without text repetition."""

from test_agent_workspace import send, state, workspace
from test_storage import next_monday
from booking_helpers import confirm


def test_explicit_pending_id_survives_a_different_car_fact_detour(workspace):
    _, planner, _, _ = workspace
    day = next_monday().isoformat()
    planner.queue(("prepare_booking", {"car": {"listing_id": 1}, "day": day, "kind": "test_drive"}))
    send(workspace, f"Prepare a test drive of listing 1 on {day}.")
    assert state(workspace)["pending_booking"]["listing_id"] == 1
    planner.queue(("read_listing", {"car": {"listing_id": 20}, "attribute": "mileage", "focus": True}))
    send(workspace, "Before I choose a time, what's the mileage of Mustang listing 20?")
    assert state(workspace)["selected_id"] == 20
    assert state(workspace)["pending_booking"]["listing_id"] == 1

    def finish_pending(message, working, call):
        observation = call("prepare_booking", car={"listing_id": 1}, time="11:00", kind="test_drive")
        assert observation["booking_proposal"]["listing_id"] == 1
        assert observation["booking_proposal"]["kind"] == "test_drive"
        return "Please confirm the Explorer test drive proposed for 11am."

    planner.callbacks.append(finish_pending)
    result = send(workspace, "11 am")
    assert result["booking"] is None
    assert result["booking_proposal"]["listing_id"] == 1
    assert result["booking_proposal"]["starts_at"].startswith(day + "T11:00")
    assert confirm(workspace, result["booking_proposal"])["booking"]["listing_id"] == 1
    assert state(workspace)["pending_booking"] is None


def test_a_listing_returned_by_memory_can_be_read_in_the_same_new_turn(workspace):
    store, planner, assistant, _ = workspace
    planner.queue(("like_listing", {"car": {"listing_id": 20}}))
    send(workspace, "I like listing 20. Save it for me.")
    fresh = store.create_session("shopper")["session_id"]
    new_workspace = store, planner, assistant, fresh

    def recall_then_read(message, working, call):
        assert working["selected_id"] is None and working["shown_ids"] == []
        memory = call("recall_shopper", subject="liked")
        listing_id = memory["memory"]["liked_listing_ids"][0]
        detail = call("read_listing", car={"listing_id": listing_id}, attribute="mileage", focus=True)
        assert detail["status"] == "ok"
        assert detail["facts"]["cars"][0]["mileage_km"] == 164000
        return "You liked the Mustang; its listed mileage is164,000km."

    planner.callbacks.append(recall_then_read)
    send(new_workspace, "What was the mileage of the car I liked last time?")
    assert state(new_workspace)["selected_id"] == 20
