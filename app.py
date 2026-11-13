"""Streamlit client for the car assistant's FastAPI service."""

import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from uuid import uuid4

import requests
import streamlit as st


API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

STYLE = """
<style>
.stApp .block-container { max-width: 58rem; padding-top: 2.5rem; }
.stApp h1 { letter-spacing: -.04em; border-top: 4px solid #9c3e2f; padding-top: 1rem; }
[data-testid="stChatMessage"] { border: 1px solid #d9ded9; border-radius: .75rem; }
[data-testid="stChatMessage"] p { overflow-wrap: anywhere; }
.car-listing {
    margin-top: .5rem; margin-bottom: .35rem; background: #fff; color: #202d32; border: 1px solid #d6dad5;
    border-left: 3px solid #9c3e2f; border-radius: .45rem; padding: .9rem 1rem;
}
.car-listing .ref { margin: 0; font-size: .78rem; color: #536167; }
.car-listing h3 { color: #202d32; padding: .2rem 0 .75rem; margin: 0; font-size: 1.14rem; }
.car-listing dl { display: grid; grid-template-columns: repeat(3, 1fr); gap: .7rem; margin: 0; }
.car-listing dt { font-size: .78rem; color: #536167; }
.car-listing dd { margin: 0; font-weight: 600; overflow-wrap: anywhere; }
@media (max-width: 540px) {
    .stApp .block-container { padding-top: 2rem; }
    .car-listing dl { grid-template-columns: repeat(2, 1fr); }
}
</style>
"""


def api(method, path, **kwargs):
    response = requests.request(method, API_BASE + path, timeout=(4, 240), **kwargs)
    response.raise_for_status()
    return response.json()


def error_message(exc):
    if isinstance(exc, requests.Timeout):
        return "The service took too long to respond."
    if isinstance(exc, requests.ConnectionError):
        return "Could not reach the car service. Check that the FastAPI server is running."
    if exc.response is not None:
        status = exc.response.status_code
        if status in (409, 503):
            # FastAPI returns shopper-safe explanations for conflicts and provider failures.
            try:
                payload = exc.response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
                return payload["detail"]
        return f"The car service returned HTTP {status}."
    return "The car service returned an unreadable response."


def sync_location():
    """Keep this local demo identity and chat available after a browser refresh."""
    shopper = st.session_state.get("shopper", "").strip()
    session = st.session_state.get("session_id")
    for key, value in (("shopper", shopper), ("chat", session)):
        if value:
            if st.query_params.get(key) != value:
                st.query_params[key] = value
        elif key in st.query_params:
            del st.query_params[key]


def reset_chat():
    st.session_state.session_id = None
    st.session_state.pending = None
    st.session_state.send_error = None
    st.session_state.pending_confirmation = None
    st.session_state.confirmation_error = None
    sync_location()


def resume_chat(session_id):
    st.session_state.session_id = session_id
    st.session_state.pending = None
    st.session_state.send_error = None
    st.session_state.pending_confirmation = None
    st.session_state.confirmation_error = None
    sync_location()


def chat_text(text):
    # Markdown makes conversational lists readable; arbitrary HTML stays disabled.
    # Do not load remote images supplied in a message or in generated prose.
    text = str(text).replace("![", "[")
    st.markdown(text, unsafe_allow_html=False)


def listing_text(text):
    """Seller source text is displayed literally, separate from assistant Markdown."""
    lines = "<br>".join(escape(str(text)).splitlines())
    st.markdown(
        f'<div style="white-space:pre-wrap;overflow-wrap:anywhere">{lines}</div>',
        unsafe_allow_html=True,
    )


def vehicle_label(value):
    label = str(value).strip().title()
    acronyms = r"\b(?:bmw|gmc|mg|mini|amg|gcc|usa|uk|suv|awd|fwd|rwd|ev|gti|gts|gt|xlt|ltz|sxt|slt|gli|tsi|tdi|se|le|lx|ex|v\d{1,2})\b"
    return re.sub(acronyms, lambda match: match.group().upper(), label, flags=re.I)


