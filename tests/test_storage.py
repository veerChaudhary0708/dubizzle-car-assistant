import csv
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta

import pytest

from inventory import init_inventory
from storage import BookingError, DUBAI, RequestConflict, SessionNotFound, StaleSession, Store, validate_slot


def next_monday():
    today = datetime.now(DUBAI).date()
    return today + timedelta(days=7 - today.weekday())


def read_leads(store):
    with store.leads_path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def inventory_store(directory):
    store = Store(directory)
    with closing(sqlite3.connect(store.db_path)) as connection:
        init_inventory(connection)
    return store


def test_new_session_keeps_preferences_and_inquiries_but_clears_focus(tmp_path):
    store = Store(tmp_path)
    original = store.create_session("a")["session_id"]
    store.commit_turn("a", original, "one", 0, "My budget is 25k. Find a Ford.",
                      {"reply": "Here are two."}, shown_ids=[20, 53],
                      preferences={"max_budget_aed": 25000}, inquiry={"kind": "search", "shown_ids": [20, 53]})
    store.commit_turn("a", original, "two", 1, "I like the second one.",
                      {"reply": "You liked the Explorer."}, selected_id=53, inquiry={"kind": "like", "listing_id": 53})
    assert store.state("a", original)["selected_id"] == 53
    other = Store(tmp_path)
    fresh = other.create_session("a")
    assert (fresh["shown_ids"], fresh["selected_id"], fresh["recent_turns"]) == ([], None, [])
    assert fresh["preferences"] == {"max_budget_aed": 25000}
    assert fresh["prior_inquiries"][0]["listing_id"] == 53
    other.commit_turn("a", fresh["session_id"], "correction", 0, "Actually, 30k.",
                      {"reply": "Budget updated."}, preferences={"max_budget_aed": 30000})
    assert [row["value"] for row in other.preference_history("a", "max_budget_aed")] == [30000, 25000]
    isolated = other.create_session("b")
    assert isolated["preferences"] == {} and isolated["prior_inquiries"] == []
    assert other.preference_history("b", "max_budget_aed") == []
    with pytest.raises(SessionNotFound):
        other.history("b", original)
    with pytest.raises(SessionNotFound):
        other.replay("b", original, "one")
    with pytest.raises(SessionNotFound):
        other.commit_turn("b", original, "malicious", 2, "replace", {"reply": "overwritten"})


