"""HTTP boundary for the local, single-machine car shopping prototype."""

from contextlib import asynccontextmanager, closing
from pathlib import Path
import os
import sqlite3
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from assistant_service import Assistant, car_card
from inventory import DEFAULT_WORKBOOK, get_listing, init_inventory, search_inventory
from planner import GeminiPlanner, ModelUnavailable
from storage import BookingError, RequestConflict, SessionNotFound, StaleSession, Store


PROJECT = Path(__file__).resolve().parent
load_dotenv(os.getenv("CAR_ASSISTANT_ENV_FILE") or PROJECT / ".env")


class Shopper(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    user_id: str = Field(min_length=1, max_length=100)


class Message(Shopper):
    message: str = Field(min_length=1, max_length=2400)
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=100)


class BookingConfirmation(Shopper):
    request_id: str = Field(min_length=1, max_length=100)
    proposal_id: str = Field(min_length=1, max_length=100)


def create_app(data_dir=None, workbook_path=None, planner=None):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(data_dir or os.getenv("CAR_ASSISTANT_DATA_DIR") or PROJECT / "runtime")
        with closing(sqlite3.connect(store.db_path)) as conn:
            init_inventory(conn, workbook_path or DEFAULT_WORKBOOK)
        app.state.store = store
        app.state.assistant = Assistant(store, planner or GeminiPlanner())
        yield

    app = FastAPI(title="Car Finder", version="1.0", lifespan=lifespan)

    @app.exception_handler(ModelUnavailable)
    async def model_error(request: Request, exc: ModelUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(SessionNotFound)
    async def unknown_session(request: Request, exc: SessionNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(StaleSession)
    @app.exception_handler(RequestConflict)
    @app.exception_handler(BookingError)
    async def conflict(request: Request, exc: Exception):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        with closing(sqlite3.connect(app.state.store.db_path)) as conn:
            return {"status": "ok", "listings": conn.execute("SELECT count(*) FROM listings").fetchone()[0]}

    @app.post("/sessions", status_code=201)
    def create_session(shopper: Shopper):
        state = app.state.store.create_session(shopper.user_id)
        return {"session_id": state["session_id"], "user_id": state["user_id"]}

    @app.get("/sessions")
    def list_sessions(user_id: str = Query(min_length=1, max_length=100)):
        return app.state.store.list_sessions(user_id)

    @app.get("/bookings")
    def list_bookings(user_id: str = Query(min_length=1, max_length=100)):
        return app.state.store.list_bookings(user_id)

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str, user_id: str = Query(min_length=1, max_length=100)):
        return {"session_id": session_id, "user_id": user_id,
                "turns": app.state.store.history(user_id, session_id),
                "booking_proposal": app.state.assistant.current_booking_proposal(user_id, session_id)}

    @app.post("/sessions/{session_id}/messages")
    def chat(session_id: str, turn: Message):
        return app.state.assistant.respond(turn.user_id, session_id, turn.request_id, turn.message)

    @app.post("/sessions/{session_id}/booking-confirmation")
    def confirm_booking(session_id: str, confirmation: BookingConfirmation):
        return app.state.assistant.confirm_booking(
            confirmation.user_id, session_id, confirmation.request_id, confirmation.proposal_id)

    @app.get("/inventory")
    def inventory(make: str | None = None, model: str | None = None, trim: str | None = None,
                  color: str | None = None, body_style: str | None = None,
                  min_year: int | None = None, max_year: int | None = None,
                  min_price: int | None = Query(None, ge=0), max_price: int | None = Query(None, ge=0),
                  min_mileage: int | None = Query(None, ge=0), max_mileage: int | None = Query(None, ge=0),
                  query: str | None = Query(None, max_length=140), limit: int = Query(10, ge=1, le=100)):
        filters = dict(make=make, model=model, trim=trim, color=color, body_style=body_style,
                      min_year=min_year, max_year=max_year, min_price=min_price, max_price=max_price,
                      min_mileage=min_mileage, max_mileage=max_mileage, query=query, limit=limit)
        with closing(sqlite3.connect(app.state.store.db_path)) as conn:
            try:
                return [car_card(r) for r in search_inventory(conn, **filters)]
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

    @app.get("/inventory/{listing_id}")
    def listing(listing_id: int):
        with closing(sqlite3.connect(app.state.store.db_path)) as conn:
            row = get_listing(conn, listing_id)
        if not row:
            raise HTTPException(404, "Listing not found.")
        return {**row, **car_card(row)}

    return app


app = create_app()
