from pathlib import Path
from unittest.mock import Mock

import requests
import pytest
from streamlit.testing.v1 import AppTest

from app import error_message


def test_client_shows_handled_api_error_detail():
    response = Mock()
    response.json.return_value = {"detail": "Try again after the quota resets."}
    exc = requests.HTTPError(response=response)
    response.status_code = 503
    assert error_message(exc) == "Try again after the quota resets."
    response.status_code = 500
    assert error_message(exc) == "The car service returned HTTP 500."


def test_timed_out_booking_retries_same_request_and_new_chat_keeps_shopper(monkeypatch):
    """A lost response must not turn the retry button into a second booking."""
    submissions = []
    turns = []

    def backend(method, url, **options):
        if url.endswith("/sessions") and method == "POST":
            payload = {"session_id": "first", "user_id": options["json"]["user_id"]}
        elif url.endswith("/sessions") and method == "GET":
            payload = []
        elif url.endswith("/bookings") and method == "GET":
            payload = [turn["booking"] for turn in turns]
        elif url.endswith("/sessions/first") and method == "GET":
            payload = {"turns": turns}
        else:
            assert method == "POST" and url.endswith("/sessions/first/messages")
            body = options["json"]
            submissions.append(body.copy())
            if len(submissions) == 1:
                raise requests.ReadTimeout()
            payload = {
                "reply": "Your viewing is confirmed.",
                "booking": {"listing_id": "L22", "starts_at": "2026-11-21T10:00:00+04:00",
                            "booking_id": "booking-1", "status": "confirmed", "kind": "viewing",
                            "car_label": "2014 Ford Mustang V6"},
                "cars": [{
                    "listing_id": "L22", "year": 2014, "make": "Ford", "model": "Mustang",
                    "price_aed": 22500, "mileage_km": None, "booking_eligible": True,
                }],
            }
            turns.append({**body, **payload})
        response = Mock()
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    client_file = Path(__file__).resolve().parents[1] / "app.py"
    view = AppTest.from_file(str(client_file)).run()
    shopper = view.sidebar.text_input[0].value
    view.chat_input[0].set_value("Book a viewing of listing L22.").run()
    assert not view.exception
    assert "too long" in view.error[0].value
    next(button for button in view.button if button.label == "Retry message").click().run()
    assert not view.exception
    assert len(submissions) == 2 and submissions[0] == submissions[1]
    assert "L22" in view.success[0].value
    card = next(item.value for item in view.markdown if '<article class="car-listing">' in item.value)
    assert all(text in card for text in ("2014 Ford Mustang", "AED 22,500", "Not verified", "Enabled"))
    view.sidebar.button[0].click().run()
    assert not view.exception
    assert view.sidebar.text_input[0].value == shopper
    assert not view.chat_message


def test_saved_chats_resume_across_refresh_and_shoppers_stay_separate(monkeypatch):
    """A fresh browser session can reopen history, while New chat clears its focus."""
    calls = []
    old_turn = {"message": "Find a Mustang.", "reply": "A **Ford Mustang V6** is available.",
                "cars": [{"listing_id": 20, "year": 2014, "make": "Ford", "model": "Mustang",
                          "booking_eligible": False}], "request_id": "earlier-message"}

    def backend(method, url, **options):
        calls.append((method, url, options))
        assert method == "GET", "Opening saved history must not send a new model turn."
        user = options["params"]["user_id"]
        if url.endswith("/bookings"):
            payload = []
        elif url.endswith("/sessions"):
            payload = ([{"session_id": "saved-chat", "user_id": user,
                         "title": "Find a Mustang.", "created_at": None, "updated_at": None}]
                       if user == "returning-shopper" else [])
        else:
            assert url.endswith("/sessions/saved-chat") and user == "returning-shopper"
            payload = {"turns": [old_turn]}
        response = Mock()
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    client_file = Path(__file__).resolve().parents[1] / "app.py"
    view = AppTest.from_file(str(client_file))
    view.query_params["shopper"] = "returning-shopper"
    view.run()
    assert not view.exception and not view.chat_message
    next(button for button in view.sidebar.button if button.label == "Find a Mustang.").click().run()
    assert not view.exception and len(view.chat_message) == 2
    assert view.query_params["chat"] == ["saved-chat"]
    # Legacy saved cards must follow the current all-inventory demo-booking rule.
    card = next(item.value for item in view.markdown if '<article class="car-listing">' in item.value)
    assert "<dt>Demo viewing</dt><dd>Enabled</dd>" in card and "Ineligible" not in card
    assert sum("actual managed status is not provided" in caption.value for caption in view.caption) == 1

    # A new Streamlit session (such as a browser refresh) is restored from its local URL.
    refreshed = AppTest.from_file(str(client_file))
    refreshed.query_params.update(view.query_params)
    refreshed.run()
    assert not refreshed.exception and len(refreshed.chat_message) == 2
    assert refreshed.sidebar.text_input[0].value == "returning-shopper"
    next(button for button in refreshed.sidebar.button if button.label == "New chat").click().run()
    assert not refreshed.chat_message and "chat" not in refreshed.query_params
    assert refreshed.sidebar.text_input[0].value == "returning-shopper"
    assert any(button.label == "Find a Mustang." for button in refreshed.sidebar.button)

    refreshed.sidebar.text_input[0].set_value("other-shopper").run()
    assert not refreshed.exception and not refreshed.chat_message
    assert all(button.label != "Find a Mustang." for button in refreshed.sidebar.button)
    assert calls[-1][2]["params"]["user_id"] == "other-shopper"


