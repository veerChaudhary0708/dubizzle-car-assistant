"""Exercise native multi-tool turns through Assistant.respond, without a legacy plan stub."""

from collections import deque
from contextlib import closing
import copy
import sqlite3
from uuid import uuid4

import pytest

from assistant_service import Assistant
from inventory import init_inventory
from planner import ModelUnavailable, TOOLS
from storage import Store
from test_storage import next_monday


class NativePlanner:
    def __init__(self):
        self.callbacks = deque()
        self.observations = []

    def run_turn(self, message, state, execute):
        assert self.callbacks, "Unexpected native planner invocation"

        def call(name, **arguments):
            observation = execute(name, TOOLS[name][0].model_validate(arguments))
            self.observations.append((name, copy.deepcopy(observation)))
            return observation

        return self.callbacks.popleft()(message, state, call)

    def queue(self, *actions, reply="Here are the verified results."):
        def run(message, state, call):
            for name, arguments in actions:
                call(name, **arguments)
            return reply
        self.callbacks.append(run)


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path)
    with closing(sqlite3.connect(store.db_path)) as connection:
        init_inventory(connection)
    planner = NativePlanner()
    assistant = Assistant(store, planner)
    sid = store.create_session("shopper")["session_id"]
    return store, planner, assistant, sid


def send(workspace, message, request_id=None):
    _, _, assistant, sid = workspace
    return assistant.respond("shopper", sid, request_id or uuid4().hex, message)


def state(workspace):
    store, _, _, sid = workspace
    return store.state("shopper", sid)


def display_snapshot(value):
    return {key: copy.deepcopy(value.get(key)) for key in
            ("shown_ids", "selected_id", "active_search", "last_results", "lead", "pending_booking")}


def test_multiple_actions_read_staged_search_and_preserve_all_events_and_cursor(workspace):
    store, planner, _, sid = workspace

    def combined(message, working, call):
        found = call("search_inventory", filters={})
        assert [car["listing_id"] for car in found["cars"]] == [1, 2, 3, 4, 5]
        assert working["shown_ids"] == [1, 2, 3, 4, 5]
        call("like_listing", car={"position": 2})
        detail = call("read_listing", car={"listing_id": 2}, attribute="year", focus=True)
        assert detail["facts"]["cars"][0]["year"] == 2019
        assert working["selected_id"] == 2
        return "I saved the second car, a 2019 Mercedes-Benz C-Class."

    planner.callbacks.append(combined)
    result = send(workspace, "Show available cars, save the second one, and tell me its year.")
    assert [car["listing_id"] for car in result["cars"]] == [1, 2, 3, 4, 5]
    assert [event["kind"] for event in store.inquiries("shopper", sid)] == ["detail", "like", "search"]
    assert len(store.history("shopper", sid)) == 1
    assert state(workspace)["active_search"]["next_offset"] == 5
    planner.queue(("show_results", {"mode": "next"}))
    assert [car["listing_id"] for car in send(workspace, "Show more.")["cars"]] == [6, 7, 8, 9, 10]


