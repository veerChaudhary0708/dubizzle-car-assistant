"""Native tool orchestration without credentials, a network, or real side effects."""
from collections import deque
import json

from google.genai import types
import pytest

from planner import GeminiPlanner, MAX_TOOL_CALLS, MAX_TOOL_ROUNDS, ModelUnavailable, NATIVE_TOOLS, Search


@pytest.fixture
def state():
    return {
        "shown_ids": [20], "selected_id": 20,
        "preferences": {"max_budget_aed": 30000, "needs": "child seats"},
        "lead": {"needs": "child seats"},
        "pending_booking": None,
        "active_search": {"filters": {"make": "Ford"}, "next_offset": 1},
        "shown_cars": [{"listing_id": 20, "price_aed": 21500, "mileage_km": 164000}],
        "selected_car": {"listing_id": 20, "price_aed": 21500, "mileage_km": 164000},
        "last_results": [{"listing_id": 20, "price_aed": 21500}],
        "shopping_context": {"subject": "self"},
        "recent_turns": [],
    }


def tool(name, arguments=None, identifier=None, signature=None):
    return types.Part(function_call=types.FunctionCall(name=name, args=arguments or {}, id=identifier),
                      thought_signature=signature)


def response(*parts):
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))])


class FakeSDK:
    def __init__(self, monkeypatch, responses):
        self.responses = deque(responses)
        self.requests = []
        self.planner = object.__new__(GeminiPlanner)
        self.planner.agent_tool = types.Tool()
        monkeypatch.setattr(self.planner, "_generate", self.generate)

    def generate(self, contents, config):
        self.requests.append((contents if isinstance(contents, str) else list(contents), config))
        assert self.responses, "Unexpected extra Gemini request"
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def modes(self):
        return [request[1].tool_config.function_calling_config.mode.value
                if request[1].tool_config else "TEXT" for request in self.requests]


def test_observations_feed_next_model_round_and_full_signed_content_is_retained(monkeypatch, state):
    planned = response(types.Part(text="Let me check."),
                       tool("search_inventory", {"filters": {"make": "Nissan"}}, "search-1", b"signed-content"))
    sdk = FakeSDK(monkeypatch, [planned,
        response(tool("inspect_results", {"scope": "search"}, "inspect-1")),
        response(types.Part(text="The five Nissan listings do not state their odometer readings."))])
    executed = []

    def execute(name, action):
        executed.append((name, action))
        if name == "search_inventory":
            assert isinstance(action, Search)
            return {"status": "ok", "total": 5, "listing_ids": [11, 23, 41, 67, 78]}
        return {"status": "ok", "known_mileage_count": 0, "unknown_mileage_count": 5}

    reply = sdk.planner.run_turn("Find Nissan cars and tell me their mileage.", state, execute)
    assert "five Nissan" in reply and "Let me check" not in reply
    assert [name for name, _ in executed] == ["search_inventory", "inspect_results"]
    second_contents = sdk.requests[1][0]
    assert second_contents[1] is planned.candidates[0].content
    assert second_contents[1].parts[1].thought_signature == b"signed-content"
    observation = second_contents[2]
    assert observation.role == "user"
    assert observation.parts[0].function_response.id == "search-1"
    assert observation.parts[0].function_response.response["total"] == 5
    last_observation = sdk.requests[2][0][-1].parts[0].function_response.response
    assert last_observation["unknown_mileage_count"] == 5
    contents = sdk.requests[0][0]
    context = json.loads(contents[-1].parts[0].text)["context"]
    assert context["shown_cars"][0]["mileage_km"] == 164000
    assert context["selected_car"]["price_aed"] == 21500 and context["last_results"]
    assert sdk.modes == ["AUTO", "AUTO", "AUTO"]