def test_show_more_button_uses_chat_pipeline_and_disappears_when_exhausted(monkeypatch):
    turns = [{"message": "Show me cars.", "reply": "First page", "request_id": "search",
              "pagination": {"start": 1, "end": 5, "total": 7, "has_more": True}, "cars": []}]
    submitted = []

    def backend(method, url, **options):
        if url.endswith(("/sessions", "/bookings")):
            assert method == "GET"
            payload = []
        elif method == "GET":
            payload = {"turns": turns}
        else:
            assert method == "POST" and url.endswith("/sessions/paged/messages")
            body = options["json"]
            submitted.append(body.copy())
            if len(submitted) == 1:
                raise requests.ReadTimeout()
            payload = {"reply": "Last page", "cars": [],
                       "pagination": {"start": 6, "end": 7, "total": 7, "has_more": False}}
            turns.append({**body, **payload})
        response = Mock()
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    view.query_params.update({"shopper": "browser", "chat": "paged"})
    view.run()
    next(button for button in view.button if button.label == "Show more cars").click().run()
    assert not view.exception
    assert "too long" in view.error[0].value
    assert all(button.label != "Show more cars" for button in view.button)
    next(button for button in view.button if button.label == "Retry message").click().run()
    assert not view.exception
    assert len(submitted) == 2 and submitted[0] == submitted[1]
    assert submitted[0]["message"] == "Show me more" and submitted[0]["request_id"]
    assert submitted[0]["user_id"] == "browser"
    assert all(button.label != "Show more cars" for button in view.button)
    assert len(view.chat_message) == 4


def test_markdown_answers_and_literal_listing_source_are_safe_and_readable(monkeypatch):
    turn = {"message": "Explain this car.",
            "reply": "**V6** means six cylinders.\n\n- General explanation.\n<script>alert(1)</script>\n![image](https://example.invalid/pixel)",
            "cars": [{"listing_id": 9, "year": 2019, "make": "bmw", "model": "x5", "trim": "v6",
                      "price_aed": 25000, "mileage_km": 120000, "booking_eligible": True,
                      "title": "Seller <b>title</b>", "description": "<img src=x onerror=alert(1)>"}]}

    def backend(method, url, **options):
        response = Mock()
        response.json.return_value = [] if url.endswith(("/sessions", "/bookings")) else {"turns": [turn]}
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    view.query_params.update({"shopper": "reader", "chat": "details"})
    view.run()
    assert not view.exception
    reply = next(item for item in view.markdown if item.value.startswith("**V6**"))
    assert not reply.proto.allow_html
    assert "![" not in reply.value
    assert "2019 BMW X5 V6" in next(item.value for item in view.markdown if "car-listing" in item.value and "<article" in item.value)
    assert any("&lt;img src=x onerror=alert(1)&gt;" in item.value for item in view.markdown)
    assert any(expander.label == "Listing details" for expander in view.expander)