def test_generation_failure_discards_staged_profile_search_and_proposal(workspace):
    store, planner, _, sid = workspace
    before = state(workspace)

    def interrupted(message, working, call):
        call("update_preferences", preferences=[
            {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is 25k"}])
        call("search_inventory", filters={"make": "Ford", "model": "Mustang"})
        prepared = call("prepare_booking", car={"listing_id": 20}, day=next_monday().isoformat(),
                        time="10:00", kind="test_drive")
        assert prepared["booking_proposal"]["listing_id"] == 20
        assert not prepared.get("booking")
        assert working["preferences"]["max_budget_aed"] == 25000
        # The model fails after receiving a successful staged tool observation.
        raise ModelUnavailable("The response could not be completed.")

    planner.callbacks.append(interrupted)
    with pytest.raises(ModelUnavailable):
        send(workspace, f"My budget is 25k. Find a Mustang and prepare listing 20 for a test drive on {next_monday()} at 10 am.",
             request_id="interrupted-turn")
    assert state(workspace) == before
    assert store.history("shopper", sid) == []
    assert store.inquiries("shopper", sid) == []
    assert store.replay("shopper", sid, "interrupted-turn") is None
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM preferences").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM turn_events").fetchone()[0] == 0


def test_inspection_of_shown_or_all_matches_never_changes_focus_or_pagination(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Mercedes-Benz"}}),
                  ("read_listing", {"car": {"position": 2}, "attribute": "year", "focus": True}))
    send(workspace, "Show Mercedes cars and tell me the second one's year.")
    before = display_snapshot(state(workspace))

    def inspect(message, working, call):
        shown = call("inspect_results", scope="shown")
        all_matches = call("inspect_results", scope="search")
        assert [car["listing_id"] for car in shown["facts"]["cars"]] == before["shown_ids"]
        assert len(all_matches["facts"]["cars"]) == 20
        assert shown["facts"]["coverage"]["price_aed"]["unknown"] == 5
        assert all_matches["facts"]["coverage"]["price_aed"]["unknown"] == 20
        assert display_snapshot(working) == before
        return "The prices of these Mercedes listings are unknown."

    planner.callbacks.append(inspect)
    result = send(workspace, "Compare the prices of these cars, including the other search matches.")
    assert result["cars"] == []
    assert display_snapshot(state(workspace)) == before


def test_internal_lookup_preserves_visible_search_lead_focus_and_pending_booking(workspace):
    store, planner, _, sid = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford"}}),
                  ("prepare_booking", {"car": {"listing_id": 20}, "day": next_monday().isoformat(), "kind": "test_drive"}))
    send(workspace, "Show Fords, then arrange a test drive of listing 20 next Monday.")
    before = display_snapshot(state(workspace))
    assert before["pending_booking"]

    def lookup(message, working, call):
        found = call("search_inventory", filters={"make": "Toyota"}, display=False)
        assert len(found["facts"]["cars"]) == 5
        assert all(car["make"].lower() == "toyota" for car in found["facts"]["cars"])
        assert not found.get("cars") and "pagination" not in found
        detail = call("read_listing", car={"listing_id": found["facts"]["cars"][0]["listing_id"]},
                      attribute="overview")
        assert detail["facts"]["cars"][0]["listing_id"] == 61
        # This read gathers comparison evidence; it does not select the Toyota.
        assert display_snapshot(working) == before
        return "There are five Toyota listings available for comparison."

    planner.callbacks.append(lookup)
    result = send(workspace, "Check whether there are Toyota alternatives, while keeping this booking and my current results.")
    assert result["cars"] == []
    assert display_snapshot(state(workspace)) == before
    restored = Store(store.db_path.parent).state("shopper", sid)
    assert restored["retrieved_search"]["filters"] == {"make": "Toyota"}
    assert restored["retrieved_search"]["result_ids"] == [61, 76, 91, 95, 98]
    planner.queue(("show_results", {"mode": "latest"}))
    shown_later = send(workspace, "Show the Toyota alternatives you just found.")
    assert [car["listing_id"] for car in shown_later["cars"]] == [61, 76, 91, 95, 98]


def test_show_current_redisplays_the_same_page_without_advancing_or_losing_focus(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {}}))
    send(workspace, "Show available cars.")
    planner.queue(("show_results", {"mode": "next"}),
                  ("read_listing", {"car": {"position": 2}, "attribute": "year", "focus": True}))
    send(workspace, "Show the second page and tell me the year of its second car.")
    before = state(workspace)
    assert before["shown_ids"] == [6, 7, 8, 9, 10] and before["selected_id"] == 7
    planner.queue(("show_results", {"mode": "current"}))
    replayed_page = send(workspace, "Show those cars again.")
    after = state(workspace)
    assert [car["listing_id"] for car in replayed_page["cars"]] == before["shown_ids"]
    assert after["active_search"]["next_offset"] == before["active_search"]["next_offset"] == 10
    assert after["shown_ids"] == before["shown_ids"] and after["selected_id"] == 7


def test_zero_match_attempt_keeps_last_results_and_car_focus_but_updates_attempted_filters(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "model": "Mustang", "max_price": 25000}}))
    send(workspace, "Find a Mustang up to AED 25,000.")
    before = state(workspace)
    planner.queue(("search_inventory", {"filters": {"max_price": 1}, "mode": "refine"}))
    empty = send(workspace, "What about up to one dirham?")
    after = state(workspace)
    assert empty["cars"] == [] and empty["total_matches"] == 0
    assert after["active_search"]["result_ids"] == []
    assert after["active_search"]["filters"] == {"make": "Ford", "model": "Mustang", "max_price": 1}
    assert after["last_results"] == before["last_results"]
    assert after["shown_ids"] == before["shown_ids"] == [20]
    assert after["selected_id"] == before["selected_id"] == 20
    planner.queue(("read_listing", {"attribute": "mileage"}))
    send(workspace, "What is that Mustang's mileage?")
    assert planner.observations[-1][1]["facts"]["cars"][0]["mileage_km"] == 164000


