"""Execute typed chat actions against the inventory and the current shopper's state."""

import copy
import math
import re
from contextlib import closing
import sqlite3
from datetime import datetime

from inventory import BODY, COLORS, get_listing, search_inventory
from constraints import extract_price_constraints
from planner import Preference, Save, Search, THIRD_PARTY, lead_collection
from storage import BookingError, DUBAI, RequestConflict
from booking import AFFIRMATIVE, BookingService, current_proposal, draft_from_state, normalize_appointment_fields


PREF_LABELS = {
    "name": "name", "min_budget_aed": "minimum budget", "max_budget_aed": "maximum budget",
    "make": "preferred make", "model": "preferred model", "color": "preferred color",
    "body_style": "preferred body style", "needs": "shopping needs",
}
PAGE_SIZE = 5
CONVERSATION = {
    "hello": "Hi! What kind of car are you looking for, and what budget would you like me to use?",
    "thanks": "You're welcome. I can look up another car or help arrange a viewing.",
    "help": "I can search this inventory, check listing details, remember your preferences and reserve a demo viewing. What would you like to do?",
    "off_topic": "I can help with used-car shopping here. Would you like to search for a car or arrange a viewing?",
    "competitor": "I can help you explore the cars in this inventory. What make or price range would you like to see?",
    "clarify": "Could you clarify which car, feature or viewing you have in mind?",
    "currency": "The inventory uses AED. What price range in AED should I use?",
}


def car_card(row):
    keys = ("listing_id", "year", "make", "model", "trim", "title", "description", "price_aed", "mileage_km", "photo_url")
    return {**{k: row[k] for k in keys}, "booking_eligible": True}


def car_name(row):
    return f"{row['year']} {row['make'].title()} {row['model'].title()} (listing {row['listing_id']})"