def test_mixed_intent_executes_each_validated_tool_and_returns_both_observations(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(
        tool("update_preferences", {"preferences": [{"key": "make", "value": "Nissan", "evidence": "I am interested in Nissan"}]}, "save"),
        tool("search_inventory", {"filters": {"make": "Nissan"}, "subject": "self"}, "find")),
        response(types.Part(text="I've remembered your Nissan interest and found five listings."))])
    names = []
    def execute(name, action):
        names.append(name)
        return {"status": "ok", "accepted": name == "update_preferences", "total": 5}
    sdk.planner.run_turn("I am interested in Nissan. Show me the options.", state, execute)
    assert names == ["update_preferences", "search_inventory"]
    parts = sdk.requests[1][0][-1].parts
    assert [part.function_response.id for part in parts] == ["save", "find"]


def test_duplicate_tool_call_uses_cached_observation_without_duplicate_execution(monkeypatch, state):
    args = {"car": {"listing_id": 20}, "day": "2030-11-11", "time": "10:00", "kind": "test_drive"}
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", args, "first")),
        response(tool("prepare_booking", args, "retry")), response(types.Part(text="Please confirm this appointment proposal."))])
    executed = []
    def execute(name, action):
        executed.append(name)
        return {"status": "awaiting_confirmation", "booking_proposal": {"listing_id": 20, "proposal_id": "proposal-one"}}
    sdk.planner.run_turn("Book this test drive.", state, execute)
    assert executed == ["prepare_booking"]
    assert sdk.requests[2][0][-1].parts[0].function_response.id == "retry"
    assert sdk.requests[2][0][-1].parts[0].function_response.response["status"] == "awaiting_confirmation"


def test_cancel_invalidates_proposal_cache_but_immediate_retry_stays_deduplicated(monkeypatch, state):
    args = {"car": {"listing_id": 20}, "day": "2030-11-11", "time": "10:00", "kind": "test_drive"}
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", args, "first"), tool("cancel_booking", {}, "cancel"),
        tool("prepare_booking", args, "new-booking"), tool("prepare_booking", args, "retry-new")),
        response(types.Part(text="The final booking is prepared."))])
    executed, preparations = [], []
    def execute(name, action):
        executed.append(name)
        if name == "prepare_booking":
            preparations.append(len(preparations) + 1)
            return {"status": "awaiting_confirmation", "preparation": preparations[-1]}
        return {"status": "cancelled"}
    sdk.planner.run_turn("Make the final requested reservation.", state, execute)
    assert executed == ["prepare_booking", "cancel_booking", "prepare_booking"]
    observations = [part.function_response.response for part in sdk.requests[-1][0][-1].parts]
    assert observations[0]["preparation"] == 1
    assert observations[2]["preparation"] == observations[3]["preparation"] == 2


def test_intervening_reads_do_not_repeat_a_prepared_mutation(monkeypatch, state):
    args = {"car": {"listing_id": 20}, "day": "2030-11-11", "time": "10:00", "kind": "test_drive"}
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", args),
        tool("read_listing", {"attribute": "mileage"}), tool("inspect_results", {}),
        tool("prepare_booking", args)), response(types.Part(text="Your appointment remains prepared."))])
    executed = []
    sdk.planner.run_turn("Book it and check the mileage.", state,
                         lambda name, action: executed.append(name) or {"status": "ok"})
    assert executed == ["prepare_booking", "read_listing", "inspect_results"]


def test_repeated_search_after_different_search_runs_again(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(
        tool("search_inventory", {"filters": {"make": "Ford"}}),
        tool("search_inventory", {"filters": {"make": "Nissan"}}),
        tool("search_inventory", {"filters": {"make": "Ford"}})),
        response(types.Part(text="Ford results are displayed again."))])
    displayed = []
    sdk.planner.run_turn("End with the Ford results.", state,
                         lambda name, action: displayed.append(action.filters.make) or {"status": "ok"})
    assert displayed == ["Ford", "Nissan", "Ford"]


