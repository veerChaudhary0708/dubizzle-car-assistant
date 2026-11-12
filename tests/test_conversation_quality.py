"""Exercise conversational state transitions independently of live model wording.

The live, real-model companion is work/quality_acceptance.py. These tests verify
which listing facts reach the generator and how multi-turn actions affect storage.
"""
from datetime import datetime, timedelta
import sqlite3

from fastapi.testclient import TestClient
import pytest

from main import create_app
from storage import DUBAI, Store
from test_api import ScriptedPlanner, ask, new_session


class QualityPlanner(ScriptedPlanner):
    def __init__(self):
        super().__init__()
        self.explanations = []

    def explain(self, message, rows, state):
        self.explanations.append({"message": message, "rows": rows, "state": state})
        return "V6 describes a six-cylinder engine arranged in two banks. A trim is a vehicle variant."

    def converse(self, message, state, category):
        return "I can help you understand this car and compare its verified listing details."


@pytest.fixture
def quality_api(tmp_path):
    planner = QualityPlanner()
    with TestClient(create_app(data_dir=tmp_path, planner=planner)) as client:
        yield client, planner, tmp_path


def search_mustang(client, planner, sid):
    planner.next_action("search_inventory", filters={"make": "Ford", "model": "Mustang", "max_price": 25000})
    result = ask(client, sid, "Find a Ford Mustang under AED 25,000.")
    assert result.status_code == 200, result.text
    assert [car["listing_id"] for car in result.json()["cars"]] == [20]


def test_overview_and_multiple_facts_answer_the_question(quality_api):
    client, planner, _ = quality_api
    sid = new_session(client)
    search_mustang(client, planner, sid)
    planner.next_action("read_listing", attribute="overview")
    overview = ask(client, sid, "Tell me more about this car.").json()["reply"]
    assert "Mustang" in overview and "21,500" in overview and "164,000" in overview
    assert "convertible" in overview.lower()
    planner.next_action("read_listing", attribute="price", attributes=["price", "mileage", "warranty"])
    facts = ask(client, sid, "What are its price and mileage, and does it include a warranty?").json()["reply"]
    assert "21,500" in facts and "164,000" in facts
    assert "doesn't specify" in facts and "warranty" in facts.lower()
    assert not planner.explanations  # These facts need no generative second call.


def test_explanations_receive_only_resolved_listing_and_keep_selection(quality_api):
    client, planner, _ = quality_api
    sid = new_session(client)
    planner.next_action("explain_vehicle")
    general = ask(client, sid, "What does V6 mean?")
    assert general.status_code == 200 and "six-cylinder" in general.json()["reply"]
    assert planner.explanations[-1]["rows"] == []
    search_mustang(client, planner, sid)
    planner.next_action("explain_vehicle")
    explained = ask(client, sid, "What does V6 trim mean for this car?")
    assert explained.status_code == 200
    rows = planner.explanations[-1]["rows"]
    assert len(rows) == 1 and rows[0]["listing_id"] == 20
    assert rows[0]["mileage_km"] == 164000 and rows[0]["price_aed"] == 21500
    planner.next_action("read_listing", attribute="mileage")
    assert "164,000" in ask(client, sid, "And what's its mileage?").json()["reply"]
    fresh = new_session(client)
    planner.next_action("explain_vehicle")
    ask(client, fresh, "What does V6 mean generally?")
    assert planner.explanations[-1]["rows"] == []


def test_comparison_resolves_references_without_inventing_missing_mileage(quality_api):
    client, planner, _ = quality_api
    sid = new_session(client)
    search_mustang(client, planner, sid)
    planner.next_action("compare_listings", cars=[{}, {"listing_id": 12}])
    result = ask(client, sid, "Compare this car with listing 12.")
    assert result.status_code == 200, result.text
    reply = result.json()["reply"]
    assert "21,500" in reply and "35,000" in reply
    assert "13,500" in reply and "can't fully compare mileage" in reply
    assert "Unknown" in reply
    assert not planner.explanations  # Comparisons use verified fields and arithmetic.