def test_committed_booking_receipt_survives_new_chat_refresh_and_shopper_switch(monkeypatch):
    receipt = {"booking_id": "booking-28", "status": "confirmed", "listing_id": 20,
               "car_label": "2014 Ford Mustang V6", "kind": "test_drive",
               "starts_at": "2026-11-21T06:00:00+00:00", "session_id": "booked-chat"}
    calls = []

    def backend(method, url, **options):
        calls.append((method, url, options))
        assert method == "GET", "Browsing bookings must not make another reservation."
        user = options["params"]["user_id"]
        if url.endswith("/bookings"):
            payload = [receipt] if user == "booking-shopper" else []
        elif url.endswith("/sessions"):
            payload = []
        else:
            assert url.endswith("/sessions/booked-chat") and user == "booking-shopper"
            payload = {"turns": [{"message": "Book it for Saturday at 10am.",
                                  "reply": "Your simulated test drive is confirmed.", "booking": receipt,
                                  "booking_outcome": {"status": "prepared"}}]}
        response = Mock()
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    client_file = str(Path(__file__).resolve().parents[1] / "app.py")
    view = AppTest.from_file(client_file)
    view.query_params.update({"shopper": "booking-shopper", "chat": "booked-chat"})
    view.run()
    assert not view.exception
    assert len(view.success) == 2  # Saved bookings panel and original chat receipt.
    assert not view.info  # The committed receipt takes precedence over staged outcome metadata.
    assert all("Simulated test drive confirmed" in message.value for message in view.success)
    assert any(item.value == "2014 Ford Mustang V6" for item in view.text)
    assert any("21 Nov 2026 at 10:00 AM" in item.value and "Dubai time (UTC+04:00)" in item.value
               for item in view.markdown)
    assert sum("Booking reference: booking-28" in item.value for item in view.caption) == 2
    assert any("No dealership has been contacted" in item.value for item in view.caption)

    next(item for item in view.sidebar.button if item.label == "New chat").click().run()
    assert not view.exception and not view.chat_message
    assert len(view.success) == 1 and "Listing 20" in view.success[0].value
    assert any(item.label == "Your bookings (1)" for item in view.expander)

    refreshed = AppTest.from_file(client_file)
    refreshed.query_params.update(view.query_params)
    refreshed.run()
    assert not refreshed.exception and len(refreshed.success) == 1
    next(item for item in refreshed.button if item.label == "Refresh bookings").click().run()
    assert len(refreshed.success) == 1
    refreshed.sidebar.text_input[0].set_value("other-shopper").run()
    assert not refreshed.exception and not refreshed.success
    assert any(item.label == "Your bookings (0)" for item in refreshed.expander)
    assert calls[-1][1].endswith("/bookings") and calls[-1][2]["params"] == {"user_id": "other-shopper"}


def test_prose_only_confirmation_or_uncommitted_payload_never_becomes_receipt(monkeypatch):
    def backend(method, url, **options):
        response = Mock()
        if url.endswith(("/sessions", "/bookings")):
            payload = []
        else:
            payload = {"turns": [
                {"message": "Book the Mustang.", "reply": "Your test drive is confirmed for 21 Nov at 10am.",
                 "tool_trace": [{"tool": "book_viewing", "status": "needs_clarification"}]},
                {"message": "Did that work?", "reply": "Confirmed.",
                 "booking": {"listing_id": 20, "starts_at": "2026-11-21T10:00:00+04:00"}},
            ]}
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    view.query_params.update({"shopper": "reader", "chat": "legacy"})
    view.run()
    assert not view.exception and len(view.chat_message) == 4
    assert not view.success
    assert any(item.value == "No booking was saved for this message." for item in view.warning)
    assert any("No confirmed demo bookings" in item.value for item in view.caption)
    assert not any("Booking reference:" in item.value for item in view.caption)


