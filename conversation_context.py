"""Transport complete recent dialogue separately from authoritative app state."""

import copy
import json

from google.genai import types


def dialogue_contents(message, context):
    """Keep conversational roles and wording intact; action records remain data.

    The caller controls history scope (including third-party shopping isolation).
    This function does not infer intent, summarize text, or mutate memory.
    """
    current = copy.deepcopy(context)
    recent = current.pop("recent_turns", [])
    contents = []
    action_records = []
    for index, turn in enumerate(recent):
        shopper = turn.get("shopper", "")
        reply = turn.get("reply", "")
        if shopper:
            contents.append(types.Content(role="user", parts=[types.Part(text=shopper)]))
        if reply:
            contents.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        if any(turn.get(key) is not None for key in ("booking_outcome", "booking_receipt", "booking_proposal")):
            action_records.append({"recent_turn_index": index,
                                   "booking_outcome": turn.get("booking_outcome"),
                                   "booking_receipt": turn.get("booking_receipt"),
                                   "booking_proposal": turn.get("booking_proposal")})
    current["recent_action_records"] = action_records
    payload = json.dumps({"context": current, "shopper_message": message},
                         ensure_ascii=False, allow_nan=False)
    contents.append(types.Content(role="user", parts=[types.Part(text=payload)]))
    return contents
