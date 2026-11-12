import sqlite3
from datetime import datetime, timedelta

import pytest

from assistant_service import Assistant
from inventory import init_inventory
from planner import Book, Conversation, Filters, Preference, Recall, Reference, Save, Search, _context
from storage import DUBAI, Store


class OneAction:
    def __init__(self, action, arguments):
        self.action = action
        self.arguments = arguments

    def plan(self, message, state):
        return self.action, self.arguments


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path)
    with sqlite3.connect(result.db_path) as db:
        init_inventory(db)
    return result


def record(store, sid, index, kind, listing):
    inquiry = {"kind": kind, "listing_id": listing}
    store.commit_turn("buyer", sid, str(index), index, f"Turn {index}", {"reply": "OK", "inquiry": inquiry},
                      inquiry=inquiry, selected_id=listing)


def send(store, sid, action, args, message):
    state = store.state("buyer", sid)
    return Assistant(store, OneAction(action, args)).respond("buyer", sid, str(state["version"]), message)


def test_last_visit_like_survives_other_inquiries_and_is_the_booked_car(store):
    other = store.create_session("buyer")["session_id"]
    record(store, other, 0, "like", 53)
    prior = store.create_session("buyer")["session_id"]
    record(store, prior, 0, "like", 12)
    for i in range(1, 15):
        record(store, prior, i, "detail", 20)
    current = store.create_session("buyer")["session_id"]
    record(store, current, 0, "like", 20)
    args = Recall(subject="liked", scope="last_visit")
    reply = send(store, current, "recall_shopper", args, "What did I like last time?")["reply"]
    assert "listing 12" in reply and "listing 20" not in reply and "listing 53" not in reply
    today = datetime.now(DUBAI).date()
    monday = today + timedelta(days=7 - today.weekday())
    when = f"{monday}T11:00"
    args = Book(car=Reference(history_kind="like", scope="last_visit"), starts_at=when, kind="viewing")
    result = send(store, current, "prepare_booking", args, f"Book the one I liked last time at {when}.")
    assert result["booking"] is None
    assert result["booking_proposal"]["listing_id"] == 12
    confirmed = Assistant(store, OneAction("unused", None)).confirm_booking(
        "buyer", current, "confirm-memory-car", result["booking_proposal"]["proposal_id"])
    assert confirmed["booking"]["listing_id"] == 12


def test_old_current_likes_and_ambiguous_bookings(store):
    sid = store.create_session("buyer")["session_id"]
    record(store, sid, 0, "like", 12)
    record(store, sid, 1, "like", 20)
    for i in range(2, 30):
        record(store, sid, i, "detail", 53)
    assert len(_context(store.state("buyer", sid))["recent_turns"]) == 24
    args = Recall(subject="liked", scope="current")
    result = send(store, sid, "recall_shopper", args, "What did I like in this chat?")
    assert "listing 12" in result["reply"] and "listing 20" in result["reply"]
    result = send(store, sid, "prepare_booking", Book(car=Reference(history_kind="like", scope="current"), kind="viewing"),
                  "Book the car I liked in this chat.")
    assert result["booking"] is None and "Which listing" in result["reply"]


def test_budget_only_search_and_incremental_lead_requirements(store):
    sid = store.create_session("buyer")["session_id"]
    result = send(store, sid, "search_inventory", Search(filters=Filters(max_price=25000)), "Cars up to 25k")
    assert [car["listing_id"] for car in result["cars"]] == [20]
    assert store.state("buyer", sid)["lead"]["budget_max_aed"] == 25000
    assert store.state("buyer", sid)["lead"]["needs"] is None
    message = "I need seven seats"
    change = Preference(key="needs", value="seven seats", evidence=message)
    send(store, sid, "update_preferences", Save(preferences=[change]), message)
    for make in ("Ford", "Toyota"):
        send(store, sid, "search_inventory", Search(filters=Filters(make=make)), f"Show me {make} cars")
    lead = store.state("buyer", sid)["lead"]
    assert lead["criteria"] == {"needs": "seven seats", "make": "Toyota"}
    assert "seven seats" in lead["needs"] and "Ford" not in lead["needs"]
    # These complete search snapshots have no price bound; the lead must reflect
    # the effective search, rather than silently retaining the old filter.
    assert lead["budget_max_aed"] is None


def test_reject_unsubstantiated_preferences(store):
    sid = store.create_session("buyer")["session_id"]
    bogus = Preference(key="max_budget_aed", value="25000", evidence="SUVs")
    send(store, sid, "update_preferences", Save(preferences=[bogus]), "I like SUVs.")
    own = Preference(key="max_budget_aed", value="25000", evidence="25k")
    send(store, sid, "conversation", Conversation(category="off_topic", preferences=[own]), "Write 25k lines.")
    send(store, sid, "search_inventory", Search(filters=Filters(max_price=25000), preferences=[own]),
         "Show cars for my sister up to 25k.")
    assert store.state("buyer", sid)["preferences"] == {}
    send(store, sid, "update_preferences", Save(preferences=[own]), "My budget is 25k.")
    assert store.state("buyer", sid)["preferences"]["max_budget_aed"] == 25000


