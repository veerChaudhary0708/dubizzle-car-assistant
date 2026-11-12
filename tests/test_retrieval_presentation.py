"""Internal retrieval can become a display without corrupting cursor or focus."""
from test_agent_workspace import workspace, send, state


def test_presenting_persisted_internal_results_promotes_them_to_active_search(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang"}}))
    send(workspace, "Show me the Mustang.")
    planner.queue(("search_inventory", {"filters": {"make": "Mercedes-Benz"}, "display": False}))
    send(workspace, "Check Mercedes alternatives without changing the displayed car.")
    expected = state(workspace)["retrieved_search"]["result_ids"]
    assert len(expected) > 5 and state(workspace)["shown_ids"] == [20]
    planner.queue(("show_results", {"mode": "latest"}))
    displayed = send(workspace, "Show those alternatives now.")
    assert [car["listing_id"] for car in displayed["cars"]] == expected[:5]
    assert state(workspace)["active_search"]["kind"] == "search"
    assert state(workspace)["active_search"]["result_ids"] == expected
    assert state(workspace)["last_results"]["result_ids"] == expected
    planner.queue(("show_results", {"mode": "next"}))
    next_page = send(workspace, "More of those, please.")
    assert [car["listing_id"] for car in next_page["cars"]] == expected[5:10]


def test_reading_an_internal_comparison_candidate_preserves_user_selected_car(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang"}}))
    send(workspace, "Show the Mustang.")
    assert state(workspace)["selected_id"] == 20

    def compare(message, working, call):
        call("search_inventory", filters={"make": "Nissan"}, display=False)
        detail = call("read_listing", car={"listing_id": 11}, attribute="overview")
        assert detail["facts"]["cars"][0]["listing_id"] == 11
        assert working["selected_id"] == 20
        return "The Nissan Altima's price is unknown; your Mustang remains selected."

    planner.callbacks.append(compare)
    send(workspace, "Compare this Mustang with Nissan alternatives without changing my selected car.")
    assert state(workspace)["shown_ids"] == [20]
    assert state(workspace)["selected_id"] == 20


def test_unverified_candidates_are_valid_references_without_becoming_verified_matches(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang"}}))
    send(workspace, "Show me the Mustang.")

    def check_unknown(message, working, call):
        found = call("search_inventory", filters={"make": "Nissan", "max_price": 25000}, display=False)
        assert found["facts"]["cars"] == []
        assert [car["listing_id"] for car in found["unverified_candidates"]["cars"]] == [11, 23, 41, 67, 78]
        detail = call("read_listing", car={"listing_id": 11}, attribute="price")
        assert detail["facts"]["cars"][0]["listing_id"] == 11
        assert detail["facts"]["cars"][0]["price_aed"] is None
        return "The Nissan Altima's price is not supplied, so I cannot verify it meets your limit."

    planner.callbacks.append(check_unknown)
    send(workspace, "Check Nissan alternatives under AED 25,000 and explain what data is missing.")

    # An unverified candidate remains a valid reference on a later turn, while
    # the strict search still has zero verified matches.
    assert state(workspace)["retrieved_search"]["result_ids"] == []
    assert 11 in state(workspace)["retrieved_search"]["unverified_ids"]
    planner.queue(("read_listing", {"car": {"listing_id": 11}, "attribute": "price"}))
    send(workspace, "What price was given for that Altima?")
    assert planner.observations[-1][1]["facts"]["cars"][0]["listing_id"] == 11


def test_ungrounded_id_never_silently_substitutes_another_car(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang"}}))
    send(workspace, "Show the Mustang.")
    planner.queue(("read_listing", {"car": {"listing_id": 11}, "attribute": "price"}))
    send(workspace, "What's the price?")
    observation = planner.observations[-1][1]
    assert observation["status"] == "needs_clarification"
    assert "facts" not in observation
    assert state(workspace)["selected_id"] == 20


def test_refine_shown_uses_displayed_search_after_internal_lookup(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford"}, "order": "newest"}))
    send(workspace, "Show Fords newest first.")
    planner.queue(("search_inventory", {"filters": {"make": "Nissan"}, "display": False}))
    send(workspace, "Check Nissan alternatives internally.")
    planner.queue(("search_inventory", {"mode": "refine", "refine_base": "shown", "filters": {"max_price": 25000}}))
    result = send(workspace, "Keep the displayed Ford search and limit it to AED 25,000.")
    assert result["filters"]["make"].lower() == "ford"
    assert result["filters"]["max_price"] == 25000
    assert state(workspace)["active_search"]["order"] == "newest"
