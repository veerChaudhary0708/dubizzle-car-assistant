"""Run the submission conversations against real Gemini and disposable local data.

Run from the project directory: uv run python scripts/demo.py
This consumes Gemini quota. It does not use or change the application's runtime DB.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from dotenv import dotenv_values
from fastapi.testclient import TestClient


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from main import create_app  # noqa: E402
from planner import GeminiPlanner  # noqa: E402
from storage import DUBAI  # noqa: E402


class DemoPlanner(GeminiPlanner):
    """Count actual provider calls and prohibit them during confirmation checks."""

    generation_calls = 0
    forbid_generation = False

    def _generate(self, *args, **kwargs):
        if self.forbid_generation:
            raise AssertionError("Booking confirmation must not call Gemini")
        self.generation_calls += 1
        return super()._generate(*args, **kwargs)


def source_hashes():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(PROJECT.glob("*.py"))}


def render_markdown(report):
    lines = [
        "# Submission demo: real Gemini, isolated data", "",
        f"Recorded: {report['started_at']}. Model: `{report['model']}`.", "",
        "Reproduce with `uv run python scripts/demo.py` after configuring your own API key. "
        "This consumes API quota. The script uses the real FastAPI routes and Gemini with a disposable "
        "database; it never opens the running application's saved chats.", "",
        "The replies below are actual model outputs, not expected-response templates. "
        "Session labels are synthetic. Source hashes and machine-checked assertions are in "
        "[submission-demo.json](submission-demo.json).", "",
    ]
    for section in report["sections"]:
        lines.extend([f"## {section['title']}", ""])
        for event in section["events"]:
            if event["kind"] == "session":
                lines.extend([f"**New session `{event['session']}`; shopper `{event['shopper']}`; "
                              f"saved turns before the first message: {event['initial_turns']}.**", ""])
            elif event["kind"] == "chat":
                lines.extend([f"**Shopper:** {event['message']}", "", "**Assistant:**", "",
                              event["reply"], ""])
                if event.get("booking_proposal"):
                    p = event["booking_proposal"]
                    lines.extend([f"Backend proposal: listing {p.get('listing_id')}, {p.get('kind')}, "
                                  f"`{p.get('starts_at')}`, status `{p.get('status')}`. "
                                  "No appointment has been reserved.", ""])
            elif event["kind"] == "confirmation":
                lines.extend(["**Client action:** Confirm the exact current proposal through "
                              "`POST /sessions/{session_id}/booking-confirmation`.", "",
                              f"**Backend:** {event['reply']}", "",
                              "```json", json.dumps(event["booking"], indent=2), "```", ""])
            elif event["kind"] == "csv":
                lines.extend(["**Actual exported lead CSV:**", "", "```csv", event["csv"].strip(), "```", ""])
    lines.extend(["## Verification", "",
                  f"Automated result: **{report['status']}**; "
                  f"{sum(item['passed'] for item in report['checks'])}/{len(report['checks'])} checks passed.", "",
                  f"Provider calls: {report.get('provider_calls', 0)}. "
                  "Confirmation and its retry were checked with provider generation disabled.", ""])
    if report.get("error"):
        lines.extend([f"Failure: {report['error']}", ""])
    for check in report["checks"]:
        lines.append(f"- {'PASS' if check['passed'] else 'FAIL'}: {check['name']}")
    lines.extend(["", "Model prose still requires review: confirm that the warranty answer does not "
                  "invent coverage, V6 explanation distinguishes general knowledge from listing facts, "
                  "the fresh-session answer recalls the correct shopper, and the lead questions are "
                  "relevant. Passing one recorded run is not a guarantee of all future model wording.", ""])
    return "\n".join(lines)


def run(output_dir):
    settings = dotenv_values(os.getenv("CAR_ASSISTANT_ENV_FILE") or PROJECT / ".env")
    key = os.getenv("GEMINI_API_KEY") or settings.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("Set GEMINI_API_KEY in the environment or .env before running the live demo.")
    model = os.getenv("GEMINI_MODEL") or settings.get("GEMINI_MODEL") or "gemini-3.5-flash"
    planner = DemoPlanner(api_key=key, model=model)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "model": model,
              "isolated_data": True, "source_sha256": source_hashes(), "status": "running",
              "sections": [], "checks": []}

    def check(name, result):
        report["checks"].append({"name": name, "passed": bool(result)})
        if not result:
            raise AssertionError(name)

    def section(title):
        entry = {"title": title, "events": []}
        report["sections"].append(entry)
        return entry["events"]

    try:
        with tempfile.TemporaryDirectory(prefix="car-submission-demo-") as directory:
            app = create_app(data_dir=directory, planner=planner)
            with TestClient(app) as client:
                check("Backend imports exactly 100 inventory listings", client.get("/health").json()["listings"] == 100)

                def new_session(shopper, label, events):
                    response = client.post("/sessions", json={"user_id": shopper})
                    check(f"{label}: session created", response.status_code == 201)
                    sid = response.json()["session_id"]
                    turns = client.get(f"/sessions/{sid}", params={"user_id": shopper}).json()["turns"]
                    events.append({"kind": "session", "shopper": shopper, "session": label, "initial_turns": len(turns)})
                    check(f"{label}: starts with no saved messages", turns == [])
                    return sid

                def ask(shopper, sid, label, message, events):
                    response = client.post(f"/sessions/{sid}/messages", json={
                        "user_id": shopper, "message": message, "request_id": uuid4().hex})
                    data = response.json()
                    events.append({"kind": "chat", "shopper": shopper, "session": label,
                                   "message": message, "http_status": response.status_code,
                                   "reply": data.get("reply", data.get("detail", "")),
                                   "cars": data.get("cars", []), "tool_trace": data.get("tool_trace", []),
                                   "booking_proposal": data.get("booking_proposal"), "booking": data.get("booking")})
                    print(json.dumps({"session": label, "message": message, "reply": events[-1]["reply"]}, ensure_ascii=False), flush=True)
                    check(f"{label}: {message} — HTTP 200", response.status_code == 200)
                    return data

                memory_user = "submission-memory-shopper"
                first = section("1. Multi-turn inventory exploration")
                sid = new_session(memory_user, "memory-1", first)
                result = ask(memory_user, sid, "memory-1",
                             "My budget is AED 25,000. I prefer convertibles. Find a Ford Mustang under AED 25,000.", first)
                check("Mustang search returns listing 20", [car["listing_id"] for car in result["cars"]] == [20])
                check("Mustang asking price is AED 21,500", result["cars"][0]["price_aed"] == 21500)
                result = ask(memory_user, sid, "memory-1", "What's its mileage?", first)
                check("Mileage answer contains the verified 164,000 km", "164000" in result["reply"].replace(",", "").replace(" ", ""))
                ask(memory_user, sid, "memory-1", "Does it have a warranty?", first)
                ask(memory_user, sid, "memory-1", "What does V6 mean?", first)
                ask(memory_user, sid, "memory-1", "I like this car.", first)
                state = app.state.store.state(memory_user, sid)
                check("Explicit personal budget was persisted", state["preferences"].get("max_budget_aed") == 25000)
                check("First conversation has not booked anything", app.state.store.list_bookings(memory_user) == [])

                recall = section("2. Returning shopper in a completely new session")
                fresh = new_session(memory_user, "memory-2", recall)
                fresh_state = app.state.store.state(memory_user, fresh)
                check("New session has no selected car", fresh_state["selected_id"] is None)
                check("New session retains the same shopper's budget", fresh_state["preferences"].get("max_budget_aed") == 25000)
                result = ask(memory_user, fresh, "memory-2", "What was my budget, and which car did I like last time?", recall)
                check("Fresh-session reply recalls AED 25,000", "25000" in result["reply"].replace(",", "").replace(" ", ""))
                check("Fresh-session reply recalls the Mustang", "mustang" in result["reply"].lower())

                lead = section("3. Gather price range and needs; export a lead without booking")
                lead_user = "submission-lead-shopper"
                lead_sid = new_session(lead_user, "lead-1", lead)
                check("Different shopper does not inherit preferences", app.state.store.state(lead_user, lead_sid)["preferences"] == {})
                result = ask(lead_user, lead_sid, "lead-1", "What cars are available?", lead)
                check("Browsing asks a budget question", "?" in result["reply"] and any(word in result["reply"].lower() for word in ("budget", "price range", "spend")))
                result = ask(lead_user, lead_sid, "lead-1", "Between AED 20,000 and AED 30,000", lead)
                check("Budget reply asks about vehicle requirements", "?" in result["reply"] and any(word in result["reply"].lower() for word in ("need", "feature", "type", "prefer", "kind")))
                requirements = "Automatic with room for two child seats"
                ask(lead_user, lead_sid, "lead-1", requirements, lead)
                with app.state.store.leads_path.open(encoding="utf-8", newline="") as source:
                    rows = list(csv.DictReader(source))
                row = next(row for row in rows if row["session_id"] == lead_sid)
                check("CSV contains the stated AED 20,000–30,000 range", row["budget_min_aed"] == "20000" and row["budget_max_aed"] == "30000")
                check("CSV captures automatic transmission and child-seat needs", "automatic" in row["needs"].lower() and "child" in row["needs"].lower())
                check("Lead collection does not require an appointment", app.state.store.list_bookings(lead_user) == [])
                import io
                preview = io.StringIO(newline="")
                writer = csv.DictWriter(preview, fieldnames=list(row))
                writer.writeheader()
                writer.writerow({**row, "session_id": "lead-1"})
                lead.append({"kind": "csv", "csv": preview.getvalue(), "note": "Only the generated session UUID is replaced with its demo label."})

                booking = section("4. Review and confirm a simulated booking")
                today = datetime.now(DUBAI).date()
                day = today + timedelta(days=((7 - today.weekday()) % 7) or 7)
                result = ask(memory_user, sid, "memory-1",
                             f"Book a test drive of the 2014 Ford Mustang on {day.isoformat()} at 10 AM Dubai time.", booking)
                proposal = result.get("booking_proposal") or {}
                check("Complete request produces an awaiting-confirmation proposal", proposal.get("status") == "awaiting_confirmation" and bool(proposal.get("proposal_id")))
                check("Proposal contains exact car, type and time", proposal.get("listing_id") == 20 and proposal.get("kind") == "test_drive" and proposal.get("starts_at") == f"{day}T10:00:00+04:00")
                check("Preparing the proposal reserves nothing", app.state.store.list_bookings(memory_user) == [])
                ask(memory_user, sid, "memory-1", "Before I confirm, remind me of its mileage.", booking)
                exposed = client.get(f"/sessions/{sid}", params={"user_id": memory_user}).json()["booking_proposal"]
                check("Mileage question preserves the exact proposal", exposed["proposal_id"] == proposal["proposal_id"])
                check("Question before confirmation reserves nothing", app.state.store.list_bookings(memory_user) == [])
                planner.forbid_generation = True
                call_count = planner.generation_calls
                confirmation = {"user_id": memory_user, "request_id": uuid4().hex, "proposal_id": proposal["proposal_id"]}
                response = client.post(f"/sessions/{sid}/booking-confirmation", json=confirmation)
                check("Explicit confirmation succeeds without Gemini", response.status_code == 200 and planner.generation_calls == call_count)
                confirmed = response.json()
                receipt = confirmed["booking"]
                booking.append({"kind": "confirmation", "reply": confirmed["reply"], "booking": receipt})
                check("Receipt has the exact reviewed car, type and time", all(receipt[key] == proposal[key] for key in ("listing_id", "kind", "starts_at")) and receipt["status"] == "confirmed" and bool(receipt["booking_id"]))
                repeated = client.post(f"/sessions/{sid}/booking-confirmation", json=confirmation)
                check("Retry returns the same receipt", repeated.status_code == 200 and repeated.json() == confirmed)
                check("Exactly one appointment exists", client.get("/bookings", params={"user_id": memory_user}).json() == [receipt])
                check("Other shopper cannot see the appointment", client.get("/bookings", params={"user_id": lead_user}).json() == [])
                planner.forbid_generation = False
                report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["provider_calls"] = planner.generation_calls
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["source_unchanged"] = source_hashes() == report["source_sha256"]
        if not report["source_unchanged"]:
            report["status"] = "failed"
            report["error"] = "Production source changed during the run"
        output_dir.mkdir(parents=True, exist_ok=True)
        # The report contains only synthetic conversations and public listing facts.
        serialized = json.dumps(report, indent=2, ensure_ascii=False)
        if key in serialized:
            raise RuntimeError("Refusing to write a report containing a credential")
        (output_dir / "submission-demo.json").write_text(serialized, encoding="utf-8")
        (output_dir / "submission-demo.md").write_text(render_markdown(report), encoding="utf-8")
        print(f"Demo {report['status']}: {sum(c['passed'] for c in report['checks'])}/{len(report['checks'])} checks; "
              f"{report['provider_calls']} provider calls.", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "evidence")
    raise SystemExit(run(parser.parse_args().output_dir))