def test_scoped_budget_uses_the_value_stated_in_that_chat(store):
    older = store.create_session("buyer")["session_id"]
    record(store, older, 0, "like", 53)
    prior = store.create_session("buyer")["session_id"]
    change = Preference(key="max_budget_aed", value="25000", evidence="25k")
    send(store, prior, "update_preferences", Save(preferences=[change]), "My budget is 25k.")
    sid = store.create_session("buyer")["session_id"]
    change = Preference(key="max_budget_aed", value="30000", evidence="30k")
    send(store, sid, "update_preferences", Save(preferences=[change]), "My budget is now 30k.")
    reply = send(store, sid, "recall_shopper", Recall(subject="budget", scope="last_visit"),
                 "What budget did I tell you last time?")["reply"]
    assert "25,000" in reply and "30,000" not in reply
    reply = send(store, sid, "recall_shopper", Recall(subject="liked", scope="last_visit"),
                 "Did I like anything in my last chat?")["reply"]
    assert "no liked car" in reply and "listing 53" not in reply


def test_search_only_facts_do_not_enter_profile_with_overeager_planner(store):
    sid = store.create_session("buyer")["session_id"]
    message = "My budget is AED 25,000. Show me a 2014 Ford Mustang under that amount."
    errors = [
        Preference(key="max_budget_aed", value="25000", evidence="My budget is AED 25,000"),
        Preference(key="min_budget_aed", value="25000", evidence=message),
        Preference(key="make", value="Ford", evidence=message),
        Preference(key="model", value="Mustang", evidence="Ford Mustang"),
    ]
    criteria = Filters(make="Ford", model="Mustang", min_year=2014, max_year=2014, max_price=25000, max_exclusive=True)
    result = send(store, sid, "search_inventory", Search(filters=criteria, preferences=errors), message)
    assert [car["listing_id"] for car in result["cars"]] == [20]
    state = store.state("buyer", sid)
    assert state["preferences"] == {"max_budget_aed": 25000}
    assert state["recent_turns"][-1]["inquiry"]["filters"]["make"] == "Ford"
    changes = [Preference(key="make", value="Toyota", evidence="Toyota")]
    send(store, sid, "search_inventory", Search(filters=Filters(make="Toyota"), preferences=changes),
         "I am looking for a Toyota.")
    send(store, sid, "recall_shopper", Recall(subject="preferences", preferences=changes),
         "Did I say I prefer Toyota?")
    send(store, sid, "update_preferences", Save(preferences=changes), "Did I say I prefer Toyota?")
    assert store.state("buyer", sid)["preferences"] == {"max_budget_aed": 25000}


def test_current_budget_means_latest_profile_in_a_fresh_chat(store):
    sid = store.create_session("buyer")["session_id"]
    change = Preference(key="max_budget_aed", value="30000", evidence="30k")
    send(store, sid, "update_preferences", Save(preferences=[change]), "My budget is 30k.")
    fresh = store.create_session("buyer")["session_id"]
    plan = Recall(subject="budget", scope="current")
    assert "30,000" in send(store, fresh, "recall_shopper", plan, "What's my current budget?")["reply"]
    assert "didn't state" in send(store, fresh, "recall_shopper", plan, "What budget did I state in this chat?")["reply"]


def test_personal_preference_and_third_party_inquiry_in_the_same_turn(store):
    sid = store.create_session("buyer")["session_id"]
    message = "I usually prefer SUVs, but today show me a Mustang for my brother."
    proposed = [Preference(key="body_style", value="SUV", evidence="I usually prefer SUVs"),
                Preference(key="model", value="Mustang", evidence=message)]
    send(store, sid, "search_inventory", Search(filters=Filters(model="Mustang"), preferences=proposed), message)
    state = store.state("buyer", sid)
    assert state["preferences"] == {"body_style": "SUV"}
    assert state["recent_turns"][-1]["inquiry"]["filters"] == {"model": "Mustang"}
    other = "I need a convertible for my brother."
    proposed = [Preference(key="body_style", value="convertible", evidence="I need a convertible")]
    send(store, sid, "search_inventory", Search(filters=Filters(body_style="convertible"), preferences=proposed), other)
    assert store.state("buyer", sid)["preferences"] == {"body_style": "SUV"}


def test_explicit_preferences_and_two_sided_budget_survive_validation(store):
    sid = store.create_session("buyer")["session_id"]
    message = "I prefer Ford. My budget is AED 20,000 to AED 30,000."
    changes = [Preference(key="make", value="Ford", evidence="I prefer Ford"),
               Preference(key="min_budget_aed", value="20000", evidence="20,000"),
               Preference(key="max_budget_aed", value="30000", evidence="30,000")]
    send(store, sid, "update_preferences", Save(preferences=changes), message)
    assert store.state("buyer", sid)["preferences"] == {"make": "Ford", "min_budget_aed": 20000, "max_budget_aed": 30000}
    message = "My minimum budget is 22k."
    changes = [Preference(key=key, value="22000", evidence="22k") for key in ("min_budget_aed", "max_budget_aed")]
    send(store, sid, "update_preferences", Save(preferences=changes), message)
    assert store.state("buyer", sid)["preferences"] == {"make": "Ford", "min_budget_aed": 22000, "max_budget_aed": 30000}