def future_monday():
    future = datetime.now(DUBAI).date() + timedelta(days=1)
    return future + timedelta(days=(-future.weekday()) % 7)


def test_booking_survives_date_time_split_restart_and_other_detail(quality_api):
    client, planner, directory = quality_api
    sid = new_session(client)
    planner.next_action("prepare_booking", car={"listing_id": 20}, kind="test_drive")
    missing = ask(client, sid, "Book a test drive of listing 20.").json()
    assert missing["booking"] is None
    pending = Store(directory).state("shopper", sid)["pending_booking"]
    assert pending["listing_id"] == 20 and pending["kind"] == "test_drive"
    day = future_monday().isoformat()
    planner.next_action("prepare_booking", day=day, kind="test_drive")
    incomplete = ask(client, sid, f"Monday {day}.").json()
    assert incomplete["booking"] is None and "time" in incomplete["reply"].lower()
    assert Store(directory).state("shopper", sid)["pending_booking"]["day"] == day
    fresh = new_session(client)
    assert Store(directory).state("shopper", fresh)["pending_booking"] is None
    planner.next_action("read_listing", attribute="mileage")
    assert "164,000" in ask(client, sid, "Remind me of its mileage first.").json()["reply"]
    planner.next_action("prepare_booking", time="10:00", kind="test_drive")
    completed = ask(client, sid, "10 am Dubai time, please.", request_id="booking-completion")
    assert completed.status_code == 200, completed.text
    proposal = completed.json()["booking_proposal"]
    assert completed.json()["booking"] is None
    booking = client.app.state.assistant.confirm_booking("shopper", sid, "confirmation", proposal["proposal_id"])["booking"]
    assert {key: booking[key] for key in ("listing_id", "kind", "starts_at")} == {
        "listing_id": 20, "kind": "test_drive", "starts_at": f"{day}T10:00:00+04:00"}
    assert booking["status"] == "confirmed" and booking["booking_id"].startswith("booking-")
    assert booking["session_id"] == sid
    assert Store(directory).state("shopper", sid)["pending_booking"] is None
    replay = ask(client, sid, "10 am Dubai time, please.", request_id="booking-completion")
    assert replay.json() == completed.json()
    with sqlite3.connect(Store(directory).db_path) as connection:
        assert connection.execute("SELECT count(*) FROM appointments").fetchone()[0] == 1


def test_cancel_pending_request_does_not_cancel_confirmed_booking(quality_api):
    client, planner, directory = quality_api
    sid = new_session(client)
    day = future_monday().isoformat()
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=day, time="10:00", kind="viewing")
    proposed = ask(client, sid, f"Book listing 20 on {day} at 10 am.").json()
    assert proposed["booking"] is None
    assert client.app.state.assistant.confirm_booking("shopper", sid, "first-confirmation", proposed["booking_proposal"]["proposal_id"])["booking"]
    planner.next_action("prepare_booking", car={"listing_id": 12}, kind="test_drive")
    assert ask(client, sid, "I'd also like a test drive of listing 12.").json()["booking"] is None
    planner.next_action("cancel_booking")
    cancelled = ask(client, sid, "Never mind, cancel that pending request.").json()
    assert cancelled["booking"] is None
    assert Store(directory).state("shopper", sid)["pending_booking"] is None
    with sqlite3.connect(Store(directory).db_path) as connection:
        assert connection.execute("SELECT listing_id FROM appointments").fetchall() == [(20,)]
    planner.next_action("cancel_booking")
    again = ask(client, sid, "Cancel the booking.").json()["reply"].lower()
    assert "confirmed" in again and any(term in again for term in ("unchanged", "not cancel", "cannot", "can't"))


def test_invalid_booking_day_never_creates_appointment(quality_api):
    client, planner, directory = quality_api
    sid = new_session(client)
    sunday = future_monday() + timedelta(days=6)
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=sunday.isoformat(), time="10:00", kind="test_drive")
    result = ask(client, sid, f"Book listing 20 on Sunday {sunday} at 10 am.").json()
    assert result["booking"] is None and "Monday" in result["reply"]
    with sqlite3.connect(Store(directory).db_path) as connection:
        assert connection.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
