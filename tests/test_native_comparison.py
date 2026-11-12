"""Comparison observations supply exact arithmetic without moving user focus."""

import pytest

from test_agent_workspace import display_snapshot, send, state, workspace


@pytest.mark.parametrize("listing_ids, signed_difference", [([20, 12], -13500), ([12, 20], 13500)])
def test_price_difference_has_direction_and_missing_mileage_stays_unknown(workspace, listing_ids, signed_difference):
    _, planner, _, _ = workspace

    def compare(message, working, call):
        observation = call("compare_listings", cars=[{"listing_id": value} for value in listing_ids])
        price, mileage = observation["comparisons"]
        assert price["field"] == "price_aed" and price["status"] == "known"
        assert price["difference_left_minus_right"] == signed_difference
        assert price["absolute_difference"] == 13500 and price["lower_listing_id"] == 20
        assert price["missing_listing_ids"] == []
        assert mileage["field"] == "mileage_km" and mileage["status"] == "unknown"
        assert mileage["difference_left_minus_right"] is None and mileage["absolute_difference"] is None
        assert mileage["missing_listing_ids"] == [12]
        assert {car["listing_id"] for car in observation["facts"]["cars"]} == {12, 20}
        assert working["selected_id"] is None
        return "The Mazda costs AED13,500 more; its mileage is not supplied."

    planner.callbacks.append(compare)
    send(workspace, "Compare listings 20 and 12: how much more does the Mazda cost, and which has lower mileage?")
    assert state(workspace)["selected_id"] is None


def test_five_car_comparison_cannot_subtract_unknown_prices_or_mileages(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Nissan"}}))
    send(workspace, "Show Nissan cars.")
    before = display_snapshot(state(workspace))

    def compare(message, working, call):
        observation = call("compare_listings", cars=[{"position": position} for position in range(1, 6)])
        assert len(observation["facts"]["cars"]) == 5
        assert len(observation["comparisons"]) == 20  # Two fields for each of ten pairs.
        for calculation in observation["comparisons"]:
            assert calculation["status"] == "unknown"
            assert calculation["absolute_difference"] is None
            assert calculation["difference_left_minus_right"] is None
            assert calculation["lower_listing_id"] is None
            assert len(calculation["missing_listing_ids"]) == 2
        assert display_snapshot(working) == before
        return "None of the five listings supplies a verified price or odometer reading."

    planner.callbacks.append(compare)
    result = send(workspace, "Compare all five on price and mileage.")
    assert result["cars"] == [] and display_snapshot(state(workspace)) == before


def test_internal_candidates_can_be_compared_without_repeating_their_ids(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"model": "Mustang"}}))
    send(workspace, "Show the Mustang.")
    before = display_snapshot(state(workspace))

    def compare(message, working, call):
        found = call("search_inventory", filters={"make": "Mazda"}, display=False)
        mazda = found["facts"]["cars"][0]["listing_id"]
        comparison = call("compare_listings", cars=[{"listing_id": 20}, {"listing_id": mazda}])
        assert comparison["comparisons"][0]["absolute_difference"] == 13500
        assert display_snapshot(working) == before
        return "The Mazda is AED13,500 more expensive than the Mustang."

    planner.callbacks.append(compare)
    send(workspace, "Look for a Mazda alternative and compare its price, keeping the Mustang selected and displayed.")
    assert display_snapshot(state(workspace)) == before


def test_ungrounded_comparison_id_requests_clarification_instead_of_substitution(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"model": "Mustang"}}))
    send(workspace, "Show the Mustang.")
    before = display_snapshot(state(workspace))

    def compare(message, working, call):
        observation = call("compare_listings", cars=[{"listing_id": 20}, {"listing_id": 12}])
        assert observation["status"] == "needs_clarification"
        assert not observation.get("comparisons") and not observation.get("facts")
        assert display_snapshot(working) == before
        return "Which other car would you like to compare?"

    planner.callbacks.append(compare)
    send(workspace, "Can you compare this with another car?")
    assert display_snapshot(state(workspace)) == before
