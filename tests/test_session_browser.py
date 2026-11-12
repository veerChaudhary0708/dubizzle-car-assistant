"""Public endpoints used by the saved-conversation sidebar."""

from fastapi.testclient import TestClient

from main import create_app


def test_session_browser_lists_only_nonempty_chats_for_requested_shopper(tmp_path):
    # No language model is involved in listing or reading previously saved turns.
    app = create_app(data_dir=tmp_path, planner=object())
    with TestClient(app) as client:
        alice = client.post("/sessions", json={"user_id": "alice"}).json()["session_id"]
        empty = client.post("/sessions", json={"user_id": "alice"}).json()["session_id"]
        bob = client.post("/sessions", json={"user_id": "bob"}).json()["session_id"]
        app.state.store.commit_turn("alice", alice, "first", 0, "Find me a Mustang.", {"reply": "One match."})
        app.state.store.commit_turn("bob", bob, "first", 0, "Find me an SUV.", {"reply": "Two matches."})

        response = client.get("/sessions", params={"user_id": "alice"})
        assert response.status_code == 200
        assert len(response.json()) == 1
        entry = response.json()[0]
        assert entry["session_id"] == alice and entry["user_id"] == "alice"
        assert entry["title"] == "Find me a Mustang."
        assert entry["created_at"] and entry["updated_at"]
        assert empty not in {row["session_id"] for row in response.json()}
        assert client.get(f"/sessions/{entry['session_id']}", params={"user_id": "alice"}).json()["turns"][0]["reply"] == "One match."
        assert client.get(f"/sessions/{alice}", params={"user_id": "bob"}).status_code == 404
        assert client.get("/sessions", params={"user_id": "new-shopper"}).json() == []
        assert client.get("/sessions").status_code == 422