def test_focus_and_refinement_target_are_explicit_validated_tool_arguments(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(
        tool("read_listing", {"car": {"listing_id": 11}, "attribute": "overview"}),
        tool("read_listing", {"car": {"position": 2}, "attribute": "overview", "focus": True}),
        tool("search_inventory", {"filters": {"max_price": 30000}, "mode": "refine", "refine_base": "shown"})),
        response(types.Part(text="I kept the lookup separate, focused the chosen car, and refined the displayed set."))])
    actions = []
    sdk.planner.run_turn("Look up an alternative, focus the second car, and refine those displayed results.", state,
                         lambda name, action: actions.append((name, action)) or {"status": "ok"})
    assert actions[0][1].focus is False and actions[1][1].focus is True
    assert actions[2][1].refine_base == "shown"


def test_persisted_unverified_candidates_are_present_in_compact_context(monkeypatch, state):
    state["retrieved_search"] = {"filters": {"make": "Nissan", "max_price": 25000},
                                 "result_ids": [], "unverified_ids": [11, 23, 41, 67, 78]}
    state["authorized_ids"] = {11, 20}  # Internal sets must never enter JSON context.
    sdk = FakeSDK(monkeypatch, [response(types.Part(text="Their prices are unknown."))])
    sdk.planner.run_turn("What was missing from those candidates?", state, lambda *args: {})
    context = json.loads(sdk.requests[0][0][0].parts[0].text)["context"]
    assert context["retrieved_search"]["unverified_ids"] == [11, 23, 41, 67, 78]
    assert context["retrieved_search"]["count"] == 0 and context["retrieved_search"]["unverified_count"] == 5
    assert "authorized_ids" not in context


def test_search_change_invalidates_old_read_observation(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(tool("inspect_results", {"scope": "shown"})),
        response(tool("search_inventory", {"filters": {"make": "Nissan"}})),
        response(tool("inspect_results", {"scope": "shown"})),
        response(types.Part(text="The display now contains Nissan cars."))])
    shown, readings = [20], []
    def execute(name, action):
        if name == "search_inventory":
            shown[:] = [11, 23]
        else:
            readings.append(list(shown))
        return {"status": "ok", "shown_ids": list(shown)}
    sdk.planner.run_turn("Check these cars, then find Nissan cars.", state, execute)
    assert readings == [[20], [11, 23]]
    assert json.loads(sdk.requests[-1][0])["observations"][-1]["result"]["shown_ids"] == [11, 23]


def test_invalid_and_undeclared_tools_are_observations_not_executions(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(
        tool("read_listing", {"attribute": "invented_field", "secret": "not echoed"}, "invalid"),
        tool("conversation", {"category": "hello"}, "legacy")),
        response(tool("read_listing", {"attribute": "mileage"}, "valid")),
        response(types.Part(text="The odometer reads 164,000 km."))])
    executed = []
    def execute(name, action):
        executed.append(name)
        return {"status": "ok", "mileage_km": 164000}
    sdk.planner.run_turn("What is its mileage?", state, execute)
    assert executed == ["read_listing"]
    results = [part.function_response.response for part in sdk.requests[1][0][-1].parts]
    assert [result["status"] for result in results] == ["invalid_arguments", "unknown_tool"]
    assert "not echoed" not in json.dumps(results)


def test_round_limit_forces_final_answer_without_more_tools(monkeypatch, state):
    rounds = [response(tool("read_listing", {"attribute": "year", "car": {"listing_id": i + 1}})) for i in range(MAX_TOOL_ROUNDS)]
    sdk = FakeSDK(monkeypatch, [*rounds, response(types.Part(text="Here is what I could verify."))])
    executed = []
    sdk.planner.run_turn("Inspect these cars.", state, lambda name, action: executed.append(name) or {"status": "ok"})
    assert len(executed) == MAX_TOOL_ROUNDS
    assert sdk.modes == ["AUTO"] * MAX_TOOL_ROUNDS + ["TEXT"]
    assert sdk.requests[-1][1].tools is None and sdk.requests[-1][1].tool_config is None


