"""SQLite conversation memory, simulated appointments, and a local leads CSV."""

import csv
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4


# Dubai observes UTC+04:00 year-round. This also works on Windows without tzdata.
DUBAI = timezone(timedelta(hours=4), "Asia/Dubai")
_UNSET = object()


class SessionNotFound(LookupError):
    pass


class StaleSession(Exception):
    pass


class RequestConflict(Exception):
    pass


class BookingError(ValueError):
    pass


def validate_slot(listing_id, starts_at, *, now=None):
    """Validate appointment timing; Store checks inventory membership before reserving."""
    try:
        slot = datetime.fromisoformat(starts_at) if isinstance(starts_at, str) else starts_at
        if not isinstance(slot, datetime):
            raise ValueError
        slot = slot.replace(tzinfo=DUBAI) if slot.tzinfo is None else slot.astimezone(DUBAI)
    except (ValueError, TypeError, OverflowError) as exc:
        raise BookingError("Provide a valid appointment date and time in Dubai.") from exc
    if slot.weekday() == 6 or not 8 <= slot.hour < 20:
        raise BookingError("Viewings and test drives run Monday to Saturday, 08:00–20:00 Dubai time.")
    if slot.minute not in (0, 30) or slot.second or slot.microsecond:
        raise BookingError("Choose a half-hour slot starting on the hour or half hour.")
    if slot <= (now or datetime.now(DUBAI)):
        raise BookingError("Choose a slot in the future.")
    return slot.isoformat(timespec="seconds")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    shown_ids TEXT NOT NULL DEFAULT '[]',
    selected_id INTEGER,
    pending_booking TEXT,
    shopping_context TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS turns (
    turn_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions,
    request_id TEXT NOT NULL,
    message TEXT NOT NULL,
    response TEXT NOT NULL,
    inquiry TEXT,
    UNIQUE(session_id, request_id)
);
CREATE TABLE IF NOT EXISTS turn_events (
    turn_id INTEGER NOT NULL REFERENCES turns,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    event TEXT NOT NULL,
    PRIMARY KEY(turn_id, ordinal)
);
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    source_turn INTEGER NOT NULL REFERENCES turns,
    PRIMARY KEY(user_id, name)
);
CREATE TABLE IF NOT EXISTS preference_changes (
    turn_id INTEGER NOT NULL REFERENCES turns,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(turn_id, name)
);
CREATE TABLE IF NOT EXISTS appointments (
    listing_id INTEGER NOT NULL,
    starts_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    turn_id INTEGER NOT NULL UNIQUE REFERENCES turns,
    PRIMARY KEY(listing_id, starts_at)
);
CREATE TABLE IF NOT EXISTS leads (
    session_id TEXT PRIMARY KEY REFERENCES sessions,
    budget_min_aed INTEGER,
    budget_max_aed INTEGER,
    needs TEXT,
    criteria TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS csv_export (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    dirty INTEGER NOT NULL
);
INSERT OR IGNORE INTO csv_export(id, dirty) VALUES(1, 1);
"""


class Store:
    def __init__(self, data_dir):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / "assistant.sqlite3"
        self.leads_path = directory / "leads.csv"
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
        with self._transaction(write=True) as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
            if "criteria" not in columns:
                connection.execute("ALTER TABLE leads ADD COLUMN criteria TEXT NOT NULL DEFAULT '{}'")
            session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            # Older databases did not record timestamps. Keep those dates unknown
            # instead of making a migrated chat appear to have been created today.
            for column in ("pending_booking", "shopping_context", "created_at", "updated_at"):
                if column not in session_columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")
            # Preserve the one-event history of older versions. New turns already
            # have ordinal zero, so reopening never duplicates or replaces events.
            connection.execute("""
                INSERT OR IGNORE INTO turn_events(turn_id, ordinal, event)
                SELECT turn_id, 0, inquiry FROM turns WHERE inquiry IS NOT NULL
            """)
        self._export_leads()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self, write=False):
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection

    @staticmethod
    def _owned_session(connection, user_id, session_id):
        row = connection.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND session_id = ?", (user_id, session_id)
        ).fetchone()
        if row is None:
            raise SessionNotFound("Session not found for this shopper.")
        return row

    def create_session(self, user_id):
        session_id = uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self._transaction(write=True) as connection:
            connection.execute("""
                INSERT INTO sessions(session_id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?)
            """, (session_id, user_id, timestamp, timestamp))
        return self.state(user_id, session_id)

    def list_sessions(self, user_id):
        """List this shopper's nonempty chats, most recently active first."""
        with self._transaction() as connection:
            rows = connection.execute("""
                SELECT s.session_id, s.user_id, s.created_at, s.updated_at,
                    (SELECT message FROM turns first
                     WHERE first.session_id = s.session_id ORDER BY first.turn_id LIMIT 1) AS title
                FROM sessions s JOIN turns t ON t.session_id = s.session_id
                WHERE s.user_id = ? GROUP BY s.session_id ORDER BY max(t.turn_id) DESC
            """, (user_id,))
            return [
                {**dict(row), "title": " ".join(row["title"].split())[:80]}
                for row in rows
            ]

    @staticmethod
    def _booking_receipts(connection, *, user_id=None, turn_id=None):
        """Build receipts only from saved appointments; old turns need no migration."""
        # A fresh Store can be read before inventory initialization. Historical
        # bookings must also remain visible if a listing is later removed.
        has_inventory = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'listings'"
        ).fetchone() is not None
        labels = "l.year, l.make, l.model, l.trim" if has_inventory else "NULL AS year, NULL AS make, NULL AS model, NULL AS trim"
        inventory_join = "LEFT JOIN listings l ON l.listing_id = a.listing_id" if has_inventory else ""
        clauses, parameters = [], []
        if user_id is not None:
            clauses.append("s.user_id = ?")
            parameters.append(user_id)
        if turn_id is not None:
            clauses.append("a.turn_id = ?")
            parameters.append(turn_id)
        rows = connection.execute(f"""
            SELECT a.turn_id, a.listing_id, a.starts_at, a.kind, t.session_id, t.response, {labels}
            FROM appointments a JOIN turns t ON t.turn_id = a.turn_id
            JOIN sessions s ON s.session_id = t.session_id {inventory_join}
            WHERE {' AND '.join(clauses) or '1'} ORDER BY a.starts_at, a.turn_id
        """, parameters)
        receipts = []
        for row in rows:
            booking_id = f"booking-{row['turn_id']}"
            car_label = " ".join(str(row[key]).title() for key in ("year", "make", "model", "trim")
                                 if row[key] not in (None, "")) or f"Listing {row['listing_id']}"
            recorded = json.loads(row["response"]).get("booking")
            if isinstance(recorded, dict) and recorded.get("booking_id") == booking_id and recorded.get("car_label"):
                car_label = recorded["car_label"]  # Preserve the committed receipt's label snapshot.
            receipts.append({
                "booking_id": booking_id, "status": "confirmed",
                "listing_id": row["listing_id"], "starts_at": row["starts_at"], "kind": row["kind"],
                "car_label": car_label, "session_id": row["session_id"],
            })
        return receipts

    def list_bookings(self, user_id):
        """Return this shopper's committed reservations, never proposed chat text."""
        with self._transaction() as connection:
            return self._booking_receipts(connection, user_id=user_id)

    @classmethod
    def _response_with_receipt(cls, connection, turn_id, response):
        receipts = cls._booking_receipts(connection, turn_id=turn_id)
        result = dict(response)
        if receipts:
            result["booking"] = receipts[0]
        elif result.get("booking") is not None:
            # A legacy response can describe a booking that never reached the
            # appointments table. Do not turn that text into a confirmation card.
            result["booking"] = None
        return result

    @staticmethod
    def _history(connection, session_id, limit=None):
        sql = "SELECT turn_id, message, response FROM turns WHERE session_id = ? ORDER BY turn_id DESC"
        parameters = (session_id,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters += (limit,)
        rows = connection.execute(sql, parameters).fetchall()
        return [{**Store._response_with_receipt(connection, row["turn_id"], json.loads(row["response"])),
                 "message": row["message"]} for row in reversed(rows)]

    def history(self, user_id, session_id):
        with self._transaction() as connection:
            self._owned_session(connection, user_id, session_id)
            return self._history(connection, session_id)

    def preference_history(self, user_id, key):
        with self._transaction() as connection:
            rows = connection.execute("""
                SELECT t.session_id, t.turn_id, t.message, c.name, c.value
                FROM preference_changes c JOIN turns t ON t.turn_id = c.turn_id
                JOIN sessions s ON s.session_id = t.session_id
                WHERE s.user_id = ? AND c.name = ? ORDER BY c.turn_id DESC
            """, (user_id, key))
            return [
                {"session_id": row["session_id"], "turn_id": row["turn_id"], "message": row["message"],
                 "key": row["name"], "value": json.loads(row["value"])} for row in rows
            ]

    @staticmethod
    def _scope(user_id, session_id, scope):
        """Filter owned sessions; the last visit is the most recently active other chat."""
        criteria = ["s.user_id = ?"]
        args = [user_id]
        if scope == "current":
            criteria.append("s.session_id = ?")
            args.append(session_id)
        elif scope == "previous":
            criteria.append("s.session_id != ?")
            args.append(session_id)
        elif scope == "last_visit":
            criteria.append("""s.session_id = (
                SELECT p.session_id FROM sessions p JOIN turns pt ON pt.session_id = p.session_id
                WHERE p.user_id = ? AND p.session_id != ?
                GROUP BY p.session_id ORDER BY max(pt.turn_id) DESC LIMIT 1
            )""")
            args.extend((user_id, session_id))
        elif scope != "all":
            raise ValueError(f"Unsupported visit scope: {scope}")
        return criteria, args

    @classmethod
    def _inquiries(cls, connection, user_id, session_id, scope, kind, limit=None, with_results=False):
        criteria, args = cls._scope(user_id, session_id, scope)
        if kind is not None:
            kinds = (kind,) if isinstance(kind, str) else tuple(kind)
            criteria.append("json_extract(e.event, '$.kind') IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        if with_results:
            criteria.append("json_array_length(e.event, '$.shown_ids') > 0")
        query = """
            SELECT t.session_id, t.message, e.event FROM turn_events e
            JOIN turns t ON t.turn_id = e.turn_id JOIN sessions s ON s.session_id = t.session_id
            WHERE """ + " AND ".join(criteria) + " ORDER BY t.turn_id DESC, e.ordinal DESC"
        if limit is not None:
            query += " LIMIT ?"
            args.append(limit)
        rows = connection.execute(query, args)
        return [
            {**json.loads(row["event"]), "message": row["message"], "session_id": row["session_id"]}
            for row in rows
        ]

    def inquiries(self, user_id, session_id, *, scope="all", kind=None):
        with self._transaction() as connection:
            self._owned_session(connection, user_id, session_id)
            return self._inquiries(connection, user_id, session_id, scope, kind)

    def stated_preferences(self, user_id, session_id, *, scope):
        criteria, args = self._scope(user_id, session_id, scope)
        with self._transaction() as connection:
            self._owned_session(connection, user_id, session_id)
            changes = connection.execute("""
                SELECT c.name, c.value FROM preference_changes c
                JOIN turns t ON t.turn_id = c.turn_id JOIN sessions s ON s.session_id = t.session_id
                WHERE """ + " AND ".join(criteria) + " ORDER BY t.turn_id", args)
            latest = {row["name"]: json.loads(row["value"]) for row in changes}
            return {key: value for key, value in latest.items() if value is not None}

    def state(self, user_id, session_id):
        with self._transaction() as connection:
            session = dict(self._owned_session(connection, user_id, session_id))
            session["shown_ids"] = json.loads(session["shown_ids"])
            session["pending_booking"] = json.loads(session["pending_booking"]) if session["pending_booking"] else None
            session["shopping_context"] = (json.loads(session["shopping_context"])
                                           if session["shopping_context"] else {"subject": "self"})
            session["preferences"] = {
                row["name"]: json.loads(row["value"]) for row in connection.execute(
                    "SELECT name, value FROM preferences WHERE user_id = ?", (user_id,)
                )
            }
            session["bookings"] = self._booking_receipts(connection, user_id=user_id)
            session["recent_turns"] = self._history(connection, session_id, limit=24)
            # The cursor belongs to the committed search event, not the model's
            # rolling message window. A detail question must not lose its place.
            searches = self._inquiries(connection, user_id, session_id, "current", "search", limit=1)
            session["active_search"] = searches[0] if searches else None
            retrieved = self._inquiries(connection, user_id, session_id, "current",
                                        ("search", "retrieval"), limit=1)
            session["retrieved_search"] = retrieved[0] if retrieved else None
            results = self._inquiries(connection, user_id, session_id, "current", "search",
                                     limit=1, with_results=True)
            session["last_results"] = results[0] if results else None
            lead = connection.execute("""
                SELECT budget_min_aed, budget_max_aed, needs, criteria, updated_at FROM leads WHERE session_id = ?
            """, (session_id,)).fetchone()
            session["lead"] = dict(lead) if lead is not None else {}
            if lead is not None:
                session["lead"]["criteria"] = json.loads(lead["criteria"])
            session["prior_inquiries"] = self._inquiries(connection, user_id, session_id, "previous", None, limit=12)
            return session

    @staticmethod
    def _saved_response(connection, session_id, request_id, message):
        row = connection.execute(
            "SELECT turn_id, message, response FROM turns WHERE session_id = ? AND request_id = ?",
            (session_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if message is not None and row["message"] != message:
            raise RequestConflict("This request ID already belongs to a different message.")
        return Store._response_with_receipt(connection, row["turn_id"], json.loads(row["response"]))

    def replay(self, user_id, session_id, request_id, message=None):
        with self._transaction() as connection:
            self._owned_session(connection, user_id, session_id)
            response = self._saved_response(connection, session_id, request_id, message)
        if response is not None:
            self._export_leads()
        return response

    def commit_turn(
        self, user_id, session_id, request_id, version, message, response, *,
        shown_ids=None, selected_id=_UNSET, preferences=None, inquiry=None, booking=None, lead=None,
        pending_booking=_UNSET, inquiries=None, shopping_context=_UNSET,
    ):
        """Atomically finalize a turn or return the response from its earlier successful attempt."""
        with self._transaction(write=True) as connection:
            session = self._owned_session(connection, user_id, session_id)
            stored = self._saved_response(connection, session_id, request_id, message)
            if stored is not None:
                response = stored
            else:
                if version != session["version"]:
                    raise StaleSession("A newer turn changed this session. Reload its state.")
                events = inquiries if inquiries is not None else ([inquiry] if inquiry is not None else [])
                if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                    raise ValueError("Inquiries must be a list of event objects.")
                # The ordered list is authoritative. Keep one legacy summary for
                # older readers, but an explicitly empty list must stay empty.
                summary = (inquiry or events[-1]) if events else None
                turn = connection.execute("""
                    INSERT INTO turns(session_id, request_id, message, response, inquiry)
                    VALUES (?, ?, ?, ?, ?)
                """, (session_id, request_id, message, _encode(response),
                      _encode(summary) if summary is not None else None))
                connection.executemany(
                    "INSERT INTO turn_events(turn_id, ordinal, event) VALUES (?, ?, ?)",
                    [(turn.lastrowid, ordinal, _encode(event)) for ordinal, event in enumerate(events)],
                )
                if booking is not None:
                    self._book(connection, turn.lastrowid, booking)
                response = self._response_with_receipt(connection, turn.lastrowid, response)
                connection.execute("UPDATE turns SET response = ? WHERE turn_id = ?",
                                   (_encode(response), turn.lastrowid))
                for key, value in (preferences or {}).items():
                    connection.execute(
                        "INSERT INTO preference_changes(turn_id, name, value) VALUES (?, ?, ?)",
                        (turn.lastrowid, key, _encode(value)),
                    )
                    if value is None:
                        connection.execute("DELETE FROM preferences WHERE user_id = ? AND name = ?", (user_id, key))
                    else:
                        connection.execute("""
                            INSERT INTO preferences(user_id, name, value, source_turn) VALUES (?, ?, ?, ?)
                            ON CONFLICT(user_id, name) DO UPDATE SET value = excluded.value,
                                source_turn = excluded.source_turn
                        """, (user_id, key, _encode(value), turn.lastrowid))
                if lead is not None:
                    self._write_lead(connection, session_id, lead)
                if selected_id is _UNSET:
                    selected_id = session["selected_id"] if shown_ids is None else None
                if pending_booking is _UNSET:
                    pending_json = session["pending_booking"]
                else:
                    if pending_booking is not None and not isinstance(pending_booking, dict):
                        raise ValueError("Pending booking must be an object or None.")
                    pending_json = _encode(pending_booking) if pending_booking is not None else None
                if shopping_context is _UNSET:
                    shopping_json = session["shopping_context"]
                else:
                    if (not isinstance(shopping_context, dict)
                            or shopping_context.get("subject") not in {"self", "other"}):
                        raise ValueError("Shopping context must identify subject 'self' or 'other'.")
                    shopping_json = _encode(shopping_context)
                connection.execute("""
                    UPDATE sessions SET version = version + 1, shown_ids = ?, selected_id = ?,
                        pending_booking = ?, shopping_context = ?, updated_at = ?
                    WHERE session_id = ?
                """, (session["shown_ids"] if shown_ids is None else _encode(shown_ids), selected_id,
                      pending_json, shopping_json, datetime.now(timezone.utc).isoformat(timespec="microseconds"), session_id))
        self._export_leads()
        return response

    @staticmethod
    def _require_inventory_listing(connection, listing_id):
        # Managed status is absent from the dataset. Every existing listing is
        # bookable in this simulation, including future inventory additions.
        if connection.execute("SELECT 1 FROM listings WHERE listing_id = ?", (listing_id,)).fetchone() is None:
            raise BookingError("This listing was not found in the inventory.")

    @staticmethod
    def _book(connection, turn_id, booking):
        listing_id = booking.get("listing_id")
        Store._require_inventory_listing(connection, listing_id)
        starts_at = validate_slot(listing_id, booking.get("starts_at"))
        kind = booking.get("kind", "viewing")
        if kind not in {"viewing", "test_drive"}:
            raise BookingError("Choose a viewing or a test drive.")
        try:
            connection.execute(
                "INSERT INTO appointments(listing_id, starts_at, kind, turn_id) VALUES (?, ?, ?, ?)",
                (listing_id, starts_at, kind, turn_id),
            )
        except sqlite3.IntegrityError as exc:
            raise BookingError("That slot is already booked for this car. Choose another slot.") from exc

    def available_slots(self, listing_id, day):
        with self._transaction() as connection:
            self._require_inventory_listing(connection, listing_id)
            try:
                day = date.fromisoformat(day) if isinstance(day, str) else day
                start = datetime.combine(day, time(8), tzinfo=DUBAI)
            except (ValueError, TypeError) as exc:
                raise BookingError("Provide a valid date for available slots.") from exc
            if start.weekday() == 6:
                return []
            booked = {row[0] for row in connection.execute(
                "SELECT starts_at FROM appointments WHERE listing_id = ? AND starts_at >= ? AND starts_at < ?",
                (listing_id, start.isoformat(), (start + timedelta(hours=12)).isoformat()),
            )}
        now = datetime.now(DUBAI)
        slots = (start + timedelta(minutes=30 * n) for n in range(24))
        return [slot.isoformat() for slot in slots if slot > now and slot.isoformat() not in booked]

    @staticmethod
    def _write_lead(connection, session_id, lead):
        prior = connection.execute("""
            SELECT budget_min_aed, budget_max_aed, needs, criteria FROM leads WHERE session_id = ?
        """, (session_id,)).fetchone()
        fields = dict(prior) if prior else {"budget_min_aed": None, "budget_max_aed": None, "needs": None, "criteria": "{}"}
        fields.update({name: lead[name] for name in ("budget_min_aed", "budget_max_aed", "needs") if name in lead})
        if "criteria" in lead:
            fields["criteria"] = _encode(lead["criteria"] or {})
        needs = fields["needs"]
        if isinstance(needs, list):
            needs = ", ".join(str(item) for item in needs)
        connection.execute("""
            INSERT INTO leads(session_id, budget_min_aed, budget_max_aed, needs, criteria, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                budget_min_aed = excluded.budget_min_aed, budget_max_aed = excluded.budget_max_aed,
                needs = excluded.needs, criteria = excluded.criteria, updated_at = excluded.updated_at
        """, (session_id, fields["budget_min_aed"], fields["budget_max_aed"], needs, fields["criteria"],
              datetime.now(timezone.utc).isoformat(timespec="seconds")))
        connection.execute("UPDATE csv_export SET dirty = 1 WHERE id = 1")

    def _export_leads(self):
        # Materialize from committed database rows. Holding the SQLite write lock over
        # replacement prevents a slower writer from replacing a newer CSV snapshot.
        with self._transaction(write=True) as connection:
            dirty = connection.execute("SELECT dirty FROM csv_export WHERE id = 1").fetchone()[0]
            if not dirty and self.leads_path.is_file():
                return
            rows = connection.execute("""
                SELECT s.user_id, l.session_id, l.budget_min_aed, l.budget_max_aed, l.needs, l.updated_at
                FROM leads l JOIN sessions s ON s.session_id = l.session_id ORDER BY l.session_id
            """)
            columns = [column[0] for column in rows.description]
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=self.leads_path.parent, newline="",
                                                 encoding="utf-8", delete=False) as temp:
                    temp_path = Path(temp.name)
                    writer = csv.writer(temp)
                    writer.writerow(columns)
                    writer.writerows(rows)
                os.replace(temp_path, self.leads_path)
                connection.execute("UPDATE csv_export SET dirty = 0 WHERE id = 1")
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