def test_recent_context_contains_24_complete_turns_while_history_remains_full(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("shopper")["session_id"]
    long_reply = "A complete explanation. " * 60
    for index in range(30):
        store.commit_turn("shopper", sid, f"turn-{index}", index, f"Question {index}",
                          {"reply": long_reply + str(index)})
    recent = store.state("shopper", sid)["recent_turns"]
    assert len(recent) == 24 and recent[0]["message"] == "Question 6"
    assert recent[-1]["reply"] == long_reply + "29"
    assert len(store.history("shopper", sid)) == 30


def test_simultaneous_booking_and_identical_retries(tmp_path):
    store = inventory_store(tmp_path)
    slot = next_monday().isoformat() + "T19:30"
    booking = {"kind": "test_drive", "listing_id": 20, "starts_at": slot}
    sessions = [(user, store.create_session(user)["session_id"]) for user in ["a", "b"]]

    def reserve(pair):
        user, sid = pair
        try:
            store.commit_turn(user, sid, "reserve", 0, "Book this time.", {"reply": "Booked"},
                              booking=booking, lead={"budget_max_aed": 25000, "needs": "Convertible"})
            return True
        except BookingError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        success = list(pool.map(reserve, sessions))
    assert sorted(success) == [False, True]
    winner_user, winner_sid = sessions[success.index(True)]
    saved = store.replay(winner_user, winner_sid, "reserve")
    assert saved["reply"] == "Booked"
    receipt = saved["booking"]
    assert {key: receipt[key] for key in ("listing_id", "kind", "starts_at")} == {
        "listing_id": 20, "kind": "test_drive", "starts_at": slot + ":00+04:00"}
    assert receipt["booking_id"] and receipt["status"] == "confirmed"
    assert receipt["session_id"] == winner_sid and receipt["car_label"] == "2014 Ford Mustang V6"
    assert store.list_bookings(winner_user) == [receipt]
    assert store.commit_turn(winner_user, winner_sid, "reserve", 0, "Book this time.", {"reply": "Duplicate"},
                             booking=booking) == saved
    with pytest.raises(RequestConflict):
        store.replay(winner_user, winner_sid, "reserve", "Different text using same ID")
    with pytest.raises(StaleSession):
        store.commit_turn(winner_user, winner_sid, "other", 0, "A second request", {"reply": "stale"})
    loser_user, loser_sid = sessions[success.index(False)]
    assert store.history(loser_user, loser_sid) == []
    assert store.state(loser_user, loser_sid)["version"] == 0
    assert len(read_leads(store)) == 1
    assert len(store.available_slots(20, next_monday())) == 23
    assert len(store.available_slots(12, next_monday())) == 24


def test_slot_boundaries_are_applied_in_dubai_time():
    monday = next_monday()
    assert validate_slot(20, f"{monday}T04:00:00+00:00").endswith("08:00:00+04:00")
    assert validate_slot(99, f"{monday}T10:00").endswith("10:00:00+04:00")
    for listing, day, hour in [(20, monday, "07:30"), (20, monday, "20:00"), (20, monday, "10:15"),
                               (20, monday + timedelta(days=6), "10:00")]:
        with pytest.raises(BookingError):
            validate_slot(listing, f"{day}T{hour}")


def test_csv_recovers_after_failed_export_without_duplicate_turns(tmp_path, monkeypatch):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    with monkeypatch.context() as scope:
        def fail_replace(*args):
            raise PermissionError("CSV is open")

        scope.setattr("storage.os.replace", fail_replace)
        with pytest.raises(PermissionError):
            store.commit_turn("a", sid, "one", 0, "I want a family car.", {"reply": "Recorded"},
                              lead={"needs": "space, 7 seats"})
    assert store.replay("a", sid, "one", "I want a family car.") == {"reply": "Recorded"}
    store.commit_turn("a", sid, "two", 1, "My budget is 30k.", {"reply": "Updated"},
                      lead={"budget_max_aed": 30000})
    rows = read_leads(store)
    assert len(rows) == 1
    assert rows[0]["needs"] == "space, 7 seats"
    assert rows[0]["budget_max_aed"] == "30000"
    assert store.state("a", sid)["lead"]["needs"] == "space, 7 seats"
    assert len(store.history("a", sid)) == 2
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT dirty FROM csv_export").fetchone() == (0,)


def test_prior_visits_and_lead_edits_use_explicit_scope(tmp_path):
    store = Store(tmp_path)
    past = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", past, "1", 0, "I like 20", {}, inquiry={"kind": "like", "listing_id": 20})
    current = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", current, "1", 0, "I prefer a sedan under 30k.", {}, inquiry={"kind": "search"},
                      lead={"budget_max_aed": 30000, "needs": "sedan", "criteria": {"body_style": "sedan"}})
    store.commit_turn("shopper", current, "2", 1, "I haven't got a maximum budget.", {}, lead={"budget_max_aed": None})
    lead = store.state("shopper", current)["lead"]
    assert lead["budget_max_aed"] is None and lead["needs"] == "sedan" and lead["criteria"] == {"body_style": "sedan"}
    later = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", later, "1", 0, "I like 53", {}, inquiry={"kind": "like", "listing_id": 53})
    # A resumed chat can recall activity in another chat created after it.
    assert [r["listing_id"] for r in store.inquiries("shopper", current, scope="previous", kind="like")] == [53, 20]
    assert store.inquiries("shopper", later, scope="last_visit", kind="like") == []
    assert store.inquiries("shopper", later, scope="last_visit")[0]["session_id"] == current
    assert [r["listing_id"] for r in store.inquiries("shopper", later, scope="current")] == [53]
    assert [r["listing_id"] for r in store.inquiries("shopper", later, kind="like")] == [53, 20]
    preference_visit = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", preference_visit, "1", 0, "My limit is 25k.", {},
                      preferences={"max_budget_aed": 25000, "color": "white"})
    store.commit_turn("shopper", preference_visit, "2", 1, "I don't care about color.", {}, preferences={"color": None})
    store.create_session("shopper")  # Opening a blank chat does not constitute a visit.
    next_visit = store.create_session("shopper")["session_id"]
    store.commit_turn("shopper", next_visit, "1", 0, "My limit is now 28k.", {}, preferences={"max_budget_aed": 28000})
    assert store.stated_preferences("shopper", next_visit, scope="last_visit") == {"max_budget_aed": 25000}
    assert store.stated_preferences("shopper", next_visit, scope="current") == {"max_budget_aed": 28000}
    assert store.inquiries("shopper", next_visit, scope="last_visit") == []
    with pytest.raises(SessionNotFound):
        store.inquiries("another-shopper", later)