@pytest.mark.parametrize("order,field", [("lowest_price", "price_aed"), ("lowest_mileage", "mileage_km")])
def test_sorting_nissan_keeps_all_five_unknown_values_and_reports_coverage(workspace, order, field):
    _, planner, _, _ = workspace

    def sorted_unknowns(message, working, call):
        found = call("search_inventory", filters={"make": "Nissan"}, order=order)
        assert [car["listing_id"] for car in found["cars"]] == [11, 23, 41, 67, 78]
        coverage = found["facts"]["coverage"][field]
        assert coverage["known"] == 0 and coverage["unknown"] == 5
        assert coverage["minimum_known"] is None and coverage["complete"] is False
        return "All five Nissan listings lack verified values for that ranking."

    planner.callbacks.append(sorted_unknowns)
    result = send(workspace, "Show Nissan cars, sorted by the requested value.")
    assert result["total_matches"] == 5
    assert state(workspace)["active_search"]["result_ids"] == [11, 23, 41, 67, 78]


def test_third_party_context_survives_followups_without_saving_their_like_as_personal(workspace):
    store, planner, _, sid = workspace
    planner.queue(("update_preferences", {"preferences": [
        {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is 25k"}]}))
    send(workspace, "My budget is 25k.")
    planner.queue(("search_inventory", {"filters": {"make": "Toyota"}, "subject": "other"}))
    send(workspace, "Start a separate Toyota search for my brother.")
    assert state(workspace)["shopping_context"]["subject"] == "other"
    planner.queue(("read_listing", {"car": {"position": 1}, "attribute": "year", "focus": True}))
    send(workspace, "What year is the first one?")
    planner.queue(("like_listing", {"car": {"position": 1}}))
    send(workspace, "He likes the first one.")
    assert state(workspace)["shopping_context"]["subject"] == "other"
    assert state(workspace)["preferences"] == {"max_budget_aed": 25000}
    assert store.inquiries("shopper", sid, kind="like") == []


def test_refinement_keeps_prior_filters_and_removes_only_explicit_constraints(workspace):
    _, planner, _, _ = workspace
    planner.queue(("search_inventory", {"filters": {"make": "Ford", "max_price": 25000, "color": "white"}}))
    send(workspace, "Show white Fords up to AED 25,000.")
    planner.queue(("search_inventory", {"filters": {}, "mode": "refine", "remove_filters": ["color"]}))
    relaxed = send(workspace, "Any color is fine; keep the other filters.")
    assert relaxed["filters"] == {"make": "Ford", "max_price": 25000}
    assert [car["listing_id"] for car in relaxed["cars"]] == [20]


def test_recall_tools_see_preferences_and_likes_staged_earlier_in_the_same_turn(workspace):
    store, planner, _, sid = workspace

    def combined(message, working, call):
        call("update_preferences", preferences=[
            {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is 25k"}])
        recalled = call("recall_shopper", subject="budget")
        assert "25,000" in recalled["reply"]
        call("search_inventory", filters={"make": "Ford", "model": "Mustang"})
        call("like_listing", car={"listing_id": 20})
        liked = call("recall_shopper", subject="liked", scope="current")
        assert "Mustang" in liked["reply"]
        assert store.history("shopper", sid) == []  # Nothing committed during tool execution.
        return "Your budget is AED 25,000, and you liked the Mustang."

    planner.callbacks.append(combined)
    send(workspace, "My budget is 25k. Find a Mustang, save it to my likes, and summarize what you remember.")
    assert len(store.history("shopper", sid)) == 1
    assert store.inquiries("shopper", sid, kind="like")[0]["listing_id"] == 20


def test_complete_memory_bundle_keeps_personal_like_after_recent_brother_search(workspace):
    store, planner, assistant, sid = workspace
    planner.queue(("update_preferences", {"preferences": [
        {"key": "max_budget_aed", "value": "25000", "evidence": "My budget is 25k"},
        {"key": "needs", "value": "convertibles", "evidence": "I prefer convertibles"}]}),
        ("search_inventory", {"filters": {"model": "Mustang"}}),
        ("like_listing", {"car": {"listing_id": 20}}))
    send(workspace, "My budget is 25k. I prefer convertibles. Show a Mustang and save it to my likes.")
    for number in range(13):
        planner.queue(("read_listing", {"car": {"listing_id": 20}, "attribute": "mileage"}))
        send(workspace, f"Remind me of listing 20's mileage, check {number}.")
    planner.queue(("search_inventory", {"filters": {"make": "Toyota"}, "subject": "other"}))
    send(workspace, "Start a separate Toyota search for my brother.")
    fresh = store.create_session("shopper")["session_id"]
    fresh_workspace = store, planner, assistant, fresh

    def recall_everything(message, working, call):
        # Even a narrower tool subject must disclose coverage rather than imply
        # that omitted likes are absent from the shopper's saved memory.
        observation = call("recall_shopper", subject="preferences")
        memory = observation["memory"]
        assert memory["preferences"] == {"max_budget_aed": 25000, "body_style": "convertible", "needs": "convertibles"}
        assert memory["liked_listing_ids"] == [20]
        assert memory["liked_count"] == 1 and memory["liked_complete"] is True
        assert memory["liked_cars"][0]["model"].lower() == "mustang"
        assert memory["recent_inquiries_complete"] is False
        assert memory["inquiry_count"] > len(memory["recent_inquiries"])
        assert not any(event["kind"] == "like" for event in memory["recent_inquiries"])
        latest = memory["recent_inquiries"][0]
        assert latest["kind"] == "search" and latest["subject"] == "other"
        assert "brother" in latest["message"]
        summary = call("recall_shopper", subject="all")
        assert "Mustang" in summary["reply"] and "saved personal likes" in summary["reply"]
        return "Your budget is AED 25,000, you prefer convertibles, and you liked the Mustang."

    planner.callbacks.append(recall_everything)
    send(fresh_workspace, "What was my budget, what do I prefer, and which car did I like?")
    assert store.state("shopper", fresh)["selected_id"] is None


def test_memory_bundle_scopes_include_staged_facts_only_in_all_and_current(workspace):
    store, planner, assistant, _ = workspace
    first = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", first, "first", 0, "My budget is25k. I like the Mustang.", {},
                      preferences={"max_budget_aed": 25000}, inquiry={"kind": "like", "listing_id": 20})
    last = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", last, "last", 0, "My budget is30k. I like the Mazda.", {},
                      preferences={"max_budget_aed": 30000}, inquiry={"kind": "like", "listing_id": 12})
    current = store.create_session("shopper")["session_id"]
    current_workspace = store, planner, assistant, current

    def scoped(message, working, call):
        call("update_preferences", preferences=[
            {"key": "max_budget_aed", "value": "35000", "evidence": "My budget is 35k"}])
        call("like_listing", car={"listing_id": 53})
        expectations = {"all": (35000, [53, 12, 20]), "current": (35000, [53]),
                        "previous": (30000, [12, 20]), "last_visit": (30000, [12])}
        for scope, (budget, likes) in expectations.items():
            observation = call("recall_shopper", subject="all", scope=scope)
            memory = observation["memory"]
            assert memory["scope"] == scope
            assert memory["preferences"]["max_budget_aed"] == budget
            assert memory["liked_listing_ids"] == likes
            assert memory["liked_count"] == len(likes) and memory["liked_complete"] is True
        assert store.state("shopper", current)["preferences"]["max_budget_aed"] == 30000
        assert store.inquiries("shopper", current, scope="current", kind="like") == []
        return "Updated your budget and saved this car."

    planner.callbacks.append(scoped)
    send(current_workspace, "My budget is 35k. I like listing 53. Summarize this chat and my earlier chats.")
    assert store.state("shopper", current)["preferences"]["max_budget_aed"] == 35000


def test_memory_bundle_preserves_unavailable_liked_ids_and_shopper_isolation(workspace):
    store, planner, assistant, sid = workspace
    store.commit_turn("shopper", sid, "removed-car", 0, "I liked this formerly available car.", {"reply": "Saved"},
                      inquiry={"kind": "like", "listing_id": 999}, preferences={"max_budget_aed": 25000})

    def unavailable(message, working, call):
        memory = call("recall_shopper", subject="all")["memory"]
        assert memory["liked_listing_ids"] == [999] and memory["liked_count"] == 1
        assert memory["liked_complete"] is True
        assert memory["liked_cars"][0]["available_in_inventory"] is False
        return "Your saved like is no longer in the inventory."

    planner.callbacks.append(unavailable)
    send(workspace, "Which car did I like?")
    other = store.create_session("other")["session_id"]

    def isolated(message, working, call):
        memory = call("recall_shopper", subject="all")["memory"]
        assert memory["preferences"] == {} and memory["preferences_complete"] is True
        assert memory["liked_cars"] == memory["liked_listing_ids"] == []
        assert memory["liked_count"] == 0 and memory["liked_complete"] is True
        assert memory["recent_inquiries"] == [] and memory["recent_inquiries_complete"] is True
        return "No saved personal preferences or likes are recorded for you."

    planner.callbacks.append(isolated)
    assistant.respond("other", other, "recall", "What do you remember about me?")