@pytest.mark.parametrize("status,element,label", [
    ("awaiting_confirmation", "info", "Awaiting your confirmation — no appointment reserved yet."),
    ("collecting", "info", "Booking details needed — no appointment reserved yet."),
    ("offer", "info", "Awaiting your confirmation — no appointment reserved yet."),
    ("pending", "info", "Booking details needed — no appointment reserved yet."),
    ("rejected", "warning", "No booking was saved for this message."),
    ("not_executed", "warning", "No booking was saved for this message."),
    ("availability", "caption", "Availability check only — no appointment reserved."),
    ("cancelled", "caption", "Unfinished booking request cancelled — no appointment reserved by this message."),
])
def test_booking_progress_is_distinct_from_failed_reservation(monkeypatch, status, element, label):
    turn = {"message": "Continue the viewing request.", "reply": "Here is the current booking status.",
            "booking": None, "booking_outcome": {"status": status},
            "tool_trace": [{"tool": "book_viewing", "status": "needs_clarification"}]}

    def backend(method, url, **options):
        assert method == "GET"
        response = Mock()
        response.json.return_value = [] if url.endswith(("/sessions", "/bookings")) else {"turns": [turn]}
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    view.query_params.update({"shopper": "status-reader", "chat": "booking-progress"})
    view.run()
    assert not view.exception and not view.success
    assert any(item.value == label for item in getattr(view, element))
    if status in {"offer", "pending", "awaiting_confirmation", "collecting", "availability", "cancelled"}:
        assert not view.warning and not view.error


def test_bookings_loading_error_keeps_chat_usable_without_claiming_no_bookings(monkeypatch):
    def backend(method, url, **options):
        if url.endswith("/bookings"):
            raise requests.ConnectionError()
        response = Mock()
        response.json.return_value = []
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not view.exception and view.chat_input
    assert any("couldn't be loaded" in item.value for item in view.warning)
    assert not any("No confirmed demo bookings" in item.value for item in view.caption)
    assert any(item.label == "Refresh bookings" for item in view.button)