def test_call_limit_returns_unexecuted_status_and_forces_composition(monkeypatch, state):
    calls = [tool("read_listing", {"attribute": "year", "car": {"listing_id": i + 1}}, str(i)) for i in range(MAX_TOOL_CALLS + 2)]
    sdk = FakeSDK(monkeypatch, [response(*calls), response(types.Part(text="I checked the first eight cars."))])
    executed = []
    sdk.planner.run_turn("Inspect ten cars.", state, lambda name, action: executed.append(action.car.listing_id) or {"status": "ok"})
    assert executed == list(range(1, MAX_TOOL_CALLS + 1))
    results = json.loads(sdk.requests[1][0])["observations"]
    assert len(results) == MAX_TOOL_CALLS + 2
    assert all(entry["result"]["status"] == "tool_limit" for entry in results[MAX_TOOL_CALLS:])
    assert sdk.modes == ["AUTO", "TEXT"]


def test_natural_answer_needs_no_tool_and_does_not_expose_thought_text(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(types.Part(text="private reasoning", thought=True),
                                       types.Part(text="Hello! What car would you like to explore?"))])
    def forbidden(*args):
        raise AssertionError("A greeting needs no backend execution")
    assert sdk.planner.run_turn("Hello", state, forbidden) == "Hello! What car would you like to explore?"
    assert sdk.modes == ["AUTO"]


def test_persistent_other_subject_keeps_other_followup_without_personal_constraints(monkeypatch, state):
    state["shopping_context"] = {"subject": "other"}
    state["active_search"] = {"filters": {"make": "Mazda", "max_price": 40000}, "next_offset": 1}
    state["recent_turns"] = [
        {"message": "My budget is 30000 and I need child seats.", "reply": "Saved."},
        {"message": "For my brother, find Mazda cars under40000.", "reply": "Found one."},
    ]
    sdk = FakeSDK(monkeypatch, [response(types.Part(text="I'll keep this search for your brother."))])
    sdk.planner.run_turn("Under35000 instead.", state, lambda *args: {})
    contents = sdk.requests[0][0]
    context = json.loads(contents[-1].parts[0].text)["context"]
    assert context["preferences"] == {} and context["lead"] == {}
    assert context["shopping_context"]["subject"] == "other"
    assert context["active_search"]["filters"]["make"] == "Mazda"
    assert [item.role for item in contents[:-1]] == ["user", "model"]
    assert "brother" in contents[0].parts[0].text
    assert "child seats" not in " ".join(part.text or "" for item in contents for part in item.parts)
    assert state["preferences"]["max_budget_aed"] == 30000


def test_explicit_return_to_self_restores_profile_and_drops_other_search(monkeypatch, state):
    state["shopping_context"] = {"subject": "other"}
    sdk = FakeSDK(monkeypatch, [response(types.Part(text="Let's return to your own search."))])
    sdk.planner.run_turn("Now find a car for me instead.", state, lambda *args: {})
    context = json.loads(sdk.requests[0][0][0].parts[0].text)["context"]
    assert context["shopping_context"]["subject"] == "self"
    assert context["preferences"]["max_budget_aed"] == 30000
    assert context["active_search"] is None and context["lead"] == {}


def test_malformed_text_only_final_cannot_execute_more_tools(monkeypatch, state, caplog):
    rounds = [response(tool("inspect_results", {"scope": "shown"})) for _ in range(MAX_TOOL_ROUNDS + 1)]
    sdk = FakeSDK(monkeypatch, rounds)
    executed = []
    result = sdk.planner.run_turn("Keep checking.", state, lambda name, action: executed.append(name) or {"status": "ok"})
    assert "couldn't finish" in result
    assert executed == ["inspect_results"]
    assert sdk.modes[-1] == "TEXT"
    assert "final_composition_returned_tools" in caplog.text


def test_native_tools_expose_data_and_actions_not_legacy_answer_categories():
    assert {"inspect_results", "show_results", "search_inventory", "update_preferences", "compare_listings"} <= set(NATIVE_TOOLS)
    assert not {"conversation", "explain_vehicle", "next_inventory_page"} & set(NATIVE_TOOLS)


