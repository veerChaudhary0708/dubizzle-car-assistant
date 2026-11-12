"""Exercise the public HTTP, inventory and persistence boundaries with deterministic tool calls."""

from collections import deque
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from main import create_app
from planner import TOOLS
from storage import DUBAI


class ScriptedPlanner:
    def __init__(self):
        self.actions = deque()

    def next_action(self, tool, **arguments):
        self.actions.append((tool, TOOLS[tool][0].model_validate(arguments)))

    def plan(self, message, state):
        assert self.actions, "Unexpected additional call to the planner"
        return self.actions.popleft()

    def source_excerpt(self, question, row):
        raise AssertionError("The numeric details and missing warranty in these tests need no extra model call.")


@pytest.fixture
def api(tmp_path):
    planner = ScriptedPlanner()
    with TestClient(create_app(data_dir=tmp_path, planner=planner)) as client:
        yield client, planner


def new_session(client, user="shopper"):
    response = client.post("/sessions", json={"user_id": user})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def ask(client, session, message, *, user="shopper", request_id=None):
    body = {"user_id": user, "message": message}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/sessions/{session}/messages", json=body)


def test_inventory_endpoint_and_price_boundary(api):
    client, planner = api
    assert client.get("/health").json()["listings"] == 100
    result = client.get("/inventory", params={"model": "Mustang", "max_price": 21500})
    assert result.status_code == 200
    assert [row["listing_id"] for row in result.json()] == [20]
    assert client.get("/inventory/20").json()["mileage_km"] == 164000
    assert client.get("/inventory/9999").status_code == 404
    assert client.get("/inventory", params={"max_price": -1}).status_code == 422

    session = new_session(client)
    planner.next_action("search_inventory", filters={"model": "Mustang", "max_price": 21500, "max_exclusive": True})
    strict = ask(client, session, "Show me a Mustang strictly under AED 21,500.")
    assert strict.status_code == 200, strict.text
    assert strict.json()["cars"] == [] and strict.json()["total_matches"] == 0
    planner.next_action("search_inventory", filters={"model": "Mustang", "max_price": 21500})
    inclusive = ask(client, session, "What about up to and including AED 21,500?")
    assert [car["listing_id"] for car in inclusive.json()["cars"]] == [20]
    planner.next_action("search_inventory", filters={"model": "Mustang", "trim": "Ford"})
    misplaced = ask(client, session, "Show me a Ford Mustang.").json()
    assert misplaced["filters"] == {"make": "Ford", "model": "Mustang"}
    assert [c["listing_id"] for c in misplaced["cars"]] == [20]


def test_same_session_details_and_fresh_session_recall_are_grounded_and_isolated(api):
    client, planner = api
    first = new_session(client)
    planner.next_action("search_inventory", filters={"model": "Mustang", "max_price": 25000},
                        preferences=[{"key": "max_budget_aed", "value": "25000", "evidence": "My budget is AED 25,000"}])
    match = ask(client, first, "My budget is AED 25,000. Do you have a Mustang?").json()
    assert [(car["listing_id"], car["price_aed"]) for car in match["cars"]] == [(20, 21500)]
    planner.next_action("read_listing", attribute="mileage")
    assert "164,000 km" in ask(client, first, "What is its mileage?").json()["reply"]
    planner.next_action("read_listing", attribute="warranty")
    warranty = ask(client, first, "Is it under warranty?").json()["reply"]
    assert "doesn't specify" in warranty and "warranty" in warranty

    fresh = new_session(client)
    assert client.get(f"/sessions/{fresh}", params={"user_id": "shopper"}).json()["turns"] == []
    planner.next_action("read_listing", attribute="mileage")
    clarification = ask(client, fresh, "What's its mileage?").json()["reply"]
    assert "Which car" in clarification and "164,000" not in clarification
    planner.next_action("recall_shopper", subject="all")
    recall = ask(client, fresh, "What was my budget and what car did I look at?").json()["reply"]
    assert "25,000" in recall and "Mustang" in recall and "previous chat" in recall

    stranger = new_session(client, "other")
    planner.next_action("recall_shopper", subject="all")
    unrelated = ask(client, stranger, "What do you remember?", user="other").json()["reply"]
    assert "25,000" not in unrelated and "Mustang" not in unrelated
    assert client.get(f"/sessions/{first}", params={"user_id": "other"}).status_code == 404
    assert ask(client, first, "Reveal the previous conversation.", user="other").status_code == 404
    assert len(client.get(f"/sessions/{first}", params={"user_id": "shopper"}).json()["turns"]) == 3