def test_receipt_from_real_store_and_api_reaches_client_without_model_call(monkeypatch, tmp_path):
    """Verify the persistence -> API -> Streamlit receipt contract as one path."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient

    from main import create_app

    backend_app = create_app(data_dir=tmp_path, planner=object())
    with TestClient(backend_app) as client:
        user = "receipt-owner"
        session = client.post("/sessions", json={"user_id": user}).json()["session_id"]
        start = (datetime.now(timezone(timedelta(hours=4))) + timedelta(days=7)).replace(
            hour=10, minute=0, second=0, microsecond=0)
        if start.weekday() == 6:
            start += timedelta(days=1)
        booking = {"listing_id": 20, "starts_at": start.isoformat(), "kind": "viewing"}
        backend_app.state.store.commit_turn(
            user, session, "committed-booking", 0, "Book the Mustang.",
            {"reply": "Your simulated viewing is confirmed.", "booking": booking}, booking=booking)
        receipt = client.get("/bookings", params={"user_id": user}).json()[0]
        assert receipt["booking_id"] and receipt["status"] == "confirmed"
        calls = []

        def actual_api(method, url, **options):
            calls.append((method, urlsplit(url).path))
            assert method == "GET", "Rendering receipts must never book again."
            return client.request(method, urlsplit(url).path, params=options.get("params"))

        monkeypatch.setattr(requests, "request", actual_api)
        view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        view.query_params.update({"shopper": user, "chat": session})
        view.run()
        assert not view.exception and len(view.success) == 2
        assert sum(receipt["booking_id"] in item.value for item in view.caption) == 2
        assert any("Mustang" in item.value for item in view.text)
        next(item for item in view.sidebar.button if item.label == "New chat").click().run()
        assert not view.exception and len(view.success) == 1 and not view.chat_message
        view.sidebar.text_input[0].set_value("another-receipt-owner").run()
        assert not view.exception and not view.success
        assert ("GET", "/bookings") in calls


@pytest.mark.parametrize("committed_before_timeout", [False, True])
def test_confirm_booking_uses_exact_proposal_and_reuses_request_after_timeout(monkeypatch, committed_before_timeout):
    proposal = {"status": "awaiting_confirmation", "proposal_id": "proposal-current", "listing_id": 20,
                "car_label": "2014 Ford Mustang V6", "kind": "test_drive",
                "starts_at": "2026-11-21T06:00:00+00:00", "session_id": "review-chat"}
    current = [proposal]
    turns = [{"message": "The Mustang on Saturday at10am.", "reply": "Please review the appointment.",
              "booking_proposal": proposal, "booking": None}]
    bookings, submissions = [], []

    def backend(method, url, **options):
        if method == "GET":
            if url.endswith("/sessions"):
                payload = []
            elif url.endswith("/bookings"):
                payload = bookings
            else:
                assert url.endswith("/sessions/review-chat")
                payload = {"turns": turns, "booking_proposal": current[0]}
        else:
            assert url.endswith("/sessions/review-chat/booking-confirmation"), "Confirmation must bypass the chat/model route."
            body = options["json"]
            submissions.append(body.copy())
            assert body == {"user_id": "review-shopper", "proposal_id": "proposal-current",
                            "request_id": body["request_id"]}
            if committed_before_timeout or len(submissions) == 2:
                receipt = {**proposal, "booking_id": "booking-2", "status": "confirmed"}
                bookings.append(receipt)
                payload = {"request_id": body["request_id"], "message": "Confirm booking.",
                           "reply": "Your demo test drive is confirmed.", "booking": receipt}
                turns.append(payload)
                current[0] = None
            if len(submissions) == 1:
                raise requests.ReadTimeout()
        response = Mock()
        response.json.return_value = payload
        return response

    monkeypatch.setattr(requests, "request", backend)
    view = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
    view.query_params.update({"shopper": "review-shopper", "chat": "review-chat"})
    view.run()
    assert not view.exception and not view.success
    assert any(item.value == "Review booking" for item in view.subheader)
    assert any(item.value == "2014 Ford Mustang V6" for item in view.text)
    assert any("Test drive" in item.value for item in view.markdown)
    assert any("21 Nov 2026 at 10:00 AM" in item.value and "UTC+04:00" in item.value for item in view.markdown)
    assert any("Not reserved yet" in item.value for item in view.caption)
    assert not submissions, "Rendering a proposal must not reserve it."
    next(item for item in view.button if item.label == "Confirm booking").click().run()
    assert not view.exception
    if not committed_before_timeout:
        assert "too long" in view.error[0].value
        assert next(item for item in view.button if item.label == "Confirm booking").disabled
        next(item for item in view.button if item.label == "Retry confirmation").click().run()
        assert submissions[0] == submissions[1]
    assert not view.exception and not view.error
    assert len(bookings) == 1
    assert len(submissions) == (1 if committed_before_timeout else 2)
    assert view.session_state["pending_confirmation"] is None
    view.run()  # Inspect the completed read-only render after the callback's rerun.
    assert not view.exception and len(submissions) == (1 if committed_before_timeout else 2)
    assert not any(item.label in {"Confirm booking", "Retry confirmation"} for item in view.button)
    assert len(view.success) == 2  # Current saved bookings and committed history receipt.


def test_only_latest_current_proposal_has_confirm_button_after_questions_and_refresh(monkeypatch):
    old = {"status": "awaiting_confirmation", "proposal_id": "old-proposal", "listing_id": 20,
           "car_label": "2014 Ford Mustang", "kind": "viewing", "starts_at": "2026-11-21T10:00:00+04:00",
           "session_id": "review-chat"}
    current = [{**old, "proposal_id": "current-proposal", "starts_at": "2026-11-21T11:00:00+04:00"}]
    turns = [{"message": "Book the Mustang.", "reply": "Original proposal.", "booking_proposal": old},
             *[{"message": f"Earlier question {i}", "reply": "Earlier answer. " * 40} for i in range(30)],
             {"message": "What's its mileage?", "reply": "164,000 km."}]
    calls = []

    def backend(method, url, **options):
        calls.append((method, url))
        assert method == "GET", "Reading a current proposal must not call the model or reserve anything."
        response = Mock()
        response.json.return_value = [] if url.endswith(("/sessions", "/bookings")) else {
            "turns": turns, "booking_proposal": current[0]}
        return response

    monkeypatch.setattr(requests, "request", backend)
    file = str(Path(__file__).resolve().parents[1] / "app.py")
    view = AppTest.from_file(file)
    view.query_params.update({"shopper": "reader", "chat": "review-chat"})
    view.run()
    assert not view.exception
    confirms = [item for item in view.button if item.label == "Confirm booking"]
    assert len(confirms) == 1 and confirms[0].key == "confirm_booking_current-proposal"
    assert any("11:00 AM" in item.value for item in view.markdown)
    # In a long resumed conversation the action must follow the latest answer,
    # not merely exist somewhere above dozens of messages.
    page = list(view.main)
    last_reply = next(i for i, node in enumerate(page)
                      if node.type == "markdown" and node.value == "164,000 km.")
    confirmation = next(i for i, node in enumerate(page)
                        if node.type == "button" and node.label == "Confirm booking")
    assert confirmation > last_reply
    refreshed = AppTest.from_file(file)
    refreshed.query_params.update(view.query_params)
    refreshed.run()
    assert not refreshed.exception
    assert [item.key for item in refreshed.button if item.label == "Confirm booking"] == ["confirm_booking_current-proposal"]
    current[0] = None  # Cancellation or confirmation in another tab invalidates historical proposals.
    refreshed.run()
    assert not any(item.label == "Confirm booking" for item in refreshed.button)
    current[0] = {**old, "session_id": "another-chat"}
    refreshed.run()
    assert not any(item.label == "Confirm booking" for item in refreshed.button)
    assert all(method == "GET" for method, _ in calls)


def test_review_button_confirms_real_api_proposal_without_model_call(monkeypatch, tmp_path):
    """Exercise the real prepare -> GET proposal -> UI confirmation -> persisted receipt path."""
    from datetime import datetime, timedelta
    from urllib.parse import urlsplit

    from fastapi.testclient import TestClient

    from main import create_app
    from planner import TOOLS
    from storage import DUBAI

    today = datetime.now(DUBAI).date()
    monday = today + timedelta(days=7 - today.weekday())

    class PrepareOnce:
        calls = 0

        def plan(self, message, state):
            self.calls += 1
            assert self.calls == 1, "Confirming the displayed proposal must not call the planner."
            return "prepare_booking", TOOLS["prepare_booking"][0].model_validate({
                "car": {"listing_id": 20}, "day": monday.isoformat(),
                "time": "10:00", "kind": "test_drive"})

    planner = PrepareOnce()
    with TestClient(create_app(data_dir=tmp_path, planner=planner)) as client:
        user = "review-owner"
        session = client.post("/sessions", json={"user_id": user}).json()["session_id"]
        prepared = client.post(f"/sessions/{session}/messages", json={
            "user_id": user, "request_id": "prepare-ui",
            "message": f"Book a test drive of listing 20 on {monday} at 10am Dubai time."})
        assert prepared.status_code == 200, prepared.text
        proposal = prepared.json()["booking_proposal"]
        assert proposal and prepared.json()["booking"] is None
        assert client.get("/bookings", params={"user_id": user}).json() == []
        submissions = []

        def actual_api(method, url, **options):
            path = urlsplit(url).path
            if method == "POST":
                submissions.append((path, options["json"]))
            return client.request(method, path, params=options.get("params"), json=options.get("json"))

        monkeypatch.setattr(requests, "request", actual_api)
        file = str(Path(__file__).resolve().parents[1] / "app.py")
        view = AppTest.from_file(file)
        view.query_params.update({"shopper": user, "chat": session})
        view.run()
        assert not view.exception and not view.success
        assert any(item.value == "Review booking" for item in view.subheader)
        next(item for item in view.button if item.label == "Confirm booking").click().run()
        view.run()
        assert not view.exception and len(view.success) == 2
        assert planner.calls == 1 and len(submissions) == 1
        path, body = submissions[0]
        assert path == f"/sessions/{session}/booking-confirmation"
        assert body["proposal_id"] == proposal["proposal_id"]
        bookings = client.get("/bookings", params={"user_id": user}).json()
        assert len(bookings) == 1 and bookings[0]["status"] == "confirmed"
        assert bookings[0]["starts_at"] == f"{monday}T10:00:00+04:00"
        assert not any(item.label == "Confirm booking" for item in view.button)

        refreshed = AppTest.from_file(file)
        refreshed.query_params.update(view.query_params)
        refreshed.run()
        assert not refreshed.exception and len(refreshed.success) == 2
        assert len(submissions) == 1
        next(item for item in refreshed.sidebar.button if item.label == "New chat").click().run()
        assert not refreshed.exception and len(refreshed.success) == 1 and not refreshed.chat_message
        refreshed.sidebar.text_input[0].set_value("other-review-owner").run()
        assert not refreshed.exception and not refreshed.success
