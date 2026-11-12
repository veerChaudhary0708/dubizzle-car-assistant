"""All inventory cars are demo-bookable; nonexistent listings are not."""
from contextlib import closing
from datetime import datetime, timedelta
import sqlite3

import pytest

from storage import BookingError, DUBAI
from test_api import api, ask, new_session


def next_monday():
    today = datetime.now(DUBAI).date()
    return today + timedelta(days=7 - today.weekday())


def test_every_inventory_card_is_eligible_for_a_demo_booking(api):
    client, _ = api
    cars = client.get("/inventory", params={"limit": 100}).json()
    assert len(cars) == 100
    assert {car["listing_id"] for car in cars} == set(range(1, 101))
    assert all(car["booking_eligible"] is True for car in cars)
    assert client.get("/inventory/1").json()["booking_eligible"] is True
    assert client.get("/inventory/99").json()["booking_eligible"] is True


def test_bookable_only_search_paginates_every_inventory_car(api):
    client, planner = api
    sid = new_session(client)
    planner.next_action("search_inventory", filters={}, bookable_only=True)
    page = ask(client, sid, "Show me cars I can book for a test drive.").json()
    seen = []
    while True:
        assert page["pagination"]["total"] == 100
        assert all(car["booking_eligible"] for car in page["cars"])
        seen.extend(car["listing_id"] for car in page["cars"])
        if not page["pagination"]["has_more"]:
            break
        planner.next_action("next_inventory_page")
        page = ask(client, sid, "Show me more bookable cars.").json()
    assert seen == list(range(1, 101))
    planner.next_action("next_inventory_page")
    exhausted = ask(client, sid, "Any more bookable cars?").json()
    assert exhausted["cars"] == [] and exhausted["pagination"]["has_more"] is False


@pytest.mark.parametrize("listing_id", [1, 99])
def test_previously_excluded_listing_can_be_booked(api, listing_id):
    client, planner = api
    sid = new_session(client)
    day = next_monday().isoformat()
    store = client.app.state.store
    assert len(store.available_slots(listing_id, day)) == 24
    planner.next_action("prepare_booking", car={"listing_id": listing_id}, day=day, time="10:00", kind="test_drive")
    response = ask(client, sid, f"Book a test drive of listing {listing_id} on {day} at 10 am.")
    assert response.status_code == 200, response.text
    assert response.json()["booking"] is None
    assert store.list_bookings("shopper") == []
    proposal = response.json()["booking_proposal"]
    confirmed = client.app.state.assistant.confirm_booking("shopper", sid, "confirm-car", proposal["proposal_id"])
    receipt = confirmed["booking"]
    assert {key: receipt[key] for key in ("listing_id", "starts_at", "kind")} == {
        "listing_id": listing_id, "starts_at": day + "T10:00:00+04:00", "kind": "test_drive",
    }
    assert receipt["booking_id"] and receipt["status"] == "confirmed"
    assert receipt["session_id"] == sid and receipt["car_label"]
    assert store.list_bookings("shopper") == [receipt]
    assert store.replay("shopper", sid, "confirm-car")["booking"] == receipt
    assert len(store.available_slots(listing_id, day)) == 23
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute("SELECT listing_id FROM appointments").fetchall() == [(listing_id,)]


def test_unknown_listing_has_no_availability_and_booking_rolls_back(api):
    client, _ = api
    store = client.app.state.store
    sid = new_session(client)
    before = store.state("shopper", sid)
    day = next_monday().isoformat()
    assert client.get("/inventory/999").status_code == 404
    with pytest.raises(BookingError):
        store.available_slots(999, day)
    with pytest.raises(BookingError):
        store.commit_turn("shopper", sid, "missing-listing", 0,
                          "Book nonexistent listing 999.", {"reply": "This must not persist"},
                          booking={"listing_id": 999, "starts_at": day + "T10:00", "kind": "viewing"},
                          preferences={"max_budget_aed": 25000}, lead={"budget_max_aed": 25000},
                          pending_booking={"listing_id": 999})
    assert store.state("shopper", sid) == before
    assert store.history("shopper", sid) == []
    assert store.replay("shopper", sid, "missing-listing") is None
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 0