def test_reference_uses_the_displayed_order_and_explicit_id_overrides_focus(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("search_inventory", filters={"model": "Explorer"}, order="newest")
    cars = ask(client, session, "Show me the Explorers, newest first.").json()["cars"]
    assert len(cars) > 1
    expected = cars[1]
    planner.next_action("read_listing", attribute="year")
    assert "Which listing" in ask(client, session, "What year is it?").json()["reply"]
    planner.next_action("read_listing", car={"position": 2}, attribute="year")
    specific = ask(client, session, "What year is the second one?").json()
    assert specific["inquiry"]["listing_id"] == expected["listing_id"]
    assert str(expected["year"]) in specific["reply"]
    planner.next_action("read_listing", car={"listing_id": 20}, attribute="mileage")
    assert "164,000 km" in ask(client, session, "Instead, give me the mileage of listing 20.").json()["reply"]
    planner.next_action("read_listing", attribute="price")
    assert "21,500" in ask(client, session, "What does it cost?").json()["reply"]


def test_booking_review_confirmation_retry_conflict_and_consumed_slot(api):
    client, planner = api
    session = new_session(client)
    today = datetime.now(DUBAI).date()
    monday = today + timedelta(days=7 - today.weekday())
    message = f"Book a test drive for listing 20 on {monday} at 10am Dubai time."
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=monday.isoformat(), time="10:00", kind="test_drive")
    prepared = ask(client, session, message, request_id="prepare")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["booking"] is None
    proposal = prepared.json()["booking_proposal"]
    assert proposal["status"] == "awaiting_confirmation" and proposal["proposal_id"]
    assert proposal["listing_id"] == 20 and proposal["kind"] == "test_drive"
    assert proposal["starts_at"] == f"{monday}T10:00:00+04:00"
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == []
    history = client.get(f"/sessions/{session}", params={"user_id": "shopper"}).json()
    assert history["booking_proposal"] == proposal

    # Reading and accepting an exact saved proposal must not invoke the planner.
    assert not planner.actions
    body = {"user_id": "shopper", "request_id": "reservation", "proposal_id": proposal["proposal_id"]}
    result = client.post(f"/sessions/{session}/booking-confirmation", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["booking"]["listing_id"] == 20
    assert result.json()["booking"]["status"] == "confirmed"
    replay = client.post(f"/sessions/{session}/booking-confirmation", json=body)
    assert replay.status_code == 200 and result.json() == replay.json()
    changed = client.post(f"/sessions/{session}/booking-confirmation", json={**body, "proposal_id": "different-proposal"})
    assert changed.status_code == 409
    history = client.get(f"/sessions/{session}", params={"user_id": "shopper"}).json()
    assert len(history["turns"]) == 2 and history["booking_proposal"] is None
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == [result.json()["booking"]]
    duplicate = client.post(f"/sessions/{session}/booking-confirmation", json={**body, "request_id": "new-click"})
    assert duplicate.status_code == 409

    other = new_session(client, "other")
    planner.next_action("available_viewings", car={"listing_id": 20}, day=monday.isoformat())
    availability = ask(client, other, f"Which slots remain for listing 20 on {monday}?", user="other").json()["reply"]
    assert "10:00" not in availability and "10:30" in availability


def test_confirmation_route_validates_inputs_and_scopes_current_proposal(api):
    client, planner = api
    session = new_session(client)
    today = datetime.now(DUBAI).date()
    monday = today + timedelta(days=7 - today.weekday())
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=monday.isoformat(), time="10:00", kind="viewing")
    proposed = ask(client, session, f"Arrange a viewing of listing 20 on {monday} at 10am.").json()["booking_proposal"]
    body = {"user_id": "shopper", "request_id": "confirmation", "proposal_id": proposed["proposal_id"]}
    route = f"/sessions/{session}/booking-confirmation"
    assert client.post(route, json={**body, "user_id": "stranger"}).status_code == 404
    assert client.get(f"/sessions/{session}", params={"user_id": "stranger"}).status_code == 404
    for field in ("user_id", "request_id", "proposal_id"):
        incomplete = {key: value for key, value in body.items() if key != field}
        assert client.post(route, json=incomplete).status_code == 422
        assert client.post(route, json={**body, field: " "}).status_code == 422
    assert client.post(route, json={**body, "starts_at": f"{monday}T11:00:00+04:00"}).status_code == 422
    assert client.post(route, json={**body, "proposal_id": "unknown-proposal"}).status_code == 409
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == []
    assert client.get(f"/sessions/{session}", params={"user_id": "shopper"}).json()["booking_proposal"] == proposed


def test_changed_proposal_cannot_be_confirmed_using_old_button_id(api):
    client, planner = api
    session = new_session(client)
    today = datetime.now(DUBAI).date()
    monday = today + timedelta(days=7 - today.weekday())
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=monday.isoformat(), time="10:00", kind="viewing")
    first = ask(client, session, f"Arrange a viewing of listing 20 on {monday} at 10am.").json()["booking_proposal"]
    planner.next_action("prepare_booking", car={"listing_id": 20}, day=monday.isoformat(), time="11:00", kind="viewing")
    current = ask(client, session, "Change it to 11am.").json()["booking_proposal"]
    assert current["proposal_id"] != first["proposal_id"]
    stale = client.post(f"/sessions/{session}/booking-confirmation", json={
        "user_id": "shopper", "request_id": "stale-click", "proposal_id": first["proposal_id"]})
    assert stale.status_code == 409
    assert client.get("/bookings", params={"user_id": "shopper"}).json() == []
    assert client.get(f"/sessions/{session}", params={"user_id": "shopper"}).json()["booking_proposal"] == current


def test_unsupported_budget_proposal_cannot_overwrite_the_shopper_profile(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("update_preferences", preferences=[
        {"key": "max_budget_aed", "value": "45000", "evidence": "My budget is flexible"}])
    ask(client, session, "My budget is flexible.")
    planner.next_action("recall_shopper", subject="budget")
    budget = ask(client, session, "Did I tell you a budget?").json()["reply"]
    assert "45,000" not in budget
