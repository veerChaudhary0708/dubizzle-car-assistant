"""Receipts and the bookings API expose committed, shopper-owned appointments only."""

from contextlib import closing
import json
import sqlite3

import pytest

from storage import BookingError, SessionNotFound, Store
from test_api import api, ask, new_session
from test_storage import inventory_store, next_monday


def reserve(store, user, sid, request_id, *, listing=20, hour="10:00"):
    proposed = {"listing_id": listing, "starts_at": f"{next_monday()}T{hour}", "kind": "test_drive"}
    response = {"reply": "Reserved.", "booking": proposed}
    return store.commit_turn(user, sid, request_id, store.state(user, sid)["version"],
                             "Please reserve this appointment.", response, booking=proposed)


def test_empty_store_without_inventory_has_no_bookings(tmp_path):
    store = Store(tmp_path)
    assert store.list_bookings("shopper") == []
    sid = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", sid, "hello", 0, "Hello", {"reply": "Hello"})
    assert store.history("shopper", sid) == [{"reply": "Hello", "message": "Hello"}]


def test_bookings_across_chats_are_scoped_and_sorted_by_appointment_time(tmp_path):
    store = inventory_store(tmp_path)
    first = store.create_session("a")["session_id"]
    second = store.create_session("a")["session_id"]
    foreign = store.create_session("b")["session_id"]
    later = reserve(store, "a", first, "late", hour="12:00")["booking"]
    earlier = reserve(store, "a", second, "early", listing=12, hour="09:00")["booking"]
    other = reserve(store, "b", foreign, "other", listing=53, hour="10:00")["booking"]
    assert store.list_bookings("a") == [earlier, later]
    assert store.list_bookings("b") == [other]
    assert store.state("a", first)["bookings"] == [earlier, later]
    assert store.create_session("a")["bookings"] == [earlier, later]
    assert store.state("b", foreign)["bookings"] == [other]
    assert store.list_bookings("unknown") == []
    assert {earlier["booking_id"], later["booking_id"], other["booking_id"]} == {"booking-1", "booking-2", "booking-3"}
    assert later["car_label"] == "2014 Ford Mustang V6"
    assert later["status"] == "confirmed" and later["session_id"] == first
    assert later["starts_at"].endswith("12:00:00+04:00")


def test_commit_replay_history_and_restart_share_one_receipt(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    result = reserve(store, "a", sid, "once")
    original_state = store.state("a", sid)
    repeated = store.commit_turn("a", sid, "once", 0, "Please reserve this appointment.", {"reply": "Duplicate"},
                                 booking={"listing_id": 12, "starts_at": f"{next_monday()}T11:00", "kind": "viewing"})
    assert repeated == result
    assert store.replay("a", sid, "once") == result
    assert store.history("a", sid)[0] == {**result, "message": "Please reserve this appointment."}
    assert store.list_bookings("a") == [result["booking"]]
    assert store.state("a", sid) == original_state
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert json.loads(connection.execute("SELECT response FROM turns").fetchone()[0]) == result
        assert connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1
    assert Store(tmp_path).list_bookings("a") == [result["booking"]]
    with pytest.raises(SessionNotFound):
        store.replay("b", sid, "once")


def test_failed_reservation_creates_no_receipt_or_partial_turn(tmp_path):
    store = inventory_store(tmp_path)
    first = store.create_session("a")["session_id"]
    second = store.create_session("b")["session_id"]
    reserve(store, "a", first, "occupied")
    with pytest.raises(BookingError):
        reserve(store, "b", second, "conflict")
    assert store.list_bookings("b") == []
    assert store.history("b", second) == []
    assert store.replay("b", second, "conflict") is None
    assert store.state("b", second)["version"] == 0


def test_legacy_receipt_is_derived_without_rewriting_historical_rows(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    actual = reserve(store, "a", sid, "old")["booking"]
    legacy = {"reply": "Reserved.", "booking": {key: actual[key] for key in ("listing_id", "starts_at", "kind")}}
    raw = json.dumps(legacy)
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.execute("UPDATE turns SET response = ?", (raw,))
        connection.commit()
    reopened = Store(tmp_path)
    assert reopened.list_bookings("a") == [actual]
    assert reopened.replay("a", sid, "old")["booking"] == actual
    assert reopened.history("a", sid)[0]["booking"] == actual
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute("SELECT response FROM turns").fetchone()[0] == raw
        assert connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1


def test_false_legacy_confirmation_cannot_become_a_receipt(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    store.commit_turn("a", sid, "false", 0, "Book it", {"reply": "I said it was booked."})
    old = {"reply": "I said it was booked.", "booking": {"listing_id": 20, "starts_at": f"{next_monday()}T10:00", "kind": "viewing"}}
    raw = json.dumps(old)
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.execute("UPDATE turns SET response = ?", (raw,))
        connection.commit()
    assert store.list_bookings("a") == []
    assert store.history("a", sid)[0]["booking"] is None
    assert store.replay("a", sid, "false")["booking"] is None
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute("SELECT response FROM turns").fetchone()[0] == raw
        assert connection.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0


def test_pending_details_and_proposed_response_never_appear_as_confirmed(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    proposed = {"listing_id": 20, "kind": "viewing", "starts_at": f"{next_monday()}T10:00"}
    result = store.commit_turn("a", sid, "proposed", 0, "Show availability", {"reply": "Available.", "booking": proposed},
                               pending_booking={"listing_id": 20, "intent": "availability", "day": str(next_monday())})
    assert result["booking"] is None
    assert store.list_bookings("a") == []
    assert store.replay("a", sid, "proposed") == result


def test_committed_receipt_survives_inventory_changes(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    result = reserve(store, "a", sid, "reservation")
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.execute("DELETE FROM listings WHERE listing_id = 20")
        connection.commit()
    assert store.list_bookings("a") == [result["booking"]]
    assert store.replay("a", sid, "reservation") == result


def test_bookings_http_endpoint_is_read_only_and_shopper_scoped(api):
    client, planner = api
    sid = new_session(client)
    other = new_session(client, "other")
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == []
    assert client.get("/bookings").status_code == 422
    assert client.get("/bookings", params={"user_id": ""}).status_code == 422
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=str(next_monday()), time="10:00", kind="test_drive")
    result = ask(client, sid, f"Book a test drive for listing 20 on {next_monday()} at 10 am.").json()
    assert result["booking"] is None
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == []
    result = client.app.state.assistant.confirm_booking("shopper", sid, "confirm", result["booking_proposal"]["proposal_id"])
    assert result["booking"]["booking_id"] and result["booking"]["status"] == "confirmed"
    listed = client.get("/bookings", params={"user_id": "shopper"})
    assert listed.status_code == 200 and listed.json() == [result["booking"]]
    assert client.get("/bookings", params={"user_id": "other"}).json() == []
    assert client.get("/bookings", params={"user_id": "nobody"}).json() == []
    assert client.get(f"/sessions/{other}", params={"user_id": "other"}).json()["turns"] == []
    assert client.post("/bookings", json={"user_id": "shopper"}).status_code == 405
