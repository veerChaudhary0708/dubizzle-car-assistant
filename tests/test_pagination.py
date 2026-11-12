"""Regression coverage for server-owned, session-specific inventory pagination."""

import pytest
from fastapi.testclient import TestClient

from main import create_app
from storage import Store
from test_api import ScriptedPlanner, api, ask, new_session


def ids(result):
    return [car["listing_id"] for car in result["cars"]]


def next_page(client, planner, session, *, user="shopper", request_id=None):
    planner.next_action("next_inventory_page")
    result = ask(client, session, "Show me more cars.", user=user, request_id=request_id)
    assert result.status_code == 200, result.text
    return result.json()


def test_browse_every_listing_once_and_stop_without_losing_final_focus(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("search_inventory", filters={})
    first = ask(client, session, "What cars are available?").json()
    assert ids(first) == [1, 2, 3, 4, 5]
    assert first["pagination"] == {"start": 1, "end": 5, "total": 100, "has_more": True}
    seen = ids(first)

    for start in range(6, 101, 5):
        page = next_page(client, planner, session)
        assert ids(page) == list(range(start, start + 5))
        assert page["pagination"] == {
            "start": start, "end": start + 4, "total": 100, "has_more": start < 96,
        }
        assert "What price range" not in page["reply"]
        assert "1. " in page["reply"]  # Ordinals remain local to the visible page.
        seen.extend(ids(page))
    assert seen == list(range(1, 101))

    planner.next_action("read_listing", attribute="year", car={"position": 3})
    detail = ask(client, session, "What year is the third car?").json()
    assert detail["inquiry"]["listing_id"] == 98
    store = client.app.state.store
    before = store.state("shopper", session)
    for _ in range(2):
        exhausted = next_page(client, planner, session)
        assert exhausted["cars"] == []
        assert "no more" in exhausted["reply"].lower()
        assert exhausted["pagination"] == {"start": 0, "end": 0, "total": 100, "has_more": False}
        after = store.state("shopper", session)
        assert after["shown_ids"] == before["shown_ids"] == [96, 97, 98, 99, 100]
        assert after["selected_id"] == before["selected_id"] == 98


def test_replayed_next_request_does_not_skip_a_page(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("search_inventory", filters={})
    ask(client, session, "Show all cars.")
    second = next_page(client, planner, session, request_id="page-two")
    assert ids(second) == [6, 7, 8, 9, 10]

    # No planner action is queued: an HTTP retry must replay the committed turn.
    replay = ask(client, session, "Show me more cars.", request_id="page-two")
    assert replay.status_code == 200 and replay.json() == second
    assert ask(client, session, "A different message", request_id="page-two").status_code == 409
    assert ids(next_page(client, planner, session)) == [11, 12, 13, 14, 15]
    assert len(client.app.state.store.history("shopper", session)) == 3


def test_ordinals_follow_the_latest_page_and_detail_does_not_reset_search(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("search_inventory", filters={})
    ask(client, session, "What cars are available?")
    planner.next_action("read_listing", attribute="year", car={"position": 2})
    assert ask(client, session, "What year is the second one?").json()["inquiry"]["listing_id"] == 2

    assert ids(next_page(client, planner, session)) == [6, 7, 8, 9, 10]
    state = client.app.state.store.state("shopper", session)
    assert state["selected_id"] is None
    planner.next_action("read_listing", attribute="year", car={"position": 1})
    assert ask(client, session, "What year is the first one?").json()["inquiry"]["listing_id"] == 6
    assert ids(next_page(client, planner, session)) == [11, 12, 13, 14, 15]


@pytest.mark.parametrize("filters,order,bookable", [
    ({"make": "Mercedes-Benz"}, "newest", False),
    ({"max_price": 150000}, "lowest_price", False),
    ({}, "lowest_price", False),
    ({"min_year": 2010}, "newest", True),
])
def test_continuation_preserves_filters_sort_and_booking_eligibility(api, filters, order, bookable):
    client, planner = api
    expected = client.get("/inventory", params={**filters, "limit": 100}).json()
    # Every actual listing is bookable in this demo; the filter must not silently
    # restore the former three-listing allowlist on later pages.
    assert all(car["booking_eligible"] for car in expected)
    if order == "newest":
        expected.sort(key=lambda car: (-car["year"], car["listing_id"]))
    else:
        # Ordering does not exclude cars whose asking price is unknown.
        expected.sort(key=lambda car: (car["price_aed"] is None,
                                       car["price_aed"] if car["price_aed"] is not None else 0,
                                       car["listing_id"]))
    expected_ids = [car["listing_id"] for car in expected]
    assert len(expected_ids) > 5

    session = new_session(client)
    planner.next_action("search_inventory", filters=filters, order=order, bookable_only=bookable)
    page = ask(client, session, "Show cars matching these requirements.").json()
    seen = []
    while True:
        seen.extend(ids(page))
        assert page["filters"] == filters
        active = client.app.state.store.state("shopper", session)["active_search"]
        assert active["filters"] == filters
        assert active["order"] == order
        assert active["bookable_only"] is bookable
        assert active["result_ids"] == expected_ids
        if not page["pagination"]["has_more"]:
            break
        page = next_page(client, planner, session)
    assert seen == expected_ids
    assert page["pagination"]["end"] == len(expected_ids)


def test_new_search_resets_pagination_including_an_empty_search(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("search_inventory", filters={})
    ask(client, session, "Show all cars.")
    next_page(client, planner, session)

    expected = client.get("/inventory", params={"make": "Mercedes-Benz", "limit": 100}).json()
    planner.next_action("search_inventory", filters={"make": "Mercedes-Benz"})
    reset = ask(client, session, "Only Mercedes-Benz now.").json()
    assert ids(reset) == [car["listing_id"] for car in expected[:5]]
    assert reset["pagination"]["start"] == 1
    assert ids(next_page(client, planner, session)) == [car["listing_id"] for car in expected[5:10]]

    planner.next_action("search_inventory", filters={"model": "Mustang", "max_price": 1})
    empty = ask(client, session, "Show me a Mustang under one dirham.").json()
    assert empty["cars"] == []
    assert empty["pagination"] == {"start": 0, "end": 0, "total": 0, "has_more": False}
    assert next_page(client, planner, session)["cars"] == []
    assert client.app.state.store.state("shopper", session)["active_search"]["result_ids"] == []


def test_fresh_chats_and_other_shoppers_do_not_inherit_a_cursor(api):
    client, planner = api
    original = new_session(client)
    planner.next_action("search_inventory", filters={})
    ask(client, original, "Show all cars.")
    next_page(client, planner, original)
    for user in ("shopper", "someone-else"):
        fresh = new_session(client, user)
        result = next_page(client, planner, fresh, user=user)
        assert result["cars"] == []
        assert "search" in result["reply"].lower()
        assert not client.app.state.store.state(user, fresh)["active_search"]
    assert ask(client, original, "Show me more cars.", user="someone-else").status_code == 404
    assert ids(next_page(client, planner, original)) == [11, 12, 13, 14, 15]


def test_cursor_survives_history_window_and_application_restart(tmp_path):
    planner = ScriptedPlanner()
    with TestClient(create_app(data_dir=tmp_path, planner=planner)) as client:
        session = new_session(client)
        planner.next_action("search_inventory", filters={})
        ask(client, session, "Show all cars.")
        for _ in range(30):
            planner.next_action("conversation", category="thanks")
            ask(client, session, "Thanks.")
        state = Store(tmp_path).state("shopper", session)
        assert len(state["recent_turns"]) == 24
        assert all(turn["message"] == "Thanks." for turn in state["recent_turns"])
        assert state["active_search"]["next_offset"] == 5
        assert state["active_search"]["result_ids"] == list(range(1, 101))

    with TestClient(create_app(data_dir=tmp_path, planner=ScriptedPlanner())) as restarted:
        planner = restarted.app.state.assistant.planner
        assert ids(next_page(restarted, planner, session)) == [6, 7, 8, 9, 10]


def test_legacy_search_without_snapshot_continues_after_displayed_cars(api):
    client, planner = api
    session = new_session(client)
    expected = client.get("/inventory", params={"make": "Mercedes-Benz", "limit": 100}).json()
    expected_ids = [car["listing_id"] for car in expected]
    assert len(expected_ids) > 10
    legacy = {"kind": "search", "filters": {"make": "Mercedes-Benz"}, "shown_ids": expected_ids[:5]}
    client.app.state.store.commit_turn(
        "shopper", session, "old-search", 0, "Show me Mercedes-Benz cars.",
        {"reply": "Here are the first five cars.", "cars": expected[:5], "filters": legacy["filters"]},
        shown_ids=expected_ids[:5], inquiry=legacy,
    )
    assert ids(next_page(client, planner, session)) == expected_ids[5:10]
    active = client.app.state.store.state("shopper", session)["active_search"]
    assert active["filters"] == legacy["filters"]
    assert active["next_offset"] == 10
    assert ids(next_page(client, planner, session)) == expected_ids[10:15]