def present_car(car):
    name = " ".join(vehicle_label(car[k]) for k in ("year", "make", "model", "trim") if car.get(k))
    price = car.get("price_aed")
    mileage = car.get("mileage_km")
    price_text = f"AED {price:,.0f}" if price is not None else "Not verified"
    mileage_text = f"{mileage:,.0f} km" if mileage is not None else "Not verified"
    title = name or car.get("title") or "Car listing"
    st.markdown(
        f'<article class="car-listing"><p class="ref">Listing {escape(str(car["listing_id"]))}</p>'
        f'<h3>{escape(title)}</h3><dl>'
        f'<div><dt>Asking price</dt><dd>{price_text}</dd></div>'
        f'<div><dt>Mileage</dt><dd>{mileage_text}</dd></div>'
        '<div><dt>Demo viewing</dt><dd>Enabled</dd></div></dl></article>',
        unsafe_allow_html=True,
    )
    if car.get("title") or car.get("description"):
        with st.expander("Listing details"):
            if car.get("title"):
                listing_text(car["title"])
            if car.get("description"):
                listing_text(car["description"])


def confirmed_booking(booking):
    """Only the API's committed receipt can produce a confirmation card."""
    return (isinstance(booking, dict) and booking.get("status") == "confirmed"
            and bool(booking.get("booking_id")))


def booking_time(booking):
    date_string = booking.get("starts_at") or "Time not provided"
    try:
        date = datetime.fromisoformat(date_string)
        if date.tzinfo is not None:
            date = date.astimezone(timezone(timedelta(hours=4)))
        date_string = date.strftime("%a, %d %b %Y at %I:%M %p") + " Dubai time (UTC+04:00)"
    except (TypeError, ValueError):
        pass
    return date_string


def present_booking(booking):
    if not confirmed_booking(booking):
        return
    appointment = "Test drive" if booking.get("kind") == "test_drive" else "Viewing"
    with st.container(border=True):
        st.success(f"Simulated {appointment.lower()} confirmed · Listing {booking['listing_id']}")
        st.text(booking.get("car_label") or f"Listing {booking['listing_id']}")
        st.markdown(f"**{booking_time(booking)}**")
        st.caption(f"Booking reference: {booking['booking_id']}")
        st.caption("Demo reservation only. No dealership has been contacted.")


def present_booking_status(turn):
    """Distinguish normal booking progress from an unsuccessful reservation."""
    if confirmed_booking(turn.get("booking")):
        present_booking(turn["booking"])
        return
    status = (turn.get("booking_outcome") or {}).get("status")
    if status in {"offer", "awaiting_confirmation"}:
        st.info("Awaiting your confirmation — no appointment reserved yet.")
    elif status in {"pending", "collecting"}:
        st.info("Booking details needed — no appointment reserved yet.")
    elif status == "availability":
        st.caption("Availability check only — no appointment reserved.")
    elif status == "cancelled":
        st.caption("Unfinished booking request cancelled — no appointment reserved by this message.")
    elif (status in {"prepared", "confirmed", "rejected", "not_executed"}
          or any(step.get("tool") == "book_viewing" for step in turn.get("tool_trace", []))):
        st.warning("No booking was saved for this message.")


def review_booking(user, proposal):
    """Only the current server proposal gets an actionable confirmation button."""
    state = st.session_state
    if (not isinstance(proposal, dict) or proposal.get("status") != "awaiting_confirmation"
            or proposal.get("session_id") != state.session_id
            or not all(proposal.get(key) for key in ("proposal_id", "listing_id", "starts_at", "kind"))):
        return
    appointment = "Test drive" if proposal["kind"] == "test_drive" else "Viewing"
    with st.container(border=True):
        st.subheader("Review booking")
        st.text(proposal.get("car_label") or f"Listing {proposal['listing_id']}")
        st.markdown(f"**{appointment}** · Listing {proposal['listing_id']}")
        st.markdown(f"**{booking_time(proposal)}**")
        st.caption("Not reserved yet. Confirm once to save this demo appointment.")
        if st.button("Confirm booking", key=f"confirm_booking_{proposal['proposal_id']}",
                     type="primary", disabled=bool(state.pending or state.pending_confirmation)):
            state.pending_confirmation = {"session_id": state.session_id,
                                          "proposal_id": proposal["proposal_id"],
                                          "request_id": str(uuid4())}
            deliver_confirmation(user)


