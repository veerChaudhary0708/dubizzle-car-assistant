"""Prepare durable appointment proposals; only explicit confirmation reserves."""

import re
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from storage import BookingError, DUBAI, validate_slot


def normalize_appointment_fields(message, now=None):
    """Ground unambiguous calendar/clock phrases in Dubai; never use old prose."""
    now = now or datetime.now(DUBAI)
    today = now.astimezone(DUBAI).date() if now.tzinfo else now.date()
    days, times = [], []
    iso_pattern = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?"
    for match in re.finditer(iso_pattern, message):
        try:
            moment = datetime.fromisoformat(match[0].replace("Z", "+00:00"))
            if moment.tzinfo:
                moment = moment.astimezone(DUBAI)
            days.append(moment.date().isoformat())
            if moment.second or moment.microsecond:
                return {"error": "Choose a half-hour slot with zero seconds; appointment times are not rounded.",
                        "invalid_field": "time", "day": days[-1]}
            times.append(moment.strftime("%H:%M"))
        except ValueError:
            return {"error": "Please give a valid appointment date and time."}
    message = re.sub(iso_pattern, " ", message)
    for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", message):
        try:
            days.append(date.fromisoformat(value).isoformat())
        except ValueError:
            return {"error": "Please give a valid appointment date."}
    months = {name[:3]: index for index, name in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), 1)}
    month_pattern = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    named_dates = (
        rf"\b(?P<month>{month_pattern})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{{2}}))?\b",
        rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{month_pattern})\.?(?:,?\s+(?P<year>20\d{{2}}))?\b",
    )
    for pattern in named_dates:
        for match in re.finditer(pattern, message, re.I):
            try:
                explicit = date(int(match["year"] or today.year), months[match["month"][:3].title()], int(match["day"]))
                if not match["year"] and explicit < today:
                    explicit = explicit.replace(year=today.year + 1)
                days.append(explicit.isoformat())
            except ValueError:
                return {"error": "Please give a valid appointment date."}
    explicit_days = set(days)
    for word in re.findall(r"\b(today|tomorrow)\b", message, re.I):
        days.append((today + timedelta(days=word.lower() == "tomorrow")).isoformat())
    weekdays = {name: index for index, name in enumerate(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"))}
    for match in re.finditer(r"\b(?:(this|upcoming|coming|next)\s+)?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", message, re.I):
        if explicit_days:
            # A weekday accompanying a calendar date describes that date. It
            # must not independently replace it with the nearest weekday.
            if any(date.fromisoformat(day).weekday() != weekdays[match[2].lower()] for day in explicit_days):
                return {"error": "The weekday and calendar date do not match. Which appointment date should I use?"}
            if not match[1] or match[1].lower() == "next":
                continue
        if (match[1] or "").lower() == "next":
            nearest = today + timedelta(days=(weekdays[match[2].lower()] - today.weekday()) % 7)
            return {"error": f"By next {match[2].title()}, do you mean {nearest.isoformat()} or {(nearest + timedelta(days=7)).isoformat()}?"}
        days.append((today + timedelta(days=(weekdays[match[2].lower()] - today.weekday()) % 7)).isoformat())
    def invalid_time(error):
        return {"error": error, "invalid_field": "time", **({"day": days[0]} if len(set(days)) == 1 else {})}
    for match in re.finditer(r"(?<![\w:])\d{1,2}:\d{2}:(\d{2})(?:\.(\d+))?", message):
        if int(match[1]) or (match[2] and int(match[2])):
            return invalid_time("Choose a half-hour slot with zero seconds; appointment times are not rounded.")
    for match in re.finditer(r"(?<![\w:])(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?!\w)", message, re.I):
        hour, minute = int(match[1]), int(match[2] or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return invalid_time("Please give a valid appointment time.")
        hour = hour % 12 + (12 if match[3].lower().startswith("p") else 0)
        times.append(f"{hour:02d}:{minute:02d}")
    for match in re.finditer(r"(?<![\w:])(\d{1,2}):(\d{2})(?!\d|\s*[ap]\.?m)", message, re.I):
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            return invalid_time("Please give a valid appointment time.")
        times.append(f"{hour:02d}:{minute:02d}")
    if re.search(r"\bnoon\b", message, re.I):
        times.append("12:00")
    if re.search(r"\bmidnight\b", message, re.I):
        times.append("00:00")
    if len(set(days)) > 1 or len(set(times)) > 1:
        return {"error": "You mentioned different dates or times. Which one should I use for the appointment?"}
    return {key: values[0] for key, values in (("day", days), ("time", times)) if values}



# A closed grammar for accepting the exact proposal just shown. Full matching
# deliberately excludes questions, negation, additional tasks and appointment
# modifiers; those messages stay in the ordinary draft-editing conversation.
_CONFIRM_OBJECT = r"(?:it|this|that|(?:(?:the|my|this|that)\s+)?(?:booking|appointment|proposal|test[ -]drive|viewing))"
_CONFIRM_COMMAND = rf"(?:please\s+)?confirm(?:\s+{_CONFIRM_OBJECT})?(?:,\s*please|\s+please)?"
AFFIRMATIVE = re.compile(
    rf"\s*(?:yes|yes(?:\s*,\s*|\s+)(?:please|{_CONFIRM_COMMAND})|{_CONFIRM_COMMAND})[.!]?\s*",
    re.I,
)


def draft_from_state(state):
    """Read current drafts and conservatively retain legacy unfinished fields."""
    old = state.get("pending_booking") or {}
    if old.get("status") not in {"collecting", "awaiting_confirmation"} and old.get("intent") not in {"booking", "offer"}:
        return None
    draft = {key: old[key] for key in ("listing_id", "car_label", "day", "time", "kind", "starts_at", "proposal_id") if old.get(key) is not None}
    draft.update(session_id=state["session_id"], status=old.get("status", "collecting"))
    return draft


def current_proposal(state):
    draft = draft_from_state(state)
    if (not draft or draft.get("status") != "awaiting_confirmation"
        or not all(draft.get(key) for key in ("proposal_id", "listing_id", "car_label", "starts_at", "kind"))):
        return None
    return draft


class BookingService:
    """Domain transaction boundary: preparing is always non-reserving."""

    def __init__(self, store):
        self.store = store

    def prepare(self, state, fields, row, message, *, unresolved_car=False):
        if "kind" not in fields or fields["kind"] not in {None, "viewing", "test_drive"}:
            raise BookingError("Supply the appointment type explicitly, or null when it still needs clarification.")
        prior = draft_from_state(state) or {}
        draft = {key: prior[key] for key in ("listing_id", "car_label", "day", "time", "kind") if key in prior}
        draft.update(status="collecting", session_id=state["session_id"])
        if unresolved_car:
            draft.pop("listing_id", None)
            draft.pop("car_label", None)
        if row:
            draft.update(listing_id=row["listing_id"], car_label=f"{row['year']} {row['make'].title()} {row['model'].title()}")
        if fields["kind"] is not None:
            draft["kind"] = fields["kind"]
        supplied = {key: fields[key] for key in ("day", "time") if fields.get(key)}
        error = None
        if fields.get("starts_at"):
            try:
                moment = datetime.fromisoformat(fields["starts_at"])
                if moment.tzinfo:
                    moment = moment.astimezone(DUBAI)
                supplied["day"] = moment.date().isoformat()
                if re.search(r"[T ]\d{1,2}:", fields["starts_at"]):
                    supplied["time"] = moment.time().isoformat()
            except (TypeError, ValueError):
                error = "Please give a valid appointment date and time."
        normalized = normalize_appointment_fields(message)
        if normalized.get("error"):
            error = normalized["error"]
            if normalized.get("invalid_field"):
                supplied.pop(normalized["invalid_field"], None)
                draft.pop(normalized["invalid_field"], None)
                supplied.update({key: normalized[key] for key in ("day", "time") if key in normalized})
            else:
                supplied = {}
        else:
            supplied.update(normalized)
        draft.update(supplied)
        if draft.get("day"):
            try:
                chosen = date.fromisoformat(draft["day"])
                if chosen < datetime.now(DUBAI).date():
                    raise ValueError("past date")
                if chosen.weekday() == 6:
                    raise ValueError("Sunday")
            except (TypeError, ValueError):
                draft.pop("day", None)
                error = "Choose a future date from Monday to Saturday."
        if draft.get("time"):
            try:
                chosen_time = time.fromisoformat(draft["time"])
                if (chosen_time.tzinfo or not 8 <= chosen_time.hour < 20 or chosen_time.minute not in (0, 30)
                    or chosen_time.second or chosen_time.microsecond):
                    raise ValueError("outside slot hours")
                draft["time"] = chosen_time.strftime("%H:%M")
            except (TypeError, ValueError):
                draft.pop("time", None)
                error = "Choose a half-hour slot between 08:00 and 19:30 Dubai time."
        missing = [key for key in ("listing_id", "day", "time", "kind") if not draft.get(key)]
        if not missing and not error:
            try:
                starts = validate_slot(draft["listing_id"], f"{draft['day']}T{draft['time']}")
                if starts not in self.store.available_slots(draft["listing_id"], draft["day"]):
                    raise BookingError("That slot is unavailable. Choose another time.")
                same = all(draft.get(key) == prior.get(key) for key in ("listing_id", "day", "time", "kind"))
                draft.update(status="awaiting_confirmation", starts_at=starts,
                             proposal_id=prior.get("proposal_id") if same and prior.get("proposal_id") else uuid4().hex)
            except BookingError as exc:
                error = str(exc)
                draft.pop("time", None)
                missing = ["time"]
        proposal = dict(draft) if draft["status"] == "awaiting_confirmation" else None
        if proposal:
            reply = (f"Please confirm a simulated {draft['kind'].replace('_', ' ')} for the {draft['car_label']} "
                     f"on {draft['day']} at {draft['time']} Dubai time (UTC+04:00). "
                     "Use Confirm booking or reply yes. No appointment is reserved until you confirm.")
        else:
            labels = {"listing_id": "car", "day": "date", "time": "time", "kind": "appointment type (viewing or test drive)"}
            reply = error or ("Which " + " and ".join(labels[key] for key in missing) + " would you like for the appointment?")
        return {"draft": draft, "proposal": proposal,
                "outcome": {"status": draft["status"], "reply": reply, "missing_fields": missing},
                "reply": reply, "status": "needs_clarification" if error else draft["status"]}

    def confirmation(self, state, proposal_id):
        proposal = current_proposal(state)
        if not proposal or proposal["proposal_id"] != proposal_id:
            raise BookingError("This proposal is no longer current. Review the latest appointment details before confirming.")
        starts = validate_slot(proposal["listing_id"], proposal["starts_at"])
        if starts not in self.store.available_slots(proposal["listing_id"], starts[:10]):
            raise BookingError("That slot is no longer available. Choose another time and confirm the updated proposal.")
        booking = {"listing_id": proposal["listing_id"], "starts_at": starts, "kind": proposal["kind"]}
        reply = (f"Your simulated {proposal['kind'].replace('_', ' ')} for the {proposal['car_label']} is confirmed "
                 f"for {proposal['day']} at {proposal['time']} Dubai time (UTC+04:00). "
                 "This is a demo booking; no dealership has been contacted.")
        return booking, reply