def test_saved_chat_list_has_titles_latest_activity_and_owner_isolation(tmp_path):
    store = Store(tmp_path)
    first = store.create_session("a")["session_id"]
    second = store.create_session("a")["session_id"]
    store.create_session("a")  # Empty drafts should not clutter the saved-chat list.
    foreign = store.create_session("b")["session_id"]
    message = "Show me\n  a Mustang " + "under budget " * 10
    store.commit_turn("a", first, "first", 0, message, {"reply": "Found one"})
    store.commit_turn("a", second, "first", 0, "My other search", {"reply": "Okay"})
    store.commit_turn("b", foreign, "first", 0, "Private other shopper", {"reply": "Okay"})
    assert [chat["session_id"] for chat in store.list_sessions("a")] == [second, first]
    store.commit_turn("a", first, "later", 1, "Its mileage?", {"reply": "164,000 km"})
    chats = Store(tmp_path).list_sessions("a")
    assert [chat["session_id"] for chat in chats] == [first, second]
    assert chats[0]["title"] == " ".join(message.split())[:80]
    assert chats[0]["user_id"] == "a"
    assert datetime.fromisoformat(chats[0]["updated_at"]) >= datetime.fromisoformat(chats[0]["created_at"])
    assert store.list_sessions("nobody") == []
    assert [chat["session_id"] for chat in store.list_sessions("b")] == [foreign]