def deliver_confirmation(user):
    state = st.session_state
    confirmation = state.pending_confirmation
    try:
        with st.spinner("Saving your demo appointment…"):
            api("POST", f"/sessions/{confirmation['session_id']}/booking-confirmation",
                json={"user_id": user, "request_id": confirmation["request_id"],
                      "proposal_id": confirmation["proposal_id"]})
        state.pending_confirmation = None
        state.confirmation_error = None
    except requests.RequestException as exc:
        state.confirmation_error = error_message(exc)
    st.rerun()


def your_bookings(user):
    """Show this shopper's committed appointments independently of the open chat."""
    try:
        bookings = [booking for booking in api("GET", "/bookings", params={"user_id": user})
                    if confirmed_booking(booking)]
    except requests.RequestException:
        with st.expander("Your bookings", expanded=True):
            st.warning("Your bookings couldn't be loaded. Refresh to check your saved reservations.")
            if st.button("Refresh bookings"):
                st.rerun()
        return
    with st.expander(f"Your bookings ({len(bookings)})", expanded=bool(bookings)):
        if bookings:
            for booking in bookings:
                present_booking(booking)
        else:
            st.caption("No confirmed demo bookings for this shopper yet.")
        if st.button("Refresh bookings"):
            st.rerun()


def deliver(user):
    state = st.session_state
    try:
        with st.spinner("Checking with the car assistant…"):
            if state.session_id is None:
                session = api("POST", "/sessions", json={"user_id": user})
                state.session_id = session["session_id"]
                sync_location()
            api(
                "POST", f"/sessions/{state.session_id}/messages",
                json={"user_id": user, **state.pending},
            )
        state.pending = None
        state.send_error = None
    except requests.RequestException as exc:
        state.send_error = error_message(exc)
    st.rerun()


def saved_chats(user):
    """History belongs to the explicit shopper ID; opening it never creates a turn."""
    st.subheader("Saved chats")
    try:
        sessions = api("GET", "/sessions", params={"user_id": user})
    except requests.RequestException:
        st.caption("Saved chats couldn't be loaded. You can retry by refreshing the page.")
        return
    if not sessions:
        st.caption("Your conversations will appear here after the first message.")
        return
    with st.container(height=300, border=False):
        for session in sessions:
            session_id = session["session_id"]
            title = " ".join((session.get("title") or "Conversation").split())
            label = title if len(title) <= 60 else title[:57] + "…"
            updated = session.get("updated_at") or session.get("created_at")
            help_text = title
            if updated:
                try:
                    help_text += " · " + datetime.fromisoformat(updated.replace("Z", "+00:00")).strftime("%d %b %Y")
                except (TypeError, ValueError):
                    pass
            st.button(label, key=f"saved_{session_id}", help=help_text,
                      type="primary" if session_id == st.session_state.session_id else "secondary",
                      use_container_width=True, on_click=resume_chat, args=(session_id,),
                      disabled=bool(st.session_state.pending or st.session_state.pending_confirmation))


