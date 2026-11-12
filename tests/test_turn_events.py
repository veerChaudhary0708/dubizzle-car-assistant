"""Ordered turn events and shopping context survive atomic commits and restarts."""

import json
import sqlite3

import pytest

from storage import BookingError, RequestConflict, SessionNotFound, StaleSession, Store
from test_storage import inventory_store, next_monday


def search_event(ids, **filters):
    return {"kind": "search", "filters": filters, "result_ids": ids, "shown_ids": ids,
            "next_offset": len(ids), "order": "relevance"}


def persisted_events(store):
    with sqlite3.connect(store.db_path) as connection:
        return [(turn, ordinal, json.loads(event)) for turn, ordinal, event in connection.execute(
            "SELECT turn_id, ordinal, event FROM turn_events ORDER BY turn_id, ordinal")]


def test_one_turn_retains_search_like_and_detail_in_execution_order(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    events = [search_event([20], make="Ford", model="Mustang"),
              {"kind": "like", "listing_id": 20},
              {"kind": "detail", "listing_id": 20, "attribute": "mileage"}]
    store.commit_turn("a", sid, "combined", 0, "Find a Mustang, save it, and tell me the mileage.",
                      {"reply": "Found and saved the Mustang. Mileage: 164,000 km."},
                      inquiries=events, shown_ids=[20], selected_id=20)
    assert [row[2] for row in persisted_events(store)] == events
    assert [row[1] for row in persisted_events(store)] == [0, 1, 2]
    assert [event["kind"] for event in store.inquiries("a", sid)] == ["detail", "like", "search"]
    assert store.inquiries("a", sid, kind="like")[0]["listing_id"] == 20
    reopened = Store(tmp_path)
    state = reopened.state("a", sid)
    assert state["active_search"]["result_ids"] == [20]
    assert state["last_results"]["shown_ids"] == [20]
    assert len(reopened.history("a", sid)) == 1
    assert [row[2] for row in persisted_events(reopened)] == events
    with sqlite3.connect(store.db_path) as connection:
        assert json.loads(connection.execute("SELECT inquiry FROM turns").fetchone()[0]) == events[-1]


def test_empty_search_attempt_preserves_last_presented_results_independently(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    store.commit_turn("a", sid, "one", 0, "Show Ford cars", {"reply": "Two cars"},
                      inquiries=[search_event([20, 53], make="Ford")], shown_ids=[20, 53], selected_id=20)
    store.commit_turn("a", sid, "two", 1, "Any under one dirham?", {"reply": "No matches"},
                      inquiries=[search_event([], make="Ford", max_price=1),
                                 {"kind": "detail", "listing_id": 20, "attribute": "price"}])
    state = Store(tmp_path).state("a", sid)
    assert state["active_search"]["result_ids"] == []
    assert state["active_search"]["filters"]["max_price"] == 1
    assert state["last_results"]["shown_ids"] == [20, 53]
    assert state["shown_ids"] == [20, 53] and state["selected_id"] == 20
    fresh = store.create_session("a")
    assert fresh["active_search"] is None and fresh["last_results"] is None
    assert store.create_session("b")["last_results"] is None


def test_event_list_retry_is_idempotent_and_stale_or_foreign_writes_do_not_mutate(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    events = [search_event([61], make="Toyota"), {"kind": "like", "listing_id": 61}]
    response = {"reply": "Saved this Toyota for the current inquiry."}
    store.commit_turn("a", sid, "one", 0, "Find Toyota for my brother", response,
                      inquiries=events, shopping_context={"subject": "other", "relationship": "brother"})
    before = store.state("a", sid)
    assert store.commit_turn("a", sid, "one", 0, "Find Toyota for my brother", {"reply": "changed"},
                             inquiries=[search_event([20])], shopping_context={"subject": "self"}) == response
    assert store.state("a", sid) == before
    assert [row[2] for row in persisted_events(store)] == events
    with pytest.raises(RequestConflict):
        store.commit_turn("a", sid, "one", 1, "Different text", {}, inquiries=[])
    with pytest.raises(StaleSession):
        store.commit_turn("a", sid, "two", 0, "stale", {}, inquiries=[search_event([20])])
    with pytest.raises(SessionNotFound):
        store.commit_turn("b", sid, "intrusion", 1, "change shopper", {}, inquiries=[])
    assert store.state("a", sid) == before


def test_booking_failure_rolls_back_all_events_context_and_preferences(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    before = store.state("a", sid)
    events = [search_event([20]), {"kind": "booking", "listing_id": 20}]
    with pytest.raises(BookingError):
        store.commit_turn("a", sid, "bad-slot", 0, "Find and book the Mustang at 10:15", {"reply": "invalid"},
                          inquiries=events, shopping_context={"subject": "other"},
                          preferences={"max_budget_aed": 25000},
                          booking={"listing_id": 20, "starts_at": f"{next_monday()}T10:15"})
    assert store.state("a", sid) == before
    assert persisted_events(store) == []
    assert store.history("a", sid) == []
    assert store.replay("a", sid, "bad-slot") is None


def test_shopping_subject_is_session_local_and_preserved_until_explicitly_changed(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    assert store.state("a", sid)["shopping_context"] == {"subject": "self"}
    store.commit_turn("a", sid, "other", 0, "For my brother", {},
                      shopping_context={"subject": "other", "relationship": "brother"})
    store.commit_turn("a", sid, "follow-up", 1, "Show more", {})
    assert Store(tmp_path).state("a", sid)["shopping_context"] == {
        "subject": "other", "relationship": "brother"}
    assert store.create_session("a")["shopping_context"] == {"subject": "self"}
    assert store.create_session("b")["shopping_context"] == {"subject": "self"}
    before = store.state("a", sid)
    with pytest.raises(ValueError):
        store.commit_turn("a", sid, "invalid", 2, "Invalid context", {},
                          shopping_context={"subject": "unknown"}, inquiries=[search_event([20])],
                          preferences={"max_budget_aed": 12345})
    assert store.state("a", sid) == before
    store.commit_turn("a", sid, "self", 2, "Now back to my own search", {}, shopping_context={"subject": "self"})
    assert store.state("a", sid)["shopping_context"] == {"subject": "self"}


def test_legacy_inquiries_migrate_once_without_changing_existing_multi_events(tmp_path):
    with sqlite3.connect(tmp_path / "assistant.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                shown_ids TEXT NOT NULL DEFAULT '[]', selected_id INTEGER
            );
            CREATE TABLE turns (
                turn_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                request_id TEXT NOT NULL, message TEXT NOT NULL, response TEXT NOT NULL, inquiry TEXT,
                UNIQUE(session_id, request_id)
            );
            INSERT INTO sessions VALUES ('old', 'a', 2, '[20]', 20);
            INSERT INTO turns VALUES (1, 'old', 'one', 'Find a Mustang', '{"reply":"Found it"}',
                '{"kind":"search","shown_ids":[20],"result_ids":[20]}');
            INSERT INTO turns VALUES (2, 'old', 'two', 'Thanks', '{"reply":"Welcome"}', NULL);
        """)
    migrated = Store(tmp_path)
    assert persisted_events(migrated) == [(1, 0, {"kind": "search", "shown_ids": [20], "result_ids": [20]})]
    assert migrated.state("a", "old")["last_results"]["shown_ids"] == [20]
    assert migrated.state("a", "old")["shopping_context"] == {"subject": "self"}
    migrated.commit_turn("a", "old", "three", 2, "Like it and tell me mileage", {},
                         inquiries=[{"kind": "like", "listing_id": 20},
                                    {"kind": "detail", "listing_id": 20, "attribute": "mileage"}])
    before = persisted_events(migrated)
    assert persisted_events(Store(tmp_path)) == before
    assert persisted_events(Store(tmp_path)) == before
    assert len(migrated.history("a", "old")) == 3


def test_explicit_empty_events_do_not_reappear_from_legacy_summary_after_restart(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    store.commit_turn("a", sid, "one", 0, "An internal lookup only", {},
                      inquiries=[], inquiry=search_event([20]))
    reopened = Store(tmp_path)
    assert reopened.inquiries("a", sid) == []
    assert reopened.state("a", sid)["active_search"] is None


def test_other_chat_scopes_follow_latest_activity_when_old_chats_resume(tmp_path):
    store = Store(tmp_path)
    first = store.create_session("a")["session_id"]
    store.commit_turn("a", first, "one", 0, "I liked the Mustang", {},
                      inquiries=[{"kind": "like", "listing_id": 20}], preferences={"max_budget_aed": 25000})
    second = store.create_session("a")["session_id"]
    store.commit_turn("a", second, "one", 0, "I liked the Explorer", {},
                      inquiries=[{"kind": "like", "listing_id": 53}], preferences={"max_budget_aed": 30000})
    assert store.inquiries("a", first, scope="last_visit")[0]["session_id"] == second
    assert [event["listing_id"] for event in store.inquiries("a", first, scope="previous")] == [53]
    store.commit_turn("a", first, "resume", 1, "My budget is now 35k", {},
                      inquiries=[{"kind": "detail", "listing_id": 20}], preferences={"max_budget_aed": 35000})
    fresh = store.create_session("a")["session_id"]
    store.create_session("a")  # An empty draft does not become the last visit.
    foreign = store.create_session("b")["session_id"]
    store.commit_turn("b", foreign, "one", 0, "Private activity", {}, inquiry={"kind": "like", "listing_id": 99})
    assert store.inquiries("a", fresh, scope="last_visit")[0]["session_id"] == first
    assert store.stated_preferences("a", fresh, scope="last_visit") == {"max_budget_aed": 35000}
    assert store.inquiries("a", second, scope="last_visit")[0]["session_id"] == first
    assert [event["session_id"] for event in store.inquiries("a", fresh, scope="previous")] == [first, second, first]


def test_internal_retrieval_survives_restart_without_replacing_presented_search(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    visible = search_event([20], make="Ford", model="Mustang")
    store.commit_turn("a", sid, "show", 0, "Show a Mustang", {},
                      inquiries=[visible], shown_ids=[20], selected_id=20)
    internal = {"kind": "retrieval", "filters": {"make": "Toyota"}, "order": "relevance",
                "result_ids": [61, 76, 91, 95, 98]}
    store.commit_turn("a", sid, "lookup", 1, "Check Toyota alternatives without changing these cards", {},
                      inquiries=[internal, {"kind": "detail", "listing_id": 20, "attribute": "price"}])
    state = Store(tmp_path).state("a", sid)
    assert state["retrieved_search"]["kind"] == "retrieval"
    assert state["retrieved_search"]["result_ids"] == [61, 76, 91, 95, 98]
    assert state["active_search"]["result_ids"] == state["last_results"]["result_ids"] == [20]
    assert state["shown_ids"] == [20] and state["selected_id"] == 20
    assert store.create_session("a")["retrieved_search"] is None
    assert store.create_session("b")["retrieved_search"] is None

    # A later presented search supersedes the internal lookup in retrieval context.
    store.commit_turn("a", sid, "next-search", 2, "Now show all Fords", {},
                      inquiries=[search_event([1, 20, 53, 55, 62], make="Ford")],
                      shown_ids=[1, 20, 53, 55, 62])
    latest = Store(tmp_path).state("a", sid)
    assert latest["retrieved_search"]["kind"] == "search"
    assert latest["retrieved_search"]["result_ids"] == [1, 20, 53, 55, 62]