def test_pending_booking_survives_restart_preserves_on_omission_and_clears_explicitly(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session("a")["session_id"]
    pending = {"listing_id": 20, "kind": "test_drive", "day": next_monday().isoformat(), "starts_at": None}
    store.commit_turn("a", sid, "start", 0, "Test drive next Monday", {"reply": "What time?"},
                      selected_id=20, pending_booking=pending)
    store = Store(tmp_path)
    assert store.state("a", sid)["pending_booking"] == pending
    store.commit_turn("a", sid, "detail", 1, "What's its mileage?", {"reply": "164,000 km"})
    assert store.state("a", sid)["pending_booking"] == pending
    assert store.create_session("a")["pending_booking"] is None
    assert store.create_session("b")["pending_booking"] is None
    with pytest.raises(SessionNotFound):
        store.state("b", sid)
    store.commit_turn("a", sid, "cancel", 2, "Cancel that", {"reply": "Canceled"}, pending_booking=None)
    assert Store(tmp_path).state("a", sid)["pending_booking"] is None
    before_retry = store.list_sessions("a")
    # A late duplicate of an older request cannot revive a cancelled booking.
    assert store.commit_turn("a", sid, "start", 0, "Test drive next Monday", {"reply": "duplicate"},
                             pending_booking=pending) == {"reply": "What time?"}
    assert store.state("a", sid)["pending_booking"] is None
    assert store.state("a", sid)["version"] == 3
    assert store.list_sessions("a") == before_retry


def test_booking_failure_rolls_back_pending_state_and_all_turn_changes(tmp_path):
    store = inventory_store(tmp_path)
    sid = store.create_session("a")["session_id"]
    day = next_monday().isoformat()
    pending = {"listing_id": 20, "kind": "viewing", "day": day, "starts_at": None}
    store.commit_turn("a", sid, "start", 0, "View it Monday", {"reply": "What time?"}, pending_booking=pending)
    before = store.state("a", sid)
    with pytest.raises(BookingError):
        store.commit_turn("a", sid, "invalid", 1, "At 10:15", {"reply": "Should not commit"},
                          booking={"listing_id": 20, "starts_at": day + "T10:15"}, pending_booking=None,
                          preferences={"max_budget_aed": 99999}, lead={"budget_max_aed": 99999})
    assert store.state("a", sid) == before
    assert store.replay("a", sid, "invalid") is None
    assert read_leads(store) == []
    with pytest.raises(StaleSession):
        store.commit_turn("a", sid, "stale", 0, "Cancel", {}, pending_booking=None)
    assert store.state("a", sid) == before
    booking = {"listing_id": 20, "kind": "viewing", "starts_at": day + "T10:00"}
    store.commit_turn("a", sid, "valid", 1, "At 10:00", {"reply": "Confirmed"},
                      booking=booking, pending_booking=None)
    assert store.state("a", sid)["pending_booking"] is None
    assert len(store.available_slots(20, day)) == 23


def test_migration_preserves_existing_chats_preferences_and_unknown_dates(tmp_path):
    # Reproduce the original sessions schema, which had no timestamps or pending state.
    with sqlite3.connect(tmp_path / "assistant.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                shown_ids TEXT NOT NULL DEFAULT '[]', selected_id INTEGER
            );
            INSERT INTO sessions VALUES ('old-chat', 'a', 1, '[20]', 20);
            CREATE TABLE turns (
                turn_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions,
                request_id TEXT NOT NULL, message TEXT NOT NULL, response TEXT NOT NULL, inquiry TEXT,
                UNIQUE(session_id, request_id)
            );
            INSERT INTO turns VALUES (1, 'old-chat', 'old-request', 'My budget is 25k. Find a Mustang.',
                '{"reply":"Here is the Mustang"}', '{"kind":"search","shown_ids":[20]}');
            CREATE TABLE preferences (
                user_id TEXT NOT NULL, name TEXT NOT NULL, value TEXT NOT NULL,
                source_turn INTEGER NOT NULL REFERENCES turns, PRIMARY KEY(user_id, name)
            );
            INSERT INTO preferences VALUES ('a', 'max_budget_aed', '25000', 1);
            CREATE TABLE leads (
                session_id TEXT PRIMARY KEY REFERENCES sessions, budget_min_aed INTEGER,
                budget_max_aed INTEGER, needs TEXT, updated_at TEXT NOT NULL
            );
            INSERT INTO leads VALUES ('old-chat', NULL, 25000, 'Mustang', '2026-01-01T00:00:00+00:00');
        """)
    migrated = Store(tmp_path)
    old = migrated.state("a", "old-chat")
    assert old["shown_ids"] == [20] and old["selected_id"] == 20
    assert old["pending_booking"] is None
    assert old["created_at"] is None and old["updated_at"] is None
    assert old["preferences"] == {"max_budget_aed": 25000}
    assert old["lead"]["needs"] == "Mustang" and old["lead"]["criteria"] == {}
    assert old["active_search"]["shown_ids"] == [20]
    assert migrated.replay("a", "old-chat", "old-request") == {"reply": "Here is the Mustang"}
    assert migrated.list_sessions("a")[0]["title"] == "My budget is 25k. Find a Mustang."
    migrated.commit_turn("a", "old-chat", "resume", 1, "Hello again", {"reply": "Welcome back"},
                         pending_booking={"listing_id": 20})
    reopened = Store(tmp_path)
    assert reopened.state("a", "old-chat")["preferences"] == {"max_budget_aed": 25000}
    assert reopened.state("a", "old-chat")["pending_booking"] == {"listing_id": 20}
    assert reopened.list_sessions("a")[0]["created_at"] is None
    assert reopened.list_sessions("a")[0]["updated_at"] is not None
    assert len(reopened.history("a", "old-chat")) == 2
    assert read_leads(reopened)[0]["needs"] == "Mustang"
