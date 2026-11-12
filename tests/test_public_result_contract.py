"""A user turn presents one final page while allowing internal research."""

from test_agent_workspace import send, state, workspace


def test_zero_white_refinement_cannot_be_replaced_by_old_cards(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"model": "Mustang"}}))
    send(workspace, "Show Mustangs.")

    def refine_then_wrong_fallback(message, working, call):
        requested = call("search_inventory", mode="refine", filters={"color": "white"})
        assert requested["total_matches"] == 0
        old = call("show_results", mode="current")
        assert old["status"] == "presentation_limit"
        assert working["active_search"]["filters"]["color"] == "white"
        return "No white Mustang can be verified from this inventory."

    planner.callbacks.append(refine_then_wrong_fallback)
    result = send(workspace, "Only white ones, please.")
    assert result["cars"] == [] and result["total_matches"] == 0
    assert result["filters"]["color"] == "white"
    assert state(workspace)["lead"]["criteria"]["color"] == "white"
    assert state(workspace)["active_search"]["filters"]["color"] == "white"


def test_zero_budget_result_cannot_be_replaced_with_an_unrequested_display(workspace):
    _, planner, _, _ = workspace

    def requested_then_broader(message, working, call):
        requested = call("search_inventory", filters={"model": "Mustang", "max_price": 20000})
        assert requested["total_matches"] == 0
        broader = call("search_inventory", filters={"model": "Mustang", "max_price": 25000})
        assert broader["status"] == "presentation_limit"
        internal = call("search_inventory", filters={"model": "Mustang"}, display=False)
        assert internal["facts"]["cars"][0]["price_aed"] == 21500
        assert working["active_search"]["filters"]["max_price"] == 20000
        return "There is no verified Mustang below AED20,000. One outside your limit is listed at AED21,500."

    planner.callbacks.append(requested_then_broader)
    result = send(workspace, "Find a Mustang under AED20,000.")
    assert result["cars"] == [] and result["total_matches"] == 0
    assert result["filters"]["max_price"] == 20000
    assert state(workspace)["lead"]["budget_max_aed"] == 20000


def test_internal_research_does_not_consume_the_one_public_result(workspace):
    _, planner, _, _ = workspace

    def inspect_then_display(message, working, call):
        call("search_inventory", filters={"make": "Ford"}, display=False)
        shown = call("search_inventory", filters={"make": "Nissan"})
        assert shown["status"] == "found"
        assert [car["listing_id"] for car in shown["cars"]] == [11, 23, 41, 67, 78]
        return "Here are the Nissan cars."

    planner.callbacks.append(inspect_then_display)
    result = send(workspace, "Check whether Ford cars exist, then show me the Nissan options.")
    assert [car["listing_id"] for car in result["cars"]] == [11, 23, 41, 67, 78]
    assert state(workspace)["active_search"]["filters"] == {"make": "Nissan"}


def test_unsuccessful_presentation_does_not_block_the_requested_search(workspace):
    _, planner, _, _ = workspace

    def missing_old_page_then_search(message, working, call):
        missing = call("show_results", mode="current")
        assert "pagination" not in missing
        shown = call("search_inventory", filters={"make": "Ford"})
        assert shown["status"] == "found"
        return "Here are the Ford cars."

    planner.callbacks.append(missing_old_page_then_search)
    result = send(workspace, "Show my current cars, or Ford cars if I have none.")
    assert [car["listing_id"] for car in result["cars"]] == [1, 20, 53, 55, 62]