@pytest.mark.parametrize("writing_tool, arguments", [
    ("read_listing", {"attribute": "mileage"}),
    ("compare_listings", {"cars": [{"listing_id": 20}, {"listing_id": 12}]}),
])
def test_preferences_on_fact_tools_invalidate_prior_read_cache(monkeypatch, state, writing_tool, arguments):
    read = {"attribute": "year", "car": {"listing_id": 20}}
    arguments = {**arguments, "preferences": [
        {"key": "needs", "value": "room for child seats", "evidence": "I need room for child seats"}]}
    sdk = FakeSDK(monkeypatch, [response(tool("read_listing", read), tool(writing_tool, arguments),
                                         tool("read_listing", read)),
                               response(types.Part(text="Your preference is saved and the facts are current."))])
    observations, saved = [], []

    def execute(name, action):
        if action.preferences:
            saved.append(action.preferences[0].value)
        result = {"status": "ok", "saved_needs": list(saved)}
        observations.append((name, result))
        return result

    sdk.planner.run_turn("I need room for child seats. Check these cars.", state, execute)
    assert [name for name, _ in observations] == ["read_listing", writing_tool, "read_listing"]
    responses = sdk.requests[-1][0][-1].parts
    assert responses[0].function_response.response["saved_needs"] == []
    assert responses[-1].function_response.response["saved_needs"] == ["room for child seats"]


def test_rejected_proposal_observation_reaches_model_for_natural_clarification(monkeypatch, state):
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", {"car": {"listing_id": 20}, "time": "10:00", "kind": "test_drive"})),
                               response(types.Part(text="Which date would you prefer for the Mustang test drive?"))])

    def rejected(name, action):
        state["booking_outcome"] = {"status": "rejected", "reply": "No appointment was created. Please choose a date."}
        return {"status": "needs_clarification", "booking_outcome": state["booking_outcome"]}

    reply = sdk.planner.run_turn("Book it at10am.", state, rejected)
    assert reply == "Which date would you prefer for the Mustang test drive?"
    observation = sdk.requests[1][0][-1].parts[0].function_response.response
    assert observation["booking_outcome"]["status"] == "rejected"
    assert not observation.get("booking")


def test_repeated_rejected_proposal_finishes_with_read_only_composition(monkeypatch, state, caplog):
    args = {"car": {"listing_id": 20}, "day": "2030-11-21", "time": "10:00", "kind": "test_drive"}
    sdk = FakeSDK(monkeypatch, [*[response(tool("prepare_booking", args)) for _ in range(MAX_TOOL_ROUNDS)],
                                response(types.Part(text="Which car would you like to view?"))])
    executed = []

    def rejected(name, action):
        executed.append(name)
        state["booking_outcome"] = {"status": "rejected", "reply": "No appointment was created. Confirm which car you want."}
        return {"status": "needs_clarification", "booking_outcome": state["booking_outcome"]}

    reply = sdk.planner.run_turn("Make it next Saturday at10am.", state, rejected)
    assert reply == "Which car would you like to view?"
    assert executed == ["prepare_booking"] and len(sdk.requests) == MAX_TOOL_ROUNDS + 1
    assert sdk.modes[-1] == "TEXT"
    final = json.loads(sdk.requests[-1][0])
    assert final["context"]["booking_outcome"]["status"] == "rejected"
    assert all(item["result"]["status"] == "needs_clarification" for item in final["observations"])
    assert "next Saturday" not in caplog.text


