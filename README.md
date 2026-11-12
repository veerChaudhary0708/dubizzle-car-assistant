# Used car finder

A FastAPI + Streamlit car-shopping assistant using Gemini and the supplied 100-listing inventory. It searches and compares cars, answers contextual follow-ups, remembers returning shoppers, records leads, and simulates viewing/test-drive bookings.

## Run locally

Requirements: Python **3.11–3.14**, [uv](https://docs.astral.sh/uv/getting-started/installation/), and a [Google AI Studio API key](https://aistudio.google.com/apikey).

From this repository's root:

```shell
uv sync --locked
```

Copy `.env.example` to `.env` and set `GEMINI_API_KEY`. Keep the key private; `.env` and runtime data are excluded from Git. The default model is `gemini-3.5-flash`; optionally set `GEMINI_MODEL` to another function-calling Gemini model available to your project. API availability and quota depend on that project.

Start the backend:

```shell
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

In a second terminal, from the same directory, start the client:

```shell
uv run streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open [the chat](http://127.0.0.1:8501) or [the API docs](http://127.0.0.1:8000/docs). Reuse the same **Shopper ID** to recall preferences. **New chat** clears conversational focus while keeping personal memory; **Saved chats** reopens an earlier conversation.

## Design decisions

Streamlit keeps the client small and separate from FastAPI. Gemini uses native function calling through the Google Gen AI SDK; validated Python tools perform retrieval and persistence before the model explains their results. With 100 listings, SQLite filtering and title/description retrieval provide inspectable grounding without a vector database or orchestration framework. Original seller descriptions and supporting excerpts are retained; missing prices, mileage, and warranty information remain unknown. See the [architecture diagram](docs/architecture.png) and [implementation details](docs/IMPLEMENTATION.md).

SQLite stores shopper preferences, inquiry history, ordered result references, and recent conversation context separately. Changes commit with the conversation turn; the local leads CSV is exported from committed records. A booking draft becomes a reviewed proposal, and only explicit confirmation can create a validated appointment and receipt. This is a local prototype: shopper IDs are not authentication, and no dealer, calendar, or email service is contacted. Authenticated access, real inventory/booking feeds, retention controls, and broader model evaluations are future work. Natural-language interpretation remains model-dependent.

## Bookings, leads, and assumptions

- Ask for a viewing or test drive in chat. Review the exact car, type, and Dubai date/time, then click **Confirm booking** or give a standalone confirmation immediately after the proposal. **Your bookings** shows saved appointments across chats, with a booking reference. Availability checks and proposals do not reserve anything.
- Hours are **Monday–Saturday, 08:00–20:00 Dubai time**. **30-minute slots**, with the last start at **19:30**, are a documented implementation assumption. Past dates, Sundays, invalid slots, and duplicate reservations are rejected.
- **All 100 cars allow demo bookings** because the dataset does not identify managed cars. This does not establish actual dealership eligibility. Cancellation currently clears unfinished drafts only.
- Startup imports the workbook's **cleaned dataset** sheet. The sheets are not combined. Conservative extraction identifies 13 asking prices and 38 odometers; unknown values are not treated as zero or guaranteed budget matches.
- The first run creates `runtime/assistant.sqlite3` and **`runtime/leads.csv`**. Leads record shopper/session IDs, price bounds, needs, and update time; a booking is not required. Use `CAR_ASSISTANT_DATA_DIR` to choose a different runtime directory. Use synthetic shopper information: relevant messages and listing context are sent to Gemini.

## Requirements and evidence

The [recorded submission demo](evidence/submission-demo.md) shows multi-turn inventory exploration and recall in a **completely new session** for the same shopper, followed by lead and booking checks. The [machine-readable record](evidence/submission-demo.json) preserves responses and verification results. [VALIDATION.md](VALIDATION.md) identifies the verified source snapshot, checks, and limitations.

| Requirement | Implementation / evidence |
|---|---|
| FastAPI backend and Streamlit client | [main.py](main.py), [app.py](app.py) |
| Inventory retrieval and description grounding | [inventory.py](inventory.py), [planner.py](planner.py), submission demo |
| Short-term context and returning-user memory | [conversation_context.py](conversation_context.py), [storage.py](storage.py), two-session demo |
| Simulated bookings and opening hours | [booking.py](booking.py), booking demo and [tests](tests/) |
| Price range / needs and local CSV leads | [assistant_service.py](assistant_service.py), [storage.py](storage.py), lead demo |
| Chit-chat, intent recognition, and guardrails | [planner.py](planner.py), [validation evidence](VALIDATION.md) |

Run the offline tests without an API key:

```shell
uv run pytest -q
```

These tests cover backend contracts, memory isolation, inventory grounding, lead export, booking validation, and client behavior. Live-model evidence is recorded separately; passing scripted tests is not a guarantee for every possible conversation.

To reproduce the live demo with your configured Gemini API key:

```shell
uv run python scripts/demo.py
```

The script uses temporary synthetic shopper data and does not require a running server. It makes real Gemini requests and uses your project's API quota.