def main():
    st.set_page_config(page_title="Car finder", page_icon="🚗", layout="centered")
    st.markdown(STYLE, unsafe_allow_html=True)
    state = st.session_state
    if "shopper" not in state:
        shopper = str(st.query_params.get("shopper", "")).strip()
        valid_shopper = 0 < len(shopper) <= 100
        state.shopper = shopper if valid_shopper else f"shopper-{uuid4().hex[:8]}"
        session_id = str(st.query_params.get("chat", ""))
        state.session_id = session_id if valid_shopper and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id) else None
        state.pending = None
        state.send_error = None
        sync_location()
    state.setdefault("pending_confirmation", None)
    state.setdefault("confirmation_error", None)

    with st.sidebar:
        st.header("Your chats")
        user = st.text_input("Shopper ID", key="shopper", max_chars=100, on_change=reset_chat).strip()
        st.caption("Use the same ID next time to recall your preferences.")
        st.button("New chat", on_click=reset_chat, use_container_width=True)
        if user:
            saved_chats(user)
        st.caption("Local demo · Chats are linked to the shopper ID.")

    st.title("Used car finder")
    st.caption("Explore the inventory, understand the details, and arrange a demo viewing.")
    st.caption("Demo bookings are available for all listed cars; actual managed status is not provided.")
    if not user:
        st.info("Enter a shopper ID in the sidebar to start a chat.")
        return

    your_bookings(user)

    turns = []
    proposal = None
    if state.session_id:
        try:
            session = api("GET", f"/sessions/{state.session_id}", params={"user_id": user})
            turns = session["turns"]
            proposal = session.get("booking_proposal")
        except requests.RequestException as exc:
            if exc.response is not None and exc.response.status_code == 404:
                st.warning("This chat isn't available for this shopper. Choose a saved chat or start a new one.")
            else:
                st.error(error_message(exc))
            if st.button("Reload chat"):
                st.rerun()
            return

    if state.pending_confirmation and any(
            turn.get("request_id") == state.pending_confirmation["request_id"] for turn in turns):
        # A lost HTTP response may still have committed the exact confirmation.
        state.pending_confirmation = None
        state.confirmation_error = None
        st.rerun()

    for turn in turns:
        with st.chat_message("user"):
            chat_text(turn["message"])
        with st.chat_message("assistant"):
            chat_text(turn["reply"])
            for car in turn.get("cars", []):
                present_car(car)
            present_booking_status(turn)

    # Keep the current action beside the latest exchange. Placing it before the
    # transcript hides it many screens above the composer in a resumed chat.
    review_booking(user, proposal)

    if state.pending_confirmation:
        if state.confirmation_error:
            st.error(state.confirmation_error)
        retry, dismiss = st.columns(2)
        if retry.button("Retry confirmation"):
            deliver_confirmation(user)
        if dismiss.button("Dismiss confirmation"):
            state.pending_confirmation = None
            state.confirmation_error = None
            st.rerun()
        return

    if state.pending:
        # The POST can succeed even if its response times out. Reuse the same
        # request ID on retry so a booking or conversation turn is written once.
        if any(t.get("request_id") == state.pending["request_id"] for t in turns):
            state.pending = None
            state.send_error = None
            st.rerun()
        with st.chat_message("user"):
            chat_text(state.pending["message"])
        if state.send_error:
            st.error(state.send_error)
        retry, dismiss = st.columns(2)
        if retry.button("Retry message"):
            deliver(user)
        if dismiss.button("Dismiss"):
            state.pending = None
            state.send_error = None
            st.rerun()
        return

    prompt = None
    latest_page = next((turn.get("pagination") for turn in reversed(turns) if turn.get("pagination") is not None), None)
    if latest_page and latest_page.get("has_more"):
        if st.button("Show more cars", key="next_results", use_container_width=True):
            prompt = "Show me more"
    if not turns:
        st.write("Tell me what you're looking for, or start with one of these.")
        browse, search, recall = st.columns(3)
        if browse.button("Browse all cars", use_container_width=True):
            prompt = "What cars are available?"
        if search.button("Mustang under AED 25,000", use_container_width=True):
            prompt = "Find a Ford Mustang under AED 25,000."
        if recall.button("Recall my preferences", use_container_width=True):
            prompt = "What do you remember about my car preferences?"

    message = st.chat_input("Ask about a car or a viewing", max_chars=2400) or prompt
    if message and message.strip():
        state.pending = {"message": message.strip(), "request_id": str(uuid4())}
        with st.chat_message("user"):
            chat_text(state.pending["message"])
        deliver(user)


if __name__ == "__main__":
    main()