def test_provider_failure_after_prepared_booking_uses_backend_outcome(monkeypatch, state, caplog):
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", {"car": {"listing_id": 20}, "time": "10:00", "kind": "test_drive"})),
                               ModelUnavailable("provider-private-payload-must-not-be-logged")])

    def prepare(name, action):
        state["booking_outcome"] = {"status": "awaiting_confirmation", "reply": "Please confirm this demo test drive proposed for 10am.",
                                    "booking_proposal": {"listing_id": 20, "proposal_id": "proposal-one"}}
        return {"status": "awaiting_confirmation", "booking_outcome": state["booking_outcome"]}

    assert sdk.planner.run_turn("sensitive-user-message", state, prepare) == state["booking_outcome"]["reply"]
    assert "generation_failed_after_booking_outcome" in caplog.text
    assert "ModelUnavailable" in caplog.text and "prepare_booking" in caplog.text
    assert "provider-private-payload" not in caplog.text and "sensitive-user-message" not in caplog.text


def test_initial_provider_failure_still_raises_without_pretending_an_action_ran(monkeypatch, state, caplog):
    sdk = FakeSDK(monkeypatch, [ModelUnavailable("provider-private-payload")])
    with pytest.raises(ModelUnavailable):
        sdk.planner.run_turn("private shopper message", state, lambda *args: pytest.fail("No tool was proposed"))
    assert "generation_failed" in caplog.text and "calls=0" in caplog.text
    assert "provider-private-payload" not in caplog.text and "private shopper message" not in caplog.text


def test_prepare_cancel_prepare_batch_returns_final_current_proposal_observation(monkeypatch, state):
    args = {"car": {"listing_id": 20}, "day": "2030-11-21", "time": "10:00", "kind": "test_drive"}
    sdk = FakeSDK(monkeypatch, [response(tool("prepare_booking", args), tool("cancel_booking"), tool("prepare_booking", args)),
                               response(types.Part(text="Please confirm the final proposed appointment."))])
    executed = []

    def execute(name, action):
        executed.append(name)
        status = "awaiting_confirmation" if name == "prepare_booking" else "cancelled"
        state["booking_outcome"] = {"status": status, "reply": "Final backend outcome: " + status}
        return {"status": status, "booking_outcome": state["booking_outcome"]}

    assert sdk.planner.run_turn("Prepare my final requested appointment.", state, execute) == "Please confirm the final proposed appointment."
    assert executed == ["prepare_booking", "cancel_booking", "prepare_booking"]
    assert state["booking_outcome"]["status"] == "awaiting_confirmation"
    outcomes = [part.function_response.response for part in sdk.requests[-1][0][-1].parts]
    assert [outcome["status"] for outcome in outcomes] == ["awaiting_confirmation", "cancelled", "awaiting_confirmation"]


def test_committed_booking_receipts_are_complete_context_separate_from_old_prose(monkeypatch, state):
    state["bookings"] = [{"booking_id": 17, "status": "confirmed", "listing_id": 20,
                          "starts_at": "2030-11-21T10:00:00+04:00", "kind": "test_drive"}]
    state["recent_turns"] = [{"message": "Book another car.", "reply": "An unsupported old confirmation."}]
    sdk = FakeSDK(monkeypatch, [response(types.Part(text="You have one saved appointment."))])
    sdk.planner.run_turn("What is booked?", state, lambda *args: {})
    context = json.loads(sdk.requests[0][0][-1].parts[0].text)["context"]
    assert context["bookings"] == state["bookings"] and context["bookings_complete"] is True
    assert context["booking_outcome"] is None


def test_final_text_only_provider_failure_falls_back_to_actual_observation(monkeypatch, state, caplog):
    sdk = FakeSDK(monkeypatch, [*[response(tool("search_inventory", {"filters": {"make": "Ford"}}))
                                 for _ in range(MAX_TOOL_ROUNDS)], ModelUnavailable("private transport details")])
    result = sdk.planner.run_turn("Find Ford cars.", state,
        lambda *args: {"status": "found", "reply": "I found five Ford listings."})
    assert result == "I found five Ford listings."
    assert sdk.requests[-1][1].tools is None and sdk.requests[-1][1].tool_config is None
    assert "final_composition_failed" in caplog.text and "private transport details" not in caplog.text
