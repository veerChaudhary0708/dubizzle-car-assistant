"""Shared public-boundary helpers for prepare-and-confirm booking tests."""

from contextlib import closing
import sqlite3
from uuid import uuid4

from test_agent_workspace import send
from test_storage import next_monday


def appointment_rows(store):
    with closing(sqlite3.connect(store.db_path)) as connection:
        return connection.execute("SELECT listing_id,starts_at FROM appointments ORDER BY starts_at").fetchall()


def prepare(workspace, *, listing=20, day=None, clock="10:00", kind="test_drive", message=None):
    _, planner, _, _ = workspace
    day = day or next_monday().isoformat()
    planner.queue(("prepare_booking", {"car": {"listing_id": listing}, "day": day, "time": clock, "kind": kind}))
    result = send(workspace, message or f"Prepare a {kind.replace('_', ' ')} for listing {listing} on {day} at {clock}.")
    assert result["booking"] is None
    return result


def confirm(workspace, proposal, request_id=None, user_id="shopper"):
    _, _, assistant, sid = workspace
    proposal_id = proposal["proposal_id"] if isinstance(proposal, dict) else proposal
    return assistant.confirm_booking(user_id, sid, request_id or uuid4().hex, proposal_id)
