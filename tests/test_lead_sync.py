"""Keep exported lead details aligned with the shopper's active inquiry."""

import csv

from test_api import api, ask, new_session


def lead_state(client, session):
    return client.app.state.store.state("shopper", session)["lead"]


def exported_lead(client, session):
    with client.app.state.store.leads_path.open(encoding="utf-8", newline="") as source:
        return next(row for row in csv.DictReader(source) if row["session_id"] == session)


def search(client, planner, session, message, filters):
    planner.next_action("search_inventory", filters=filters)
    response = ask(client, session, message)
    assert response.status_code == 200, response.text
    return response.json()


def test_new_make_clears_previous_model_price_and_feature_requirements_but_not_profile(api):
    client, planner = api
    session = new_session(client)
    planner.next_action("update_preferences", preferences=[
        {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is AED 25,000"}])
    assert ask(client, session, "My budget is AED 25,000.").status_code == 200
    search(client, planner, session, "Find a white Ford Mustang between 20k and 25k, after 2010, under 180k km.",
           {"make": "Ford", "model": "Mustang", "color": "white", "min_price": 20000,
            "max_price": 25000, "min_year": 2011, "max_mileage": 180000})
    assert "Mustang" in exported_lead(client, session)["needs"]

    search(client, planner, session, "New search: any Toyota, with no price or other restrictions.", {"make": "Toyota"})
    lead = lead_state(client, session)
    assert lead["criteria"] == {"make": "Toyota"}
    assert lead["needs"] == "Toyota"
    assert lead["budget_min_aed"] is None and lead["budget_max_aed"] is None
    exported = exported_lead(client, session)
    assert exported["needs"] == "Toyota"
    assert exported["budget_min_aed"] == exported["budget_max_aed"] == ""
    state = client.app.state.store.state("shopper", session)
    assert state["preferences"] == {"max_budget_aed": 25000}
    assert state["active_search"]["filters"] == {"make": "Toyota"}


def test_removing_color_preserves_only_filters_still_used_by_search(api):
    client, planner = api
    session = new_session(client)
    search(client, planner, session, "Show white Ford cars up to AED 50,000.",
           {"make": "Ford", "color": "white", "max_price": 50000})
    search(client, planner, session, "Any color, keeping the same make and price limit.",
           {"make": "Ford", "max_price": 50000})
    lead = lead_state(client, session)
    assert lead["criteria"] == {"make": "Ford"}
    assert lead["needs"] == "Ford"
    assert lead["budget_max_aed"] == 50000
    assert "white" not in exported_lead(client, session)["needs"]


def test_unrestricted_search_clears_all_previous_search_requirements(api):
    client, planner = api
    session = new_session(client)
    search(client, planner, session, "Find a Ford Mustang under AED 25,000.",
           {"make": "Ford", "model": "Mustang", "max_price": 25000})
    search(client, planner, session, "Start over and show every available car.", {})
    lead = lead_state(client, session)
    assert lead["criteria"] == {} and lead["needs"] is None
    assert lead["budget_min_aed"] is None and lead["budget_max_aed"] is None
    exported = exported_lead(client, session)
    assert exported["needs"] == exported["budget_max_aed"] == exported["budget_min_aed"] == ""


def test_preference_only_change_patches_budget_without_losing_current_search_criteria(api):
    client, planner = api
    session = new_session(client)
    search(client, planner, session, "Find a white Ford under AED 25,000.",
           {"make": "Ford", "color": "white", "max_price": 25000})
    planner.next_action("update_preferences", preferences=[
        {"key": "max_budget_aed", "value": "30000", "evidence": "My budget is now AED 30,000"}])
    response = ask(client, session, "My budget is now AED 30,000.")
    assert response.status_code == 200, response.text
    lead = lead_state(client, session)
    assert lead["criteria"] == {"make": "Ford", "color": "white"}
    assert lead["budget_max_aed"] == 30000
    assert set(lead["needs"].split(", ")) == {"Ford", "white"}
    assert exported_lead(client, session)["budget_max_aed"] == "30000"
    assert client.app.state.store.state("shopper", session)["preferences"] == {"max_budget_aed": 30000}


def test_pagination_does_not_restore_old_budget_after_personal_budget_correction(api):
    client, planner = api
    session = new_session(client)
    search(client, planner, session, "Find cars up to AED 150,000.", {"max_price": 150000})
    planner.next_action("update_preferences", preferences=[
        {"key": "max_budget_aed", "value": "200000", "evidence": "My budget is now AED 200,000"}])
    assert ask(client, session, "My budget is now AED 200,000.").status_code == 200
    before = lead_state(client, session)
    planner.next_action("next_inventory_page")
    response = ask(client, session, "Show me more results from that search.")
    assert response.status_code == 200, response.text
    assert lead_state(client, session) == before
    assert exported_lead(client, session)["budget_max_aed"] == "200000"


def test_unrestricted_new_search_clears_unstructured_legacy_lead_needs(api):
    client, planner = api
    session = new_session(client)
    store = client.app.state.store
    store.commit_turn("shopper", session, "legacy", 0, "Find a Mustang", {"reply": "Saved"},
                      lead={"needs": "Ford Mustang", "budget_max_aed": 25000})
    assert lead_state(client, session)["criteria"] == {}
    search(client, planner, session, "Start over, show all cars without restrictions.", {})
    lead = lead_state(client, session)
    assert lead["criteria"] == {} and lead["needs"] is None
    assert exported_lead(client, session)["needs"] == ""
