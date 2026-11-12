"""Shopper constraints control presentation even when proposed model arguments differ."""
from test_agent_workspace import workspace, send, state


def test_model_cannot_raise_explicit_price_limit_or_lead_budget(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 25000}}))
    result = send(workspace, "Find a Mustang under AED 20,000.")
    assert result["cars"] == [] and result["pagination"]["total"] == 0
    current = state(workspace)
    assert current["active_search"]["filters"]["max_price"] == 20000
    assert current["active_search"]["filters"]["max_exclusive"] is True
    assert current["lead"]["budget_max_aed"] == 20000


def test_internal_alternative_cannot_bypass_explicit_limit_at_presentation(workspace):
    _, planner, _, _ = workspace
    planner.queue(
        ("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 20000}}),
        ("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 25000}, "display": False}),
        ("show_results", {"mode": "latest"}),
    )
    result = send(workspace, "Find a Mustang under AED 20,000.")
    assert planner.observations[-1][1]["status"] == "presentation_limit"
    assert result["cars"] == []
    current = state(workspace)
    assert current["active_search"]["filters"]["max_price"] == current["lead"]["budget_max_aed"] == 20000
    assert current["retrieved_search"]["filters"]["max_price"] == 25000


def test_first_public_display_of_internal_alternative_still_checks_price_request(workspace):
    _, planner, _, _ = workspace
    planner.queue(
        ("search_inventory", {"filters": {"model": "Mustang", "max_price": 25000}, "display": False}),
        ("show_results", {"mode": "latest"}),
    )
    result = send(workspace, "Find a Mustang under AED 20,000.")
    assert planner.observations[-1][1]["status"] == "needs_search"
    assert result["cars"] == [] and "pagination" not in result
    assert state(workspace)["active_search"] is None


def test_unclear_currency_does_not_clear_existing_search_or_lead(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "max_price": 25000}}))
    send(workspace, "Show Fords up to AED 25,000.")
    original = state(workspace)
    planner.queue(("search_inventory", {"filters": {"make": "Nissan", "max_price": 20000}}))
    result = send(workspace, "Show Nissan cars under USD 20,000.")
    assert "pagination" not in result
    assert planner.observations[-1][1]["status"] == "needs_clarification"
    current = state(workspace)
    assert current["lead"] == original["lead"] and current["active_search"] == original["active_search"]


def test_inclusive_price_boundary_overrides_model_strict_boundary(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"model": "Mustang", "max_price": 21500, "max_exclusive": True}}))
    result = send(workspace, "Show Mustangs up to AED 21,500.")
    assert [car["listing_id"] for car in result["cars"]] == [20]
    assert not result["filters"].get("max_exclusive")


def test_personal_budget_and_same_strict_search_do_not_require_clarification(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 25000}}))
    result = send(workspace, "My budget is AED 25,000. I prefer convertibles. Find a Ford Mustang under AED 25,000.")
    assert [car["listing_id"] for car in result["cars"]] == [20]
    assert result["filters"]["max_exclusive"] is True
    current = state(workspace)
    assert current["preferences"]["max_budget_aed"] == 25000
    assert current["lead"]["budget_max_aed"] == 25000


def test_strict_search_at_personal_budget_still_excludes_equal_price(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 21500}}))
    result = send(workspace, "My budget is AED 21,500. Find a Ford Mustang under AED 21,500.")
    assert result["cars"] == [] and result["pagination"]["total"] == 0
    assert result["filters"]["max_price"] == 21500 and result["filters"]["max_exclusive"] is True
    assert state(workspace)["preferences"]["max_budget_aed"] == 21500