def trusted_listing_ids(value):
    """Inventory references already supplied by owned state or backend tools."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"listing_id", "selected_id"} and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                found.add(item)
            elif key in {"shown_ids", "result_ids", "unverified_ids", "liked_listing_ids"} and isinstance(item, list):
                found.update(lid for lid in item if isinstance(lid, int) and not isinstance(lid, bool) and lid > 0)
            else:
                found.update(trusted_listing_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(trusted_listing_ids(item))
    return found


CLAUSE_BREAK = re.compile(
    r"[;!?\n]|\.(?!\d)|\b(?:but|however)\b|"
    r"\band\s+(?=(?:i\s+(?:(?:also|really|usually|generally|personally|would)\s+)?"
    r"(?:prefer|like|love|need|require|have\s+a\s+budget|can\s+(?:spend|afford|pay))|"
    r"i(?:'m| am)\s+(?:partial to|interested in|considering)|"
    r"my\s+(?:(?:new|maximum|minimum|total|absolute|preferred|favorite|favourite)\s+)?"
    r"(?:budget|limit|cap|floor|ceiling|price range|preference|name))\b)|"
    r"\b(?:and\s+)?(?=(?:please\s+)?(?:show me|find me|do you have|i(?:'m| am) looking for)\b)", re.I)
QUESTION = re.compile(r"^\s*(?:who|what|when|where|which|why|how|did|do|does|is|was|were|are|have|has|can you recall|can you tell)\b", re.I)
PREFERENCE_CUE = re.compile(r"\b(?:i\s+(?:(?:really|also|generally|usually|personally|would)\s+)?(?:prefer|like|love|need|require)|my\s+(?:favo[u]?rite|preferred|preference)|i(?:'m| am)\s+(?:partial to|interested in|considering))\b", re.I)
BUDGET_CUE = re.compile(r"\b(?:my\s+(?:(?:new|maximum|minimum|total|absolute)\s+)?(?:budget|limit|cap|floor|ceiling|price range)|i\s+have\s+a\s+budget|i\s+(?:can|will|want to|plan to)\s+(?:afford|spend|pay))\b", re.I)
FLOOR = re.compile(r"\b(?:minimum|min\.?|floor|at least|no less than|at or above|starting (?:at|from))\b", re.I)
CURRENCY_AMOUNT = re.compile(r"(?<![\w.,])(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|m|million)?\b", re.I)
SCALES = {"k": 1000, "thousand": 1000, "m": 1000000, "million": 1000000, "": 1}


def _clauses(message, evidence):
    # The model may return a short excerpt ("25k") or quote the entire message.
    # Check the shopper's surrounding clause to recover its subject and intent.
    return [clause.strip(" ,") for clause in CLAUSE_BREAK.split(message.casefold()) if clause.strip(" ,")
            and (evidence in clause or clause.strip(" ,") in evidence) and not QUESTION.match(clause.strip(" ,"))]


def _budget_values(clause, answering_budget):
    clause = re.split(r",\s*(?:not|never)\b", clause, maxsplit=1)[0]
    clause = re.sub(r"\b(aed|dhs|dirhams?)(?=\d)", r"\1 ", clause, flags=re.I)
    short_answer = answering_budget and re.fullmatch(
        r"(?:actually[, ]*|about\s+|between\s+|at least\s+|up to\s+)?(?:aed\s*)?\d[\d,.\s]*(?:k|m|thousand|million)?"
        r"(?:\s*(?:-|–|—|and|to)\s*(?:aed\s*)?\d[\d,.\s]*(?:k|m|thousand|million)?)?\s*(?:aed|dirhams?)?",
        clause,
    )
    if not (BUDGET_CUE.search(clause) or short_answer):
        return {}
    if re.search(r"\b(?:usd|eur|gbp)\b|[$€£]", clause):
        return {}
    if BUDGET_CUE.search(clause):
        constraints = extract_price_constraints(clause)
        if constraints.ambiguous:
            return {}
        if constraints.bounds:
            return {f"{kind}_budget_aed": float(constraints.bounds[f"{kind}_price"])
                    for kind in ("min", "max") if f"{kind}_price" in constraints.bounds}
    amounts = list(CURRENCY_AMOUNT.finditer(clause))
    def number(match, implied_scale=""):
        return float(match[1].replace(",", "")) * SCALES[match[2] or implied_scale]

    # Bind each bound to its position in an explicit range, including "20–30k".
    for left, right in zip(amounts, amounts[1:]):
        connector = clause[left.end():right.start()].strip()
        if re.fullmatch(r"(?:-|–|—|to|and)\s*(?:aed|dirhams?)?", connector):
            inherited_scale = right[2] or "" if number(left) < 1000 else ""
            low = number(left, inherited_scale)
            high = number(right)
            if low < high:
                return {"min_budget_aed": low, "max_budget_aed": high}
    if len(amounts) == 1:
        key = "min_budget_aed" if FLOOR.search(clause) else "max_budget_aed"
        return {key: number(amounts[0])}
    return {}


def _explicit_value(clause, key, value):
    if not value:
        return bool(re.search(r"\b(?:forget|clear|remove|no longer|don't care|no preference)\b", clause))
    if key == "name":
        return bool(re.search(r"\b(?:my name is|call me|i am|i'm)\s+", clause)) and value.casefold() in clause
    cue = PREFERENCE_CUE.search(clause)
    if not cue:
        return False
    if key == "needs":
        return True
    # The compared alternative in "I prefer Ford over Toyota" has no endorsement.
    subject = re.split(r",\s*(?:not|never)\b|\b(?:over|instead of|rather than|compared (?:to|with)|because)\b",
                       clause[cue.end():], maxsplit=1)[0]
    mentioned = subject.replace("-", " ")
    term = value.casefold().replace("-", " ")
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"s?(?!\w)", mentioned))


def _preference_assertion(clause, message, *, clearing=False):
    """Keep literal personal statements separate from questions and examples."""
    explicit_save = bool(re.match(r"(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:remember|save|store)\b", clause, re.I))
    if THIRD_PARTY.search(clause) or QUESTION.match(clause):
        return False
    if re.search(r'["“”`]|(?:^|\s)\'[^\']+\'', clause):
        return False
    if re.search(r"\b(?:if|suppose|supposing|assuming|imagine|hypothetically|perhaps|maybe|might|"
                 r"for example|used to|previously|formerly|said|says|quoted?)\b", clause, re.I):
        return False
    if not explicit_save and re.search(re.escape(clause.strip()) + r"\s*\?", message, re.I):
        return False
    affirmative = re.split(r",\s*(?:not|never)\b", clause, maxsplit=1)[0]
    if not clearing and re.search(
        r"\b(?:not|never|don't|doesn't|didn't|isn't|aren't|wasn't|weren't|can't|cannot|no longer)\b",
        affirmative, re.I,
    ):
        return False
    return True


def supported_preferences(changes, message, previous=None, prior_reply=""):
    writes = {}
    answering_budget = bool(re.search(r"(?:what|which).*?(?:budget|price range)[^?]*\?\s*$", prior_reply, re.I))
    answering_needs = bool(re.search(
        r"(?:what|which).*?(?:needs|requirements|features|kind of car|body style)[^?]*\?\s*$",
        prior_reply, re.I,
    ))
    for change in changes:
        evidence = change.evidence.strip().casefold()
        if evidence not in message.casefold():
            continue
        # A turn can include the shopper's own preferences and a separate inquiry for someone else.
        value = change.value.strip()
        clauses = [clause for clause in _clauses(message, evidence)
                   if _preference_assertion(clause, message, clearing=not value)]
        if not value or change.key not in {"max_budget_aed", "min_budget_aed"}:
            literal_needs_answer = (change.key == "needs" and answering_needs and value
                and len(message.strip()) <= 180 and value.casefold() == evidence
                and message.strip().rstrip(".! ").casefold() == evidence.rstrip(".! ")
                and not re.search(r"\b(?:no preference|not sure|anything|whatever|don't know|skip|no thanks)\b", message, re.I)
                and bool(clauses))
            if not literal_needs_answer and not any(_explicit_value(clause, change.key, value) for clause in clauses):
                continue
        if change.key == "needs" and change.append and value and previous and previous.get("needs"):
            existing = previous["needs"]
            if value.casefold() not in existing.casefold():
                value = f"{existing}, {value}"
            else:
                value = existing
        if value and change.key in {"max_budget_aed", "min_budget_aed"}:
            digits = value.replace(",", "")
            if not digits.isdecimal() or len(digits) > 10:
                continue
            amount = int(digits)
            if not any(_budget_values(clause, answering_budget).get(change.key) == amount for clause in clauses):
                continue
            value = amount
        writes[change.key] = value if value != "" else None
    return writes


def automatic_preferences(conn, message, previous=None, prior_reply=""):
    """Propose unambiguous literal preferences even when the model only searches.

    This is a small, conservative ingestion pass, not an alternate intent planner.
    All candidates use the same evidence validator as model-proposed updates.
    """
    labels = {key: set() for key in ("make", "model")}
    for row in conn.execute("SELECT DISTINCT make, model FROM listings"):
        for key, value in zip(labels, row):
            if value and not str(value).isdecimal():
                labels[key].add(str(value))
    answering_budget = bool(re.search(r"(?:what|which).*?(?:budget|price range)[^?]*\?\s*$", prior_reply, re.I))
    candidates = {}
    conflicts = set()
    for raw_clause in CLAUSE_BREAK.split(message):
        evidence = raw_clause.strip(" ,")
        clause = evidence.casefold()
        if not evidence or len(evidence) > 500 or not _preference_assertion(clause, message):
            continue
        proposed = []
        budget_cue = BUDGET_CUE.search(clause)
        allowed_prefix = (r"(?:(?:actually|also|now|correction|well|personally)[, :]*\s*)*"
                          r"(?:(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:remember|save|store)(?:\s+that)?\s+)?")
        personal_budget = budget_cue and re.fullmatch(allowed_prefix, clause[:budget_cue.start()])
        for key, amount in (_budget_values(clause, answering_budget) if personal_budget or (answering_budget and not budget_cue) else {}).items():
            if amount.is_integer() and 0 <= amount < 10**10:
                proposed.append(Preference(key=key, value=str(int(amount)), evidence=evidence))
        cue = PREFERENCE_CUE.search(clause)
        if cue and re.fullmatch(allowed_prefix, clause[:cue.start()]):
            # Only the preferred side of an explicit comparison is endorsed.
            subject = re.split(r",\s*(?:not|never)\b|\b(?:over|instead of|rather than|compared (?:to|with)|because)\b",
                               clause[cue.end():], maxsplit=1)[0]
            for key, values in labels.items():
                matches = []
                for value in sorted(values, key=len, reverse=True):
                    match = re.search(r"(?<!\w)" + re.escape(value) + r"s?(?!\w)", subject, re.I)
                    if match and not any(start <= match.start() and match.end() <= end for start, end in matches):
                        matches.append(match.span())
                        proposed.append(Preference(key=key, value=value, evidence=evidence))
            for key, pattern in (("body_style", BODY), ("color", COLORS)):
                for match in re.finditer(r"\b" + pattern + r"s?\b", subject, re.I):
                    proposed.append(Preference(key=key, value=match["value"], evidence=evidence))
        # Multiple alternatives in one clause are not a single saved preference.
        by_key = {}
        for item in proposed:
            by_key.setdefault(item.key, {})[item.value.casefold()] = item
        for key, values in by_key.items():
            if len(values) != 1:
                conflicts.add(key)
                candidates.pop(key, None)
                continue
            item = next(iter(values.values()))
            correction = bool(re.match(r"(?:actually|correction|now)\b", clause))
            if key in conflicts and not correction:
                continue
            previous_item = candidates.get(key)
            if previous_item and previous_item.value.casefold() != item.value.casefold() and not correction:
                conflicts.add(key)
                candidates.pop(key, None)
                continue
            candidates[key] = item
            conflicts.discard(key)
    validated = supported_preferences(list(candidates.values()), message, previous, prior_reply)
    return [item for key, item in candidates.items() if key in validated][:8]


def search(conn, action):
    filters = action.filters.model_dump(exclude_none=True, exclude_defaults=True)
    # A model sometimes puts a manufacturer in the trim field. Reclassify only
    # exact, unambiguous inventory labels, preserving every requested constraint.
    fields = ("make", "model", "trim")
    if any(filters.get(field) for field in fields):
        labels = {field: set() for field in fields}
        for row in conn.execute("SELECT DISTINCT make, model, trim FROM listings"):
            for field, label in zip(fields, row):
                labels[field].add(label.casefold())
        mapped = {}
        for field in fields:
            if field not in filters:
                continue
            value = filters[field]
            matched = [key for key in fields if value.casefold() in labels[key]]
            target = matched[0] if len(matched) == 1 else field
            if target in mapped and mapped[target].casefold() != value.casefold():
                break
            mapped[target] = value
        else:
            for field in fields:
                filters.pop(field, None)
            filters.update(mapped)
    bounds = dict(filters)
    for field, direction in (("max", -math.inf), ("min", math.inf)):
        exclusive = bounds.pop(field + "_exclusive", False)
        key = field + "_price"
        if exclusive and key in bounds:
            bounds[key] = math.nextafter(float(bounds[key]), direction)
    rows = search_inventory(conn, **bounds, limit=100)
    # All inventory listings support simulated bookings. Keep bookable_only in
    # the action schema for existing clients and saved search snapshots.
    column = {"lowest_price": "price_aed", "lowest_mileage": "mileage_km", "newest": "year"}.get(action.order)
    if column:
        rows = sorted(rows, key=lambda r: (
            r[column] is None,
            (-r[column] if column == "year" else r[column]) if r[column] is not None else 0,
            r["listing_id"]))
    return rows, filters


def inventory_observation(rows, *, detailed=False):
    """Facts and missing-data coverage; absence is never turned into a numeric value."""
    keys = ("listing_id", "year", "make", "model", "trim", "price_aed", "mileage_km", "color", "body_style")
    cars = [{k: row.get(k) for k in keys} for row in rows]
    if detailed:
        for car, row in zip(cars, rows):
            car["seller_title"] = row.get("title", "")
            car["seller_description"] = row.get("description", "")[:6000]
    coverage = {}
    for key in ("price_aed", "mileage_km", "year"):
        known = [row for row in rows if row.get(key) is not None]
        minimum = min((row[key] for row in known), default=None)
        coverage[key] = {"known": len(known), "unknown": len(rows) - len(known),
                         "minimum_known": minimum,
                         "lowest_known_listing_ids": [r["listing_id"] for r in known if r[key] == minimum],
                         "complete": len(known) == len(rows)}
    return {"cars": cars, "coverage": coverage, "fuel_economy": {"verified": False,
            "note": "No standardized fuel-economy measurements are supplied. Odometer kilometres are distance already driven, not km/l or consumption."}}


def unknown_candidates(conn, filters):
    """Candidates that could meet a bound, but cannot be verified from supplied data."""
    fields = {"min_price": "price_aed", "max_price": "price_aed", "min_mileage": "mileage_km",
              "max_mileage": "mileage_km", "color": "color", "body_style": "body_style"}
    relevant = {k: v for k, v in filters.items() if k in fields}
    if not relevant:
        return []
    broad = {k: v for k, v in filters.items() if k not in fields and k not in {"max_exclusive", "min_exclusive"}}
    uncertain = []
    for row in search_inventory(conn, **broad, limit=100):
        missing, excluded = False, False
        for bound, requested in relevant.items():
            value = row[fields[bound]]
            if value is None:
                missing = True
            elif bound in {"color", "body_style"}:
                excluded |= str(value).casefold() != str(requested).casefold()
            elif bound.startswith("max_"):
                excluded |= value > requested or (bound == "max_price" and bool(filters.get("max_exclusive")) and value == requested)
            else:
                excluded |= value < requested or (bound == "min_price" and bool(filters.get("min_exclusive")) and value == requested)
        if missing and not excluded:
            uncertain.append(row)
    return uncertain


def _inquiry_ids(inquiry):
    if inquiry.get("listing_id") is not None:
        return [inquiry["listing_id"]]
    return inquiry.get("shown_ids", [])


def resolve_car(conn, ref, state, store=None):
    scope = ref.scope or ("previous" if ref.previous_visit else None)
    historical = scope is not None or ref.history_kind is not None
    if historical and store is not None:
        events = store.inquiries(state["user_id"], state["session_id"],
                                 scope=scope or "all", kind=ref.history_kind)
        if scope in (None, "all", "current"):
            staged = [event for event in reversed(state.get("_turn_events", []))
                      if not ref.history_kind or event.get("kind") == ref.history_kind]
            events = staged + events
        if ref.position is not None and ref.history_kind in (None, "search"):
            # An ordinal refers to the ordered cards in a result set.
            events = [e for e in events if e.get("kind") == "search"]
            if ref.make or ref.model:
                named = {r["listing_id"] for r in search_inventory(conn, make=ref.make, model=ref.model, limit=100)}
                events = [e for e in events if named.intersection(_inquiry_ids(e))]
            events = events[:1]
        ids = list(dict.fromkeys(i for event in events for i in _inquiry_ids(event)))
    elif historical:
        ids = [i for event in state["prior_inquiries"]
               if not ref.history_kind or event.get("kind") == ref.history_kind for i in _inquiry_ids(event)]
    else:
        ids = state["shown_ids"]
    if ref.listing_id is not None:
        if historical and ref.listing_id not in ids:
            return None, "I couldn't match that listing to the history you described. Which listing number should I use?"
        row = get_listing(conn, ref.listing_id)
        return row, None if row else f"There is no listing {ref.listing_id} in this inventory."
    if not historical and ref.position is None and not (ref.make or ref.model) and state["selected_id"]:
        ids = [state["selected_id"]]
    if ref.make or ref.model:
        named = search_inventory(conn, make=ref.make, model=ref.model, limit=100)
        by_id = {r["listing_id"]: r for r in named}
        choices = [by_id[i] for i in ids if i in by_id]
        if not choices and not historical and ref.position is None:
            choices = named
    else:
        choices = [r for i in ids if (r := get_listing(conn, i))]
    if ref.position is not None:
        if ref.position <= len(choices):
            return choices[ref.position - 1], None
        return None, "I don't have that position in the matching results. Which listing number do you mean?"
    if len(choices) == 1:
        return choices[0], None
    if choices:
        options = ", ".join(str(r["listing_id"]) for r in choices[:5])
        return None, f"Which listing do you mean? The matching listing numbers are {options}."
    return None, "Which car do you mean? Give me a listing number, or search for a make and model first."


def formatted_preference(key, value):
    if "budget" in key:
        return f"AED {value:,}"
    return str(value)


def memory_observation(conn, store, state, scope="all", *, current_message=""):
    """Return complete scoped saved facts separately from a bounded inquiry sample."""
    if scope == "all":
        preferences = dict(state["preferences"])
    else:
        preferences = store.stated_preferences(state["user_id"], state["session_id"], scope=scope)
    if scope in {"all", "current"}:
        preferences.update(state.get("_preference_updates", {}))
    preferences = {key: value for key, value in preferences.items() if value is not None}

    records = store.inquiries(state["user_id"], state["session_id"], scope=scope)
    if scope in {"all", "current"}:
        staged = [{**event, "session_id": state["session_id"],
                   "message": event.get("message", current_message)}
                  for event in reversed(state.get("_turn_events", []))]
        records = staged + records

    def source(record):
        text = record.get("message", "")
        recorded_context = record.get("shopping_context") or {}
        subject = record.get("shopping_subject") or record.get("subject") or recorded_context.get("subject")
        if subject in {"self", "other"}:
            basis = "recorded_event_subject"
        elif record.get("kind") == "like":
            # A like event is the saved personal-like action, not an inferred
            # preference from a search. Never replace it with today's subject.
            subject, basis = "self", "saved_personal_like"
        elif record.get("kind") in {"search", "retrieval"} and THIRD_PARTY.search(text):
            subject, basis = "other", "explicit_third_party_message"
        else:
            subject, basis = "unknown", "subject_not_recorded"
        return {"session_id": record.get("session_id"), "message": text[:500],
                "message_truncated": len(text) > 500, "subject": subject,
                "subject_evidence": basis}

    personal_likes = [record for record in records
                      if record.get("kind") == "like" and source(record)["subject"] != "other"]
    liked = {}
    unresolved_likes = 0
    for record in personal_likes:
        listing_id = record.get("listing_id")
        if not isinstance(listing_id, int) or isinstance(listing_id, bool):
            unresolved_likes += 1
            continue
        liked.setdefault(listing_id, record)
    liked_cars = []
    for listing_id, evidence in liked.items():
        row = get_listing(conn, listing_id)
        facts = inventory_observation([row])["cars"][0] if row else {"listing_id": listing_id}
        liked_cars.append({**facts, "available_in_inventory": row is not None, "source": source(evidence)})

    recent = []
    for record in records[:12]:
        entry = {key: record[key] for key in
                 ("kind", "listing_id", "shown_ids", "result_ids", "filters", "order", "attribute")
                 if key in record}
        recent.append({**entry, **source(record)})
    return {
        "scope": scope,
        "preferences": preferences,
        "preferences_complete": True,
        "liked_listing_ids": list(liked),
        "liked_cars": liked_cars,
        "liked_count": len(liked),
        "liked_complete": unresolved_likes == 0,
        "unresolved_like_events": unresolved_likes,
        "recent_inquiries": recent,
        "inquiry_count": len(records),
        "recent_inquiries_complete": len(records) <= len(recent),
        "coverage_note": ("Preferences and likes are separate scoped saved facts. Recent inquiries may be truncated; "
                          "their omission never means a saved preference or like is absent. An empty complete collection "
                          "means none recorded in this scope only. Searches, including third-party searches, do not prove personal likes."),
    }


def recall(conn, store, state, subject, scope="all"):
    prefs = state["preferences"]
    sentences = []
    if subject == "previous_budget":
        original = state.get("_original_preferences", {}).get("max_budget_aed")
        if original is not None and original != prefs.get("max_budget_aed"):
            return f"Your previous maximum budget was AED {original:,}."
        events = store.preference_history(state["user_id"], "max_budget_aed")
        values = [event["value"] for event in events]
        previous = next((v for v in values[1:] if v is not None and v != prefs.get("max_budget_aed")), None)
        return f"Your previous maximum budget was AED {previous:,}." if previous is not None else "I don't have an earlier maximum budget recorded."
    if subject in {"preferences", "budget", "all"}:
        period = {"all": "", "current": " in this chat", "last_visit": " in your last chat",
                  "previous": " in earlier chats"}[scope]
        if scope != "all":
            prefs = store.stated_preferences(state["user_id"], state["session_id"], scope=scope)
            if scope == "current":
                prefs.update(state.get("_preference_updates", {}))
                prefs = {k: v for k, v in prefs.items() if v is not None}
        use = [(k, v) for k, v in prefs.items() if subject != "budget" or "budget" in k]
        if use:
            for key, value in use:
                label = PREF_LABELS.get(key, key)
                fact = formatted_preference(key, value)
                if period:
                    sentences.append(f"You gave {fact} as your {label}{period}.")
                else:
                    sentences.append(f"Your {label} {'are' if key == 'needs' else 'is'} {fact}.")
        elif period:
            item = "a personal budget" if subject == "budget" else "a lasting preference"
            sentences.append(f"You didn't state {item}{period}.")
        elif subject == "budget":
            sentences.append("You haven't given me a personal budget yet. What price range in AED works for you?")
        else:
            sentences.append("You haven't shared any lasting preferences yet.")
    if subject in {"history", "liked", "all"}:
        records = store.inquiries(state["user_id"], state["session_id"], scope=scope,
                                  kind="like" if subject == "liked" else None)
        if scope in {"all", "current"}:
            staged = [{**event, "session_id": state["session_id"]} for event in reversed(state.get("_turn_events", []))
                      if subject != "liked" or event.get("kind") == "like"]
            records = staged + records
        found = next((i for i in records if _inquiry_ids(i)), None)
        if found and subject == "liked":
            likes = {}
            for event in records:
                likes.setdefault(event["listing_id"], event)
            shown = []
            for listing_id in list(likes)[:5]:
                row = get_listing(conn, listing_id)
                if row:
                    when = "this chat" if likes[listing_id]["session_id"] == state["session_id"] else "a previous chat"
                    shown.append(f"the {car_name(row)} in {when}")
            if shown:
                sentences.append("You liked " + ", and ".join(shown) + ".")
            if len(likes) > 5:
                sentences.append(f"You have {len(likes)} liked listings in that history.")
            elif not shown:
                sentences.append("Your saved likes refer to listings that are no longer in the inventory.")
        elif found:
            row = get_listing(conn, _inquiry_ids(found)[0])
            if row:
                phrase = {"like": "You liked", "search": "I showed you", "detail": "You asked about",
                          "booking": "You booked a visit for"}.get(found.get("kind"), "We discussed")
                timing = "in this chat" if found["session_id"] == state["session_id"] else "in a previous chat"
                sentences.append(f"{phrase} the {car_name(row)} {timing}.")
        last_search = next((i for i in records if i.get("kind") == "search"), None)
        if last_search and subject != "liked":
            terms = last_search.get("filters", {})
            text = " ".join(str(terms[k]) for k in ("color", "make", "model", "body_style") if terms.get(k))
            search_text = f"Your last search was for {text or 'cars'}"
            if "max_price" in terms:
                search_text += f" {'under' if terms.get('max_exclusive') else 'up to'} AED {terms['max_price']:,}"
            sentences.append(search_text + ".")
        elif not found:
            period = {"previous": "in your earlier chats", "last_visit": "in your last chat",
                      "current": "in this chat", "all": "for you"}[scope]
            sentences.append(f"I have no {'liked car' if subject == 'liked' else 'car inquiry'} recorded {period}.")
    return " ".join(sentences)


def listing_fact(planner, message, row, attribute):
    name = car_name(row)
    if attribute in {"price", "mileage"}:
        key = "price_aed" if attribute == "price" else "mileage_km"
        value = row[key]
        if value is None:
            return f"The listing for the {name} doesn't state a verified {'total asking price' if attribute == 'price' else 'odometer reading'}."
        fact = f"AED {value:,.0f}" if attribute == "price" else f"{value:,} km"
        return f"The {attribute} listed for the {name} is {fact}."
    if attribute in {"year", "make", "model", "trim", "color", "body_style"}:
        value = row[attribute]
        label = attribute.replace("_", " ")
        if value is None:
            return f"The listing for the {name} doesn't specify its {label}."
        return f"The listed {label} for the {name} is {value}."
    if attribute == "overview":
        price = f"AED {row['price_aed']:,.0f}" if row["price_aed"] is not None else "Not stated clearly"
        mileage = f"{row['mileage_km']:,} km" if row["mileage_km"] is not None else "Not stated clearly"
        parts = [f"Here's what the listing tells us about the **{name}**:",
                 f"- Asking price: **{price}**", f"- Mileage: **{mileage}**",
                 f"- Listed variant: {row['trim'] or 'Not specified'}"]
        for key in ("body_style", "color"):
            if row.get(key):
                parts.append(f"- {key.replace('_', ' ').capitalize()}: {row[key]}")
        title = row["title"].strip()
        if title and not re.search(r"https?://|www\.|@[\w.-]+|(?:\+?971|\b05\d)[\d\s()-]{6,}", title, re.I):
            parts.append(f'\nThe seller describes it as: “{title}”')
        parts.append("\nSeller descriptions aren't an inspection report. Check the service records, accident history and mechanical condition before deciding. I can explain the variant, check another detail, or help arrange a demo viewing.")
        return "\n".join(parts)
    excerpt = None
    if attribute != "warranty" or re.search(r"warrant|guarantee|ضمان", row["description"] + row["title"], re.I):
        excerpt = planner.source_excerpt(message, row)
    if excerpt:
        return f'For the {name}, the seller states: “{excerpt}”'
    unknown = "whether it has a warranty" if attribute == "warranty" else "that detail"
    return f"The listing for the {name} doesn't specify {unknown}."


def lead_update(writes, filters, message, state, *, replace_search=False):
    """Patch the current lead. A price-only inquiry has no stated vehicle needs."""
    lead = {}
    for prefix in ("min", "max"):
        field, pref = prefix + "_price", prefix + "_budget_aed"
        if replace_search:
            lead[f"budget_{prefix}_aed"] = filters.get(field)
        elif field in filters or pref in writes:
            lead[f"budget_{prefix}_aed"] = filters.get(field, writes.get(pref))
    stored = state.get("lead", {})
    criteria = {} if replace_search else dict(stored.get("criteria", {}))
    if replace_search and not THIRD_PARTY.search(message) and stored.get("criteria", {}).get("needs"):
        criteria["needs"] = stored["criteria"]["needs"]
    if not replace_search and not criteria and stored.get("needs"):
        criteria["needs"] = stored["needs"]  # Leads saved before structured criteria.
    for key in ("color", "make", "model", "trim", "body_style", "needs", "query",
                "min_year", "max_year", "min_mileage", "max_mileage"):
        if key not in filters and key not in writes:
            continue
        value = filters.get(key, writes.get(key))
        if value is None:
            criteria.pop(key, None)
        else:
            criteria[key] = value
    if replace_search or criteria != stored.get("criteria", {}):
        limits = {"min_year": "year from", "max_year": "year through",
                  "min_mileage": "km from", "max_mileage": "km up to"}
        parts = [f"{limits[k]} {v}" if k in limits else str(v) for k, v in criteria.items()]
        lead.update(criteria=criteria, needs=", ".join(dict.fromkeys(parts)) or None)
    return lead


class Assistant:
    def __init__(self, store, planner):
        self.store = store
        self.planner = planner
        self.booking_service = BookingService(store)

    def respond(self, user_id, session_id, request_id, message):
        prior = self.store.replay(user_id, session_id, request_id, message)
        if prior is not None:
            return prior
        original = self.store.state(user_id, session_id)
        proposal = current_proposal(original)
        previous = original["recent_turns"][-1] if original["recent_turns"] else {}
        if (AFFIRMATIVE.fullmatch(message) and proposal
            and (previous.get("booking_proposal") or {}).get("proposal_id") == proposal["proposal_id"]):
            return self.confirm_booking(user_id, session_id, request_id, proposal["proposal_id"], _message=message)
        state = copy.deepcopy(original)
        native = hasattr(self.planner, "run_turn")
        state["agent_turn"] = native
        state["booking_outcome"] = None
        price_constraints = extract_price_constraints(message)
        state["requested_price_constraints"] = {"bounds": price_constraints.bounds,
            "evidence": list(price_constraints.evidence), "ambiguous": price_constraints.ambiguous,
            "reason": price_constraints.reason}
        state["_original_preferences"] = dict(state["preferences"])
        state["authorized_ids"] = trusted_listing_ids(state)
        prior_reply = state["recent_turns"][-1]["reply"] if state["recent_turns"] else ""
        mutations = {"preferences": {}}
        state["appointment_input"] = normalize_appointment_fields(message)
        automatic_writes = {}
        state["_preference_updates"] = mutations["preferences"]
        events, trace = [], []
        state["_turn_events"] = events
        response = {"request_id": request_id, "reply": "", "cars": [], "booking": None}
        presentation = {}
        context = state.get("shopping_context") or {"subject": "self"}
        if THIRD_PARTY.search(message):
            context = {"subject": "other", "evidence": message[:500]}
        elif re.search(r"\b(?:for (?:me|myself)|my own|back to my|for my car)\b", message, re.I):
            context = {"subject": "self"}
        state["shopping_context"] = context
        if context != original.get("shopping_context"):
            mutations["shopping_context"] = context

        def refresh_facts():
            with closing(sqlite3.connect(self.store.db_path)) as conn:
                rows = [row for lid in state["shown_ids"] if (row := get_listing(conn, lid))]
                state["shown_cars"] = inventory_observation(rows, detailed=True)["cars"]
                selected = get_listing(conn, state["selected_id"]) if state["selected_id"] else None
                state["selected_car"] = inventory_observation([selected], detailed=True)["cars"][0] if selected else None

        refresh_facts()

        def execute(name, action):
            # All tool effects live in this workspace until final prose is ready.
            # SQLite is never locked while waiting for the model.
            presents = name in {"show_results", "next_inventory_page"} or (name == "search_inventory" and getattr(action, "display", True))
            if native and presentation and presents:
                trace.append({"tool": name, "status": "presentation_limit", "listing_ids": []})
                return {"status": "presentation_limit", "presented_request": dict(presentation),
                        "reply": "This turn already has its one public result page, including any zero-match result. Do not replace the requested filters or redisplay alternatives without a new shopper request. You may inspect alternatives using search_inventory display=false and describe them accurately, then ask before displaying a different search."}
            requested_preferences = getattr(action, "preferences", [])
            proposed = [change for change in requested_preferences if change.key not in automatic_writes]
            writes = (supported_preferences(proposed, message, state["preferences"], prior_reply)
                      if name not in {"recall_shopper", "conversation", "cancel_booking", "next_inventory_page", "show_results", "inspect_results"} else {})
            if state["shopping_context"].get("subject") == "other" and not re.search(r"\b(?:I\b|I'm|my (?:budget|limit|preference|own))", message, re.I):
                writes = {}
            changes = {"preferences": writes}
            out = {"reply": "", "cars": [], "booking": None}
            with closing(sqlite3.connect(self.store.db_path)) as conn:
                self._execute(conn, state, message, name, action, writes, out, changes)
            if name == "update_preferences" and not writes and any(change.key in automatic_writes for change in requested_preferences):
                out.update(status="already_recorded", reply="The explicit preferences recognized from this message are already staged for saving with this turn.")
            is_presented_search = "pagination" in out and (name == "search_inventory" or
                (name == "show_results" and getattr(action, "mode", None) in {"latest", "previous_search"}))
            lead = (lead_update(writes, out.get("filters", {}), message, state,
                                replace_search=is_presented_search)
                    if is_presented_search or writes else {})
            if lead:
                mutations["lead"] = {**mutations.get("lead", {}), **lead}
                state["lead"] = {**state.get("lead", {}), **lead}
            mutations["preferences"].update(writes)
            state["preferences"].update(writes)
            state["preferences"] = {k: v for k, v in state["preferences"].items() if v is not None}
            if "inquiry" in changes:
                event = changes.pop("inquiry")
                events.append(event)
                if event.get("kind") == "search":
                    state["active_search"] = event
                    if event.get("shown_ids"):
                        state["last_results"] = event
            for key, value in changes.items():
                if key != "preferences":
                    mutations[key] = value
                    state[key] = value
            if "retrieved_search" in out:
                state["retrieved_search"] = out["retrieved_search"]
            state["authorized_ids"].update(trusted_listing_ids(out))
            state["authorized_ids"].update(trusted_listing_ids(state.get("pending_booking")))
            # Only presentation tools can replace the cards/cursor returned to the UI.
            if "pagination" in out:
                presentation.update(tool=name, filters=out.get("filters", {}), pagination=out["pagination"],
                                    listing_ids=[car["listing_id"] for car in out.get("cars", [])])
                for key in ("cars", "pagination", "total_matches", "filters"):
                    if key in out:
                        response[key] = out[key]
            if out.get("booking"):
                response["booking"] = out["booking"]
            refresh_facts()
            if out.get("booking_outcome"):
                state["booking_outcome"] = out["booking_outcome"]
                response["booking_outcome"] = out["booking_outcome"]
            if name == "prepare_booking":
                response["booking_proposal"] = out.get("booking_proposal")
            elif name == "cancel_booking":
                response["booking_proposal"] = None
            observation = {**out, "status": out.get("status", "ok"),
                "accepted_preferences": writes,
                "automatically_recognized_preferences": dict(automatic_writes) if state["shopping_context"].get("subject") == "self" else {},
                "preferences": state["preferences"] if state["shopping_context"].get("subject") == "self" else {},
                "pending_booking": state.get("pending_booking"), "shopping_context": state["shopping_context"],
                "lead_collection": lead_collection(state.get("lead")),
                "booking_outcome": state.get("booking_outcome")}
            trace.append({"tool": name, "status": observation["status"],
                          "listing_ids": [c["listing_id"] for c in out.get("cars", [])]})
            return observation

        if native:
            with closing(sqlite3.connect(self.store.db_path)) as conn:
                recognized = automatic_preferences(conn, message, state["preferences"], prior_reply)
            if recognized:
                accepted = execute("update_preferences", Save(preferences=recognized))
                automatic_writes.update(accepted["accepted_preferences"])
            state["recognized_preferences_this_turn"] = dict(automatic_writes)
            response["reply"] = self.planner.run_turn(message, state, execute)
        else:
            # Dependency injection for deterministic tool tests; same execution and commit path.
            name, action = self.planner.plan(message, state)
            observation = execute(name, action)
            response["reply"] = observation["reply"]
        response["tool_trace"] = trace
        if events:
            response["inquiry"] = events[-1]
            mutations.update(inquiry=events[-1], inquiries=events)
        return self.store.commit_turn(user_id, session_id, request_id, original["version"], message, response, **mutations)

    def current_booking_proposal(self, user_id, session_id):
        return current_proposal(self.store.state(user_id, session_id))

    def confirm_booking(self, user_id, session_id, request_id, proposal_id, *, _message=None):
        message = _message or "Confirm booking."
        prior = self.store.replay(user_id, session_id, request_id, message)
        if prior is not None:
            if prior.get("confirmed_proposal_id") != proposal_id:
                raise RequestConflict("This confirmation request ID belongs to a different proposal.")
            return prior
        state = self.store.state(user_id, session_id)
        try:
            booking, reply = self.booking_service.confirmation(state, proposal_id)
        except BookingError:
            # An identical retry may have committed between replay and state/
            # availability reads. Return its receipt before declaring the saved
            # proposal stale; Store.commit_turn covers the later race window.
            saved = self.store.replay(user_id, session_id, request_id, message)
            if saved is not None:
                if saved.get("confirmed_proposal_id") != proposal_id:
                    raise RequestConflict("This confirmation request ID belongs to a different proposal.")
                return saved
            raise
        event = {"kind": "booking", "listing_id": booking["listing_id"]}
        response = {"request_id": request_id, "reply": reply, "cars": [], "booking": None,
                    "confirmed_proposal_id": proposal_id,
                    "booking_proposal": None, "booking_outcome": {"status": "confirmed", "reply": reply},
                    "tool_trace": [{"tool": "confirm_booking", "status": "confirmed", "listing_ids": [booking["listing_id"]]}]}
        return self.store.commit_turn(user_id, session_id, request_id, state["version"], message, response,
                                      booking=booking, pending_booking=None, inquiry=event)

    def _execute(self, conn, state, message, name, action, writes, out, changes):
        if name == "inspect_results":
            if action.scope == "search":
                active = state.get("retrieved_search") or state.get("active_search") or {}
                ids = active.get("result_ids", active.get("shown_ids", []))
            else:
                ids = state["shown_ids"] or (state.get("last_results") or {}).get("shown_ids", [])
            rows = [row for lid in ids if (row := get_listing(conn, lid))]
            out.update(facts=inventory_observation(rows, detailed=len(rows) <= PAGE_SIZE),
                       status="found" if rows else "no_results",
                       reply="These are the verified facts for the requested set. Unknown measurements cannot be ranked or inferred.",
                       listing_ids=ids, scope=action.scope)
            return
        if name == "show_results" and action.mode != "next":
            if action.mode == "latest":
                snapshot = state.get("retrieved_search") or state.get("active_search")
                offset = 0
            else:
                snapshot = state.get("last_results")
                if action.mode == "previous_search" and snapshot:
                    history = list(reversed(state.get("_turn_events", []))) + self.store.inquiries(
                        state["user_id"], state["session_id"], scope="current", kind="search")
                    previous = next((event for event in history if event.get("shown_ids") and
                                     event.get("result_ids") != snapshot.get("result_ids")), None)
                    snapshot = previous or snapshot
                offset = max(0, (snapshot or {}).get("next_offset", len((snapshot or {}).get("shown_ids", [])))
                             - len((snapshot or {}).get("shown_ids", [])))
                if action.mode == "previous":
                    offset = max(0, offset - PAGE_SIZE)
            if not snapshot:
                out.update(status="no_results", reply="There are no previously displayed cars in this chat. Ask which cars the shopper would like to see.")
                return
            constraints = state.get("requested_price_constraints", {}) if state.get("agent_turn") else {}
            requested = constraints.get("bounds", {})
            if constraints.get("ambiguous") or any(snapshot.get("filters", {}).get(key, False if key.endswith("exclusive") else None) != value for key, value in requested.items()):
                out.update(status="needs_search", requested_price_constraints=constraints,
                           reply="This stored result set does not represent the shopper's explicit price request. Run a search with the supplied authoritative bounds, or clarify an ambiguous price request. No cards or search state were changed.")
                return
            self._search_page(conn, snapshot, offset, out, changes)
            if changes.get("shown_ids") == state["shown_ids"]:
                changes["selected_id"] = state["selected_id"]
            return
        if name == "conversation":
            if action.category in {"hello", "thanks", "help", "clarify"} and hasattr(self.planner, "converse"):
                out["reply"] = self.planner.converse(message, state, action.category) or CONVERSATION[action.category]
            else:
                out["reply"] = CONVERSATION[action.category]
            return
        if name == "cancel_booking":
            changes["pending_booking"] = None
            out["reply"] = ("I've stopped the unfinished booking. No appointment was reserved."
                            if state.get("pending_booking") else
                            "There isn't an unfinished booking in this chat. This demo cannot cancel confirmed appointments; they remain unchanged.")
            out.update(status="cancelled", booking_outcome={"status": "cancelled", "reply": out["reply"]})
            return
        if name == "compare_listings":
            rows = []
            for ref in action.cars:
                if (ref.listing_id is not None and not re.search(rf"(?<!\w){ref.listing_id}(?!\w)", message)
                    and not (state.get("agent_turn") and ref.listing_id in state.get("authorized_ids", set()))):
                    if state.get("agent_turn"):
                        out.update(status="needs_clarification", reply="That comparison includes a listing ID not grounded in this conversation's retrieved inventory. Retrieve the intended cars or ask which cars to compare; do not substitute the selected car.")
                        return
                    ref = ref.model_copy(update={"listing_id": None})
                row, clarification = resolve_car(conn, ref, state, self.store)
                if row is None:
                    out.update(reply=clarification, status="needs_clarification")
                    return
                if row["listing_id"] not in [r["listing_id"] for r in rows]:
                    rows.append(row)
            if len(rows) < 2:
                out.update(status="needs_clarification", reply="Which other car should I compare it with? Please name a listing or its position in the results.")
                return
            calculations = []
            for index, left in enumerate(rows):
                for right in rows[index + 1:]:
                    for key, unit in (("price_aed", "AED"), ("mileage_km", "km")):
                        known = left[key] is not None and right[key] is not None
                        difference = left[key] - right[key] if known else None
                        calculations.append({"field": key, "unit": unit,
                            "left_listing_id": left["listing_id"], "right_listing_id": right["listing_id"],
                            "left_value": left[key], "right_value": right[key],
                            "status": "known" if known else "unknown",
                            "difference_left_minus_right": difference,
                            "absolute_difference": abs(difference) if known else None,
                            "lower_listing_id": (left["listing_id"] if difference < 0 else right["listing_id"])
                                                if known and difference != 0 else None,
                            "missing_listing_ids": [row["listing_id"] for row in (left, right) if row[key] is None]})
            out.update(facts=inventory_observation(rows, detailed=True), comparisons=calculations,
                       comparison_note="Differences are backend-calculated from supplied inventory fields. Unknown comparisons have no numeric difference; seller descriptions remain unverified claims.")
            lines = ["| Car | Asking price | Mileage | Listed variant |", "|---|---:|---:|---|"]
            for row in rows:
                price = f"AED {row['price_aed']:,.0f}" if row["price_aed"] is not None else "Unknown"
                mileage = f"{row['mileage_km']:,} km" if row["mileage_km"] is not None else "Unknown"
                lines.append(f"| {car_name(row)} | {price} | {mileage} | {str(row['trim'] or 'Unknown').replace('|', '/')} |")
            notes = []
            for key, label, unit in (("price_aed", "asking price", "AED"), ("mileage_km", "mileage", "km")):
                known = [r for r in rows if r[key] is not None]
                if len(known) != len(rows):
                    missing = ", ".join(str(r["listing_id"]) for r in rows if r[key] is None)
                    notes.append(f"I can't fully compare {label}: it isn't verified for listing {missing}.")
                elif len(rows) == 2:
                    lower, higher = sorted(rows, key=lambda r: r[key])
                    difference = higher[key] - lower[key]
                    if difference:
                        amount = f"AED {difference:,.0f}" if unit == "AED" else f"{difference:,.0f} km"
                        notes.append(f"Listing {lower['listing_id']}'s {label} is {amount} lower than listing {higher['listing_id']}'s.")
                    else:
                        notes.append(f"Both listings have the same {label}.")
            notes.append("Price, age and mileage alone don't establish condition or value. Compare service records and inspection results before deciding; missing details remain unknown.")
            out["reply"] = "\n".join(lines) + "\n\n" + "\n\n".join(notes)
            return
        if name == "recall_shopper":
            scope = action.scope
            if scope == "current" and action.subject in {"budget", "preferences", "all"}:
                # "My current budget" refers to the active profile even in a new session.
                # Restrict to turns in this session only for a reference to this visit.
                this_visit = re.search(r"\b(?:this|current|our) (?:chat|session|conversation|visit)\b|\b(?:this time|today|here)\b", message, re.I)
                if not this_visit:
                    scope = "all"
            out["reply"] = recall(conn, self.store, state, action.subject, scope)
            out["memory"] = memory_observation(conn, self.store, state, scope, current_message=message)
            out["memory"]["requested_subject"] = action.subject
            if action.subject == "all" and out["memory"]["liked_listing_ids"]:
                labels = []
                for car in out["memory"]["liked_cars"]:
                    labels.append(car_name(car) if car["available_in_inventory"]
                                  else f"listing {car['listing_id']} (no longer in the inventory)")
                out["reply"] += " Your saved personal likes in this history are: " + "; ".join(labels) + "."
            return
        if name == "update_preferences":
            out["status"] = "saved" if writes else "not_saved"
            if writes:
                out["reply"] = " ".join(f"I've saved your {PREF_LABELS[k]} as {formatted_preference(k, v)}."
                                        if v is not None else f"I've cleared your {PREF_LABELS[k]}." for k, v in writes.items())
                prefs = {k: v for k, v in {**state["preferences"], **writes}.items() if v is not None}
                if not any("budget" in k for k in prefs):
                    out["reply"] += " What price range in AED works for you?"
                elif not any(k in prefs for k in ("make", "model", "needs", "color", "body_style")):
                    out["reply"] += " What kind of car or features do you need?"
            else:
                out["reply"] = "What budget or car preference would you like me to remember?"
            return
        if name == "search_inventory":
            subject = getattr(action, "subject", "unchanged")
            if subject in {"self", "other"}:
                changes["shopping_context"] = {"subject": subject}
            if getattr(action, "mode", "replace") == "refine":
                base = ((state.get("last_results") or state.get("active_search"))
                        if getattr(action, "refine_base", "latest") == "shown"
                        else (state.get("retrieved_search") or state.get("active_search"))) or {}
                old = base.get("filters", {})
                updated = action.filters.model_dump(exclude_unset=True)
                merged = dict(old)
                if updated.get("make") and str(updated["make"]).casefold() != str(old.get("make")).casefold():
                    merged.pop("model", None)
                    merged.pop("trim", None)
                if updated.get("model") and str(updated["model"]).casefold() != str(old.get("model")).casefold():
                    merged.pop("trim", None)
                for boundary in ("min", "max"):
                    if boundary + "_price" in updated:
                        merged.pop(boundary + "_exclusive", None)
                merged.update(updated)
                for key in getattr(action, "remove_filters", []):
                    merged.pop(key, None)
                    if key in {"min_price", "max_price"}:
                        merged.pop(key.split("_")[0] + "_exclusive", None)
                settings = {key: base[key] for key in ("order", "bookable_only")
                            if key in base and key not in action.model_fields_set}
                action = action.model_copy(update={"filters": type(action.filters).model_validate(merged), **settings})
            display = getattr(action, "display", True)
            constraints = state.get("requested_price_constraints", {}) if state.get("agent_turn") else {}
            if display and constraints.get("ambiguous"):
                out.update(status="needs_clarification", requested_price_constraints=constraints,
                           reply="Clarify the price condition before presenting a search. " + str(constraints.get("reason") or "The stated conditions are ambiguous."))
                return
            if display and constraints.get("bounds"):
                values = action.filters.model_dump(exclude_none=True)
                values.update(constraints["bounds"])
                action = action.model_copy(update={"filters": type(action.filters).model_validate(values)})
                out["requested_price_constraints"] = constraints
            rows, filters = search(conn, action)
            snapshot = {"kind": "search", "filters": filters, "order": action.order,
                        "bookable_only": action.bookable_only,
                        "result_ids": [r["listing_id"] for r in rows]}
            uncertain = unknown_candidates(conn, filters)
            snapshot["unverified_ids"] = [r["listing_id"] for r in uncertain]
            out.update(facts=inventory_observation(rows), retrieved_search=snapshot,
                       unverified_candidates=inventory_observation(uncertain),
                       status="found" if rows else "no_verified_matches")
            if display:
                self._search_page(conn, snapshot, 0, out, changes)
            else:
                changes["inquiry"] = {**snapshot, "kind": "retrieval"}
                out.update(filters=filters, total_matches=len(rows),
                           reply="Internal retrieval only; the displayed page, selected car, lead and pending booking have not changed.")
            if uncertain:
                out["reply"] += f"\n{len(uncertain)} additional candidate(s) have missing required data, so they cannot be verified against these filters. They are not confirmed matches."
            return
        if name == "next_inventory_page" or (name == "show_results" and action.mode == "next"):
            active = state.get("active_search")
            if active is None:
                out["reply"] = "There isn't an active search in this chat yet. What cars would you like to see?"
                return
            snapshot = {k: active[k] for k in ("kind", "filters", "order", "bookable_only", "result_ids") if k in active}
            constraints = state.get("requested_price_constraints", {}) if state.get("agent_turn") else {}
            if constraints.get("ambiguous") or any(snapshot.get("filters", {}).get(key, False if key.endswith("exclusive") else None) != value for key, value in constraints.get("bounds", {}).items()):
                out.update(status="needs_search", requested_price_constraints=constraints,
                           reply="The current search differs from the explicit price request. Search using the authoritative bounds before displaying another page. No search state changed.")
                return
            offset = active.get("next_offset", len(active.get("shown_ids", [])))
            if "result_ids" not in snapshot:
                snapshot.setdefault("order", "relevance")
                snapshot.setdefault("bookable_only", False)
                # Resume chats created before pagination was introduced. Their
                # displayed IDs come first so none is immediately repeated.
                rows, filters = search(conn, Search.model_validate({
                    "filters": active.get("filters", {}), "order": active.get("order", "relevance"),
                    "bookable_only": active.get("bookable_only", False)}))
                seen = active.get("shown_ids", [])
                snapshot.update(filters=filters, result_ids=seen + [r["listing_id"] for r in rows if r["listing_id"] not in seen])
            self._search_page(conn, snapshot, offset, out, changes, continuation=True)
            return
        ref = action.car
        if (ref.listing_id is not None and not re.search(rf"(?<!\w){ref.listing_id}(?!\w)", message)
            and ref.listing_id not in state.get("authorized_ids", set())):
            if state.get("agent_turn"):
                out.update(status="needs_clarification", reply="That listing ID is not grounded in this conversation's retrieved inventory. Retrieve the intended car or ask which car the shopper means; do not substitute the selected car.")
                return
            # A planner may copy an old ID; resolve a pronoun from the server's reference state.
            ref = ref.model_copy(update={"listing_id": None})
        if name == "prepare_booking" and not ref.model_dump(exclude_none=True, exclude_defaults=True):
            pending = draft_from_state(state) or {}
            if pending.get("listing_id"):
                ref = ref.model_copy(update={"listing_id": pending["listing_id"]})
        if name == "explain_vehicle":
            explicit_ref = bool(ref.model_dump(exclude_none=True, exclude_defaults=True))
            has_focus = state.get("selected_id") or len(state.get("shown_ids", [])) == 1
            row, clarification = resolve_car(conn, ref, state, self.store) if explicit_ref or has_focus else (None, None)
            if row is None and explicit_ref:
                out["reply"] = clarification
                return
            out["reply"] = self.planner.explain(message, [row] if row else [], state)
            if row:
                changes.update(selected_id=row["listing_id"], inquiry={"kind": "detail", "listing_id": row["listing_id"], "attribute": "explanation"})
            return
        row, clarification = resolve_car(conn, ref, state, self.store)
        if row is None:
            if name == "prepare_booking":
                self._prepare_booking(action, None, state, message, out, changes,
                                      unresolved_car=bool(ref.model_dump(exclude_none=True, exclude_defaults=True)))
                if clarification:
                    out["reply"] = clarification
            else:
                out.update(reply=clarification, status="needs_clarification")
            return
        listing_id = row["listing_id"]
        # Reading evidence for a comparison is not a change of user focus.
        if name != "available_viewings" and (name != "read_listing" or not state.get("agent_turn") or getattr(action, "focus", False)):
            changes["selected_id"] = listing_id
        if name == "read_listing":
            out["facts"] = inventory_observation([row], detailed=True)
            attributes = list(dict.fromkeys(action.attributes or [action.attribute]))
            if state.get("agent_turn"):
                out["reply"] = "Use the supplied structured fields for exact facts. Seller title and description are unverified claims; quote them faithfully with qualifiers. Missing details stay unknown."
            else:
                out["reply"] = "\n\n".join(listing_fact(self.planner,
                    f"What is the {attribute}?" if len(attributes) > 1 else message, row, attribute)
                    for attribute in attributes)
            changes["inquiry"] = {"kind": "detail", "listing_id": listing_id, "attribute": action.attribute}
        elif name == "like_listing":
            personal_like = re.search(r"\b(?:I\s+(?:really\s+)?(?:like|love|prefer)|my\s+favo[u]?rite|remember\s+(?:it|this|that)\s+for\s+me)\b", message, re.I)
            if ((THIRD_PARTY.search(message) or state.get("shopping_context", {}).get("subject") == "other")
                and not personal_like):
                out.update(status="not_saved", reply="This interest belongs to someone else. It has not been saved as the shopper's personal like.")
                return
            out["reply"] = f"I'll remember that you liked the {car_name(row)}."
            changes["inquiry"] = {"kind": "like", "listing_id": listing_id}
        elif name == "prepare_booking":
            self._prepare_booking(action, row, state, message, out, changes)
        elif name == "available_viewings":
            # Availability is an observation, never a change to a saved draft.
            # Keep its retrieved ID in the audit history so a date-only answer
            # can refer to this same car without changing selected focus.
            changes["inquiry"] = {"kind": "detail", "listing_id": listing_id, "attribute": "availability"}
            out["listing_id"] = listing_id
            normalized = normalize_appointment_fields(message)
            day = normalized.get("day") or action.day
            if normalized.get("error") or not day:
                out.update(status="needs_clarification", reply=normalized.get("error") or "Which date would you like to check?")
                return
            try:
                slots = self.store.available_slots(listing_id, day)
                out.update(status="available" if slots else "unavailable", available_slots=slots,
                           reply=f"Available starts for the {car_name(row)} on {day}, Dubai time: " +
                           (", ".join(datetime.fromisoformat(slot).strftime("%H:%M") for slot in slots) or "none") + ". No appointment has been reserved.")
            except BookingError as exc:
                out.update(status="invalid_request", reply=str(exc))

    @staticmethod
    def _search_page(conn, snapshot, offset, out, changes, continuation=False):
        result_ids = snapshot["result_ids"]
        total = len(result_ids)
        end = min(offset + PAGE_SIZE, total)
        page_ids = result_ids[offset:end]
        shown = [row for listing_id in page_ids if (row := get_listing(conn, listing_id))]
        ids = [r["listing_id"] for r in shown]
        out.update(filters=snapshot.get("filters", {}), cars=[car_card(r) for r in shown],
                   total_matches=total,
                   pagination={"start": offset + 1 if page_ids else 0, "end": end if page_ids else 0,
                               "total": total, "has_more": end < total})
        if continuation and offset >= total:
            out["reply"] = "There are no more matching listings in this search. Would you like to change the filters?"
            return  # Keep the last displayed page and selection available for follow-ups.
        changes["inquiry"] = {**snapshot, "kind": "search", "shown_ids": ids, "next_offset": end}
        if shown:
            changes.update(shown_ids=ids, selected_id=ids[0] if len(ids) == 1 else None)
        if shown:
            count = (f"Showing results {offset + 1}–{end} of {total} matching listings."
                     if continuation or total > PAGE_SIZE else
                     f"I found {total} matching listing{'s' if total != 1 else ''}.")
            lines = []
            for i, row in enumerate(shown, 1):
                price = f"AED {row['price_aed']:,.0f}" if row["price_aed"] is not None else "asking price not verified"
                lines.append(f"{i}. {car_name(row)} — {price}")
            out["reply"] = count + "\n" + "\n".join(lines)
            if end < total:
                out["reply"] += '\nSay "show me more" to see the next results.'
            elif continuation:
                out["reply"] += "\nYou've reached the end of this search."
        elif page_ids:
            out["reply"] = "These saved results are no longer in the inventory. " + (
                'Say "show me more" to continue.' if end < total else "You've reached the end of this search.")
        else:
            out["reply"] = "No listings can be verified against all of these criteria. The previously displayed cars remain available to discuss or show again."

    def _prepare_booking(self, action, row, state, message, out, changes, *, unresolved_car=False):
        result = self.booking_service.prepare(state, action.model_dump(), row, message,
                                              unresolved_car=unresolved_car)
        changes["pending_booking"] = result["draft"]
        out.update(status=result["status"], reply=result["reply"], booking_proposal=result["proposal"],
                   booking_outcome=result["outcome"], pending_booking=result["draft"])
