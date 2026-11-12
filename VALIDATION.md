# Final submission validation

Verified on 12 November 2026 against the source hashes in [the offline record](evidence/submission-offline.json) and [the live record](evidence/submission-demo.json).

## Fresh installation and offline tests

`uv sync --locked` completed successfully in a new virtual environment using Python 3.11.9 on Windows. `uv run --locked pytest -q` then passed **329 tests and 404 inventory subtests** in 48.30 seconds. The only warning was Starlette's deprecation notice for its current httpx TestClient integration.

Coverage includes all 100 inventory rows, missing-data grounding, price boundaries, ordered result references, independent shoppers, fresh sessions, preference evidence and corrections, lead CSV capture/synchronization/recovery, native tool protocol, rollback, opening hours, booking proposal/confirmation/retry, and Streamlit client behavior. Controlled planners isolate backend contracts; these tests do not measure universal language understanding.

## Real Gemini demonstrations

The [terminal-style transcript](evidence/submission-demo.md) and machine-readable record contain **11 real chat turns, 22 provider calls, and 43 passing checks** with `gemini-3.5-flash`. They exercise:

1. Mustang search followed by mileage, warranty, V6 explanation, and a saved like.
2. A completely new session with zero previous turns that recalls the same shopper's budget and car.
3. A separate shopper's browsing, price-range answer and short requirements answer, followed by inspection of the actual exported CSV, without booking.
4. A complete test-drive proposal, an intervening mileage question, explicit confirmation, idempotent retry, a saved receipt, and isolation from the other shopper. Confirmation was tested with model generation disabled.

Manual review confirmed the warranty answer reports missing information, V6 explanation separates general knowledge from seller claims, memory refers to the correct shopper, and lead questions are relevant. Minor wording limitations include repeated booking suggestions and an extra fuel-economy clarification in an odometer answer. These do not affect the verified stored values or transaction boundaries.

Run `uv run python scripts/demo.py` with your own configured Gemini key to reproduce. This consumes API quota and uses temporary synthetic data, not the running app's saved chats. Natural-language outputs can vary; the transcript is observed evidence, not an expected-response template.

## Scope and assumptions

The current submission includes the final lead-collection follow-up behavior and the regression fix for a personal budget and stricter search at the same amount. Different monetary limits still require clarification. All cars support simulated bookings because managed-car status is absent. Slots last 30 minutes, Monday–Saturday, 08:00–20:00 in Dubai time. Shopper IDs scope local records but are not authentication. No real dealer, email, payment or calendar integration is included.

Only synthetic demo evidence is published. API keys, the local runtime database, actual shopper chats/leads, caches and historical development reports are excluded.
