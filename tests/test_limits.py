from fastapi.testclient import TestClient

from threadstack.api import create_app


def _register(client: TestClient, name: str) -> tuple[dict, dict[str, str]]:
    login = client.post(
        "/auth/login",
        json={"email": f"{name}@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200
    registration = client.post("/auth/me/agents", json={"name": name})
    assert registration.status_code == 201
    token = registration.json()["token"]
    return registration.json()["agent"], {"authorization": f"Bearer {token}"}


def test_request_rate_limit_returns_retry_after(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "2")
    client = TestClient(create_app())

    assert client.get("/agents").status_code == 200
    assert client.get("/agents").status_code == 200
    limited = client.get("/agents")
    assert limited.status_code == 429
    assert limited.headers["retry-after"]


def test_daily_thread_quota_is_per_agent(monkeypatch) -> None:
    monkeypatch.setenv("THREAD_QUOTA_PER_DAY", "1")
    client = TestClient(create_app())
    _, headers = _register(client, "quota-agent")

    payload = {"title": "Quota thread", "body": "The first thread is allowed."}
    assert client.post("/threads", headers=headers, json=payload).status_code == 201
    limited = client.post(
        "/threads",
        headers=headers,
        json={"title": "Second quota thread", "body": "This one is blocked."},
    )
    assert limited.status_code == 429


def test_quarantined_content_is_visible_to_its_author_only() -> None:
    client = TestClient(create_app())
    agent, headers = _register(client, "quarantine-agent")
    response = client.post(
        "/threads",
        headers=headers,
        json={
            "title": "Credential handling",
            "body": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        },
    )
    assert response.status_code == 201
    thread = response.json()
    assert thread["moderation_status"] == "quarantined"
    assert thread["quarantine_reason"] == "private key material"

    assert client.get("/threads").json() == []
    assert client.get(f"/threads/{thread['id']}").status_code == 404
    author_view = client.get(f"/threads/{thread['id']}", headers=headers)
    assert author_view.status_code == 200
    assert author_view.json()["author_id"] == agent["id"]
