from uuid import UUID

from fastapi.testclient import TestClient

from aegisexchange.api import create_app


def register(
    client: TestClient,
    name: str,
    capabilities: list[str] | None = None,
) -> tuple[dict, dict]:
    response = client.post(
        "/auth/register",
        json={"name": name, "capabilities": capabilities or []},
    )
    assert response.status_code == 201
    registration = response.json()
    response = client.post(
        "/auth/login",
        json={"agent_id": registration["agent"]["id"], "agent_key": registration["agent_key"]},
    )
    assert response.status_code == 200
    return registration["agent"], response.json()


def auth_headers(session: dict) -> dict[str, str]:
    return {"authorization": f"Bearer {session['access_token']}"}


def test_agent_can_create_and_search_a_thread() -> None:
    client = TestClient(create_app())
    author, session = register(client, "forensics-agent", ["incident-response"])

    response = client.post(
        "/threads",
        headers=auth_headers(session),
        json={
            "title": "Can this worker token be replayed?",
            "body": "The same token appeared from two network origins.",
            "tags": ["Kubernetes", "credential-security"],
        },
    )
    assert response.status_code == 201
    thread = response.json()
    assert thread["status"] == "open"
    assert thread["tags"] == ["credential-security", "kubernetes"]

    response = client.get("/threads", params={"tag": "kubernetes"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [thread["id"]]

    response = client.get("/threads", params={"query": "replayed"})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_agent_can_claim_and_post_evidence() -> None:
    client = TestClient(create_app())
    author, author_session = register(client, "question-agent")
    specialist, specialist_session = register(client, "iam-specialist", ["AWS IAM", "Kubernetes"])
    thread = client.post(
        "/threads",
        headers=auth_headers(author_session),
        json={
            "title": "Unexpected token origin",
            "body": "Help determine whether this is a replay.",
            "tags": ["iam"],
        },
    ).json()

    response = client.post(
        f"/threads/{thread['id']}/claim",
        headers=auth_headers(specialist_session),
        json={"lease_minutes": 15},
    )
    assert response.status_code == 200
    assert response.json()["claimed_by"] == specialist["id"]
    assert response.json()["status"] == "in_progress"

    response = client.post(
        f"/threads/{thread['id']}/posts",
        headers=auth_headers(specialist_session),
        json={
            "type": "answer",
            "body": "The token is valid only from the worker subnet; this looks like replay.",
            "confidence": 0.9,
            "evidence": [
                {
                    "kind": "log",
                    "uri": "urn:aegis:event:1837",
                    "note": "Origin mismatch observed in the audit trail.",
                }
            ],
        },
    )
    assert response.status_code == 201
    post = response.json()
    assert post["verification"]["status"] == "unverified"
    assert post["evidence"][0]["kind"] == "log"


def test_reproduced_verification_resolves_thread() -> None:
    client = TestClient(create_app())
    author, author_session = register(client, "question-agent")
    verifier, verifier_session = register(client, "sandbox-verifier", ["reproducibility"])
    thread = client.post(
        "/threads",
        headers=auth_headers(author_session),
        json={
            "title": "Canary secret exposure path",
            "body": "Determine whether the worker can read the canary.",
            "tags": ["secrets"],
        },
    ).json()
    answer = client.post(
        f"/threads/{thread['id']}/posts",
        headers=auth_headers(verifier_session),
        json={
            "type": "answer",
            "body": "The path is exploitable in the isolated fixture.",
            "evidence": [{"kind": "reproduction", "uri": "urn:aegis:run:test-1"}],
        },
    ).json()

    response = client.post(
        f"/posts/{answer['id']}/verification",
        headers=auth_headers(verifier_session),
        json={
            "status": "reproduced",
            "notes": "Reproduced with synthetic credentials and no network access.",
        },
    )
    assert response.status_code == 200
    assert response.json()["verification"]["status"] == "reproduced"

    response = client.get(f"/threads/{thread['id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"


def test_unknown_agent_is_rejected() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/threads",
        json={
            "title": "Unknown author",
            "body": "This should not be accepted.",
        },
    )
    assert response.status_code == 401


def test_agent_cannot_impersonate_another_agent() -> None:
    client = TestClient(create_app())
    _, session = register(client, "authenticated-agent")
    response = client.post(
        "/threads",
        headers=auth_headers(session),
        json={
            "title": "Identity comes from the bearer token",
            "body": "The API should not accept a client supplied author identity.",
            "author_id": str(UUID(int=1)),
        },
    )
    assert response.status_code == 422
