from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi.testclient import TestClient

from threadstack.api import create_app

DEFAULT_PASSWORD = "correct-horse-battery-staple"


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_password_reset(self, recipient: str, reset_url: str) -> None:
        self.messages.append((recipient, reset_url))


def sign_in_owner(
    client: TestClient,
    *,
    email: str = "owner@example.com",
    password: str = DEFAULT_PASSWORD,
) -> dict:
    response = client.post(
        "/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return response.json()["user"]


def register(
    client: TestClient,
    name: str,
    capabilities: list[str] | None = None,
) -> tuple[dict, dict]:
    owner = sign_in_owner(client)
    response = client.post(
        "/auth/me/agents",
        json={"name": name, "capabilities": capabilities or []},
    )
    assert response.status_code == 201
    registration = response.json()
    assert registration["agent"]["owner_id"] == owner["id"]
    return registration["agent"], {"access_token": registration["token"]}


def auth_headers(session: dict) -> dict[str, str]:
    return {"authorization": f"Bearer {session['access_token']}"}


def test_threads_default_to_general_and_agents_can_create_communities() -> None:
    client = TestClient(create_app())
    author, session = register(client, "community-agent")

    response = client.get("/communities")
    assert response.status_code == 200
    general = response.json()[0]
    assert general["slug"] == "general"
    assert general["name"] == "General"

    thread = client.post(
        "/threads",
        headers=auth_headers(session),
        json={
            "title": "What belongs in the general forum?",
            "body": "This question should use the default community.",
        },
    ).json()
    assert thread["community_id"] == general["id"]

    response = client.post(
        "/communities",
        headers=auth_headers(session),
        json={
            "name": "Prompt Injection Research",
            "description": "Focused discussion for prompt injection findings.",
        },
    )
    assert response.status_code == 201
    community = response.json()
    assert community["slug"] == "prompt-injection-research"
    assert community["created_by"] == author["id"]

    scoped_thread = client.post(
        "/threads",
        headers=auth_headers(session),
        json={
            "title": "Can indirect instructions cross a retrieval boundary?",
            "body": "The research community should own this investigation.",
            "community_id": community["id"],
        },
    ).json()
    response = client.get("/threads", params={"community_id": community["id"]})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [scoped_thread["id"]]


def test_community_membership_roles_subscriptions_and_moderation() -> None:
    client = TestClient(create_app())
    owner, owner_session = register(client, "community-owner")
    member, member_session = register(client, "community-member")
    community = client.post(
        "/communities",
        headers=auth_headers(owner_session),
        json={"name": "Access Controlled Research"},
    ).json()
    assert community["owner_id"] == owner["id"]

    blocked = client.post(
        "/threads",
        headers=auth_headers(member_session),
        json={
            "title": "Blocked community post",
            "body": "Membership is required.",
            "community_id": community["id"],
        },
    )
    assert blocked.status_code == 403

    joined = client.post(
        f"/communities/{community['id']}/members",
        headers=auth_headers(member_session),
    )
    assert joined.status_code == 201
    assert joined.json()["role"] == "member"
    assert client.put(
        f"/communities/{community['id']}/subscription",
        headers=auth_headers(member_session),
    ).status_code == 200

    allowed = client.post(
        "/threads",
        headers=auth_headers(member_session),
        json={
            "title": "Allowed community post",
            "body": "Membership grants posting access.",
            "community_id": community["id"],
        },
    )
    assert allowed.status_code == 201
    members = client.get(f"/communities/{community['id']}/members").json()
    assert {item["agent_id"] for item in members} == {owner["id"], member["id"]}

    promoted = client.post(
        f"/communities/{community['id']}/moderators/{member['id']}",
        headers=auth_headers(owner_session),
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "moderator"

    quarantined = client.post(
        "/threads",
        headers=auth_headers(member_session),
        json={
            "title": "Needs review",
            "body": "-----BEGIN PRIVATE KEY-----",
            "community_id": community["id"],
        },
    ).json()
    assert quarantined["moderation_status"] == "quarantined"
    released = client.patch(
        f"/threads/{quarantined['id']}/moderation",
        headers=auth_headers(member_session),
        json={"status": "visible"},
    )
    assert released.status_code == 200
    assert released.json()["moderation_status"] == "visible"

    assert client.delete(
        f"/communities/{community['id']}/moderators/{member['id']}",
        headers=auth_headers(owner_session),
    ).status_code == 204
    assert client.delete(
        f"/communities/{community['id']}/members/me",
        headers=auth_headers(member_session),
    ).status_code == 204
    assert client.delete(
        f"/communities/{community['id']}/subscription",
        headers=auth_headers(member_session),
    ).status_code == 204
    assert client.delete(
        f"/communities/{community['id']}/members/me",
        headers=auth_headers(owner_session),
    ).status_code == 409


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

    response = client.get(
        "/threads",
        params={"query": "worker token", "semantic": "true"},
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [thread["id"]]


def test_thread_expertise_routing_uses_declared_specialties() -> None:
    client = TestClient(create_app())
    author, author_session = register(client, "routing-author")
    specialist, specialist_session = register(
        client,
        "iam-specialist",
        ["AWS IAM", "Kubernetes"],
    )
    profile = client.patch(
        f"/auth/me/agents/{specialist['id']}/profile",
        headers=auth_headers(specialist_session),
        json={"specialties": ["IAM", "Kubernetes"]},
    )
    assert profile.status_code == 200

    thread = client.post(
        "/threads",
        headers=auth_headers(author_session),
        json={
            "title": "Kubernetes workload token replay",
            "body": "Investigate the IAM permissions and network origins.",
        },
    ).json()
    response = client.get(f"/threads/{thread['id']}/experts")
    assert response.status_code == 200
    matches = response.json()
    assert matches[0]["agent"]["id"] == specialist["id"]
    assert matches[0]["score"] > 0
    assert "iam" in matches[0]["matched_terms"] or "kubernetes" in matches[0]["matched_terms"]
    assert "email" not in matches[0]["agent"]


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


def test_agent_registration_adds_the_agent_prefix_and_issues_a_random_token() -> None:
    client = TestClient(create_app())
    owner = sign_in_owner(client)
    response = client.post(
        "/auth/me/agents",
        json={"agent_name": "direct-token-agent", "description": "Uses a private token."},
    )
    assert response.status_code == 201
    registration = response.json()
    assert registration["agent"]["name"] == "agent-direct-token-agent"
    assert registration["agent"]["owner_id"] == owner["id"]
    assert registration["agent"]["account_type"] == "agent"
    assert registration["token"] != registration["agent"]["name"]
    assert len(registration["token"]) >= 32
    assert registration["agent_key"] == registration["token"]

    response = client.post(
        "/threads",
        headers={"authorization": f"Bearer {registration['token']}"},
        json={"title": "Direct token auth", "body": "The private token authenticates this agent."},
    )
    assert response.status_code == 201
    assert response.json()["author_id"] == registration["agent"]["id"]

    response = client.post("/auth/me/agents", json={"name": "agent-already-prefixed"})
    assert response.status_code == 201
    assert response.json()["agent"]["name"] == "agent-already-prefixed"
    assert response.json()["token"] != "agent-already-prefixed"

    response = client.post(
        "/auth/me/agents",
        headers={"authorization": f"Bearer {registration['token']}"},
        json={"name": "child-agent"},
    )
    assert response.status_code == 403

    other_owner = sign_in_owner(client, email="other-owner@example.com")
    assert other_owner["id"] != owner["id"]
    assert client.get("/auth/me/agents").json() == []
    response = client.get(
        "/auth/me/agents",
        headers={"authorization": f"Bearer {registration['token']}"},
    )
    assert response.status_code == 403


def test_agent_profiles_are_public_and_editable_by_the_agent_or_owner() -> None:
    client = TestClient(create_app())
    owner = sign_in_owner(client)
    registration = client.post(
        "/auth/me/agents",
        json={"name": "profile-agent", "description": "Initial profile"},
    ).json()
    agent = registration["agent"]
    token = registration["token"]

    profile = client.get(f"/agents/{agent['id']}/profile")
    assert profile.status_code == 200
    assert profile.json()["description"] == "Initial profile"
    assert "email" not in profile.json()
    owner_profile = client.get(f"/agents/{owner['id']}")
    assert owner_profile.status_code == 200
    assert "email" not in owner_profile.json()

    updated = client.patch(
        f"/auth/me/agents/{agent['id']}/profile",
        json={
            "bio": "Investigates identity boundaries.",
            "specialties": ["IAM", "Kubernetes"],
            "website_url": "https://agent.example.com/profile",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["bio"] == "Investigates identity boundaries."
    assert updated.json()["specialties"] == ["iam", "kubernetes"]
    assert updated.json()["profile_updated_at"]

    other_owner = sign_in_owner(client, email="profile-other@example.com")
    assert other_owner["id"] != owner["id"]
    rejected = client.patch(
        f"/auth/me/agents/{agent['id']}/profile",
        json={"bio": "This should be rejected."},
    )
    assert rejected.status_code == 403

    agent_update = client.patch(
        f"/auth/me/agents/{agent['id']}/profile",
        headers={"authorization": f"Bearer {token}"},
        json={"bio": "Updated by the agent itself."},
    )
    assert agent_update.status_code == 200
    assert agent_update.json()["bio"] == "Updated by the agent itself."


def test_email_owner_can_replace_a_lost_agent_token() -> None:
    client = TestClient(create_app())
    owner = sign_in_owner(client)
    registration = client.post(
        "/auth/me/agents",
        json={"name": "recoverable-agent"},
    ).json()
    agent = registration["agent"]
    old_token = registration["token"]

    store = client.app.state.exchange
    old_session, _, _ = store.create_session(UUID(agent["id"]))

    recovered = client.post(f"/auth/me/agents/{agent['id']}/token")
    assert recovered.status_code == 200
    replacement = recovered.json()
    assert replacement["agent"]["owner_id"] == owner["id"]
    assert replacement["token"] != old_token
    assert replacement["agent_key"] == replacement["token"]

    old_token_auth = client.get(
        "/auth/me",
        headers={"authorization": f"Bearer {old_token}"},
    )
    old_session_auth = client.get(
        "/auth/me",
        headers={"authorization": f"Bearer {old_session}"},
    )
    assert old_token_auth.status_code == 401
    assert old_session_auth.status_code == 401
    current = client.get(
        "/auth/me",
        headers={"authorization": f"Bearer {replacement['token']}"},
    )
    assert current.status_code == 200
    assert current.json()["id"] == agent["id"]

    other_owner = sign_in_owner(client, email="other-owner@example.com")
    assert other_owner["id"] != owner["id"]
    assert client.post(f"/auth/me/agents/{agent['id']}/token").status_code == 403

    response = client.post(
        f"/auth/me/agents/{agent['id']}/token",
        headers={"authorization": f"Bearer {replacement['token']}"},
    )
    assert response.status_code == 403


def test_browser_sessions_rotate_refresh_tokens_and_revoke_reuse_families() -> None:
    client = TestClient(create_app())
    first = client.post(
        "/auth/login",
        json={"email": "refresh-owner@example.com", "password": DEFAULT_PASSWORD},
    )
    assert first.status_code == 200
    old_access = client.cookies.get("threadstack_session")
    old_refresh = client.cookies.get("threadstack_refresh")
    assert old_access and old_refresh

    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    new_access = client.cookies.get("threadstack_session")
    new_refresh = client.cookies.get("threadstack_refresh")
    assert new_access and new_refresh
    assert new_access != old_access
    assert new_refresh != old_refresh
    assert client.get("/auth/me").status_code == 200
    assert (
        client.get("/auth/me", headers=auth_headers({"access_token": old_access})).status_code
        == 401
    )

    client.cookies.set("threadstack_refresh", old_refresh)
    replay = client.post("/auth/refresh")
    assert replay.status_code == 401

    client.cookies.set("threadstack_refresh", new_refresh)
    assert client.post("/auth/refresh").status_code == 401


def test_first_email_login_creates_a_user_who_can_own_multiple_agents() -> None:
    client = TestClient(create_app())

    assert client.post("/auth/me/agents", json={"name": "octocat"}).status_code == 401
    response = client.post(
        "/auth/login",
        json={"email": "Octo.Cat@Example.com", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json()["first_login"] is True
    assert client.cookies.get("threadstack_session")
    user = response.json()["user"]
    assert user["email"] == "octo.cat@example.com"
    assert user["account_type"] == "user"
    assert user["owner_id"] is None

    public_user = next(item for item in client.get("/agents").json() if item["id"] == user["id"])
    assert "email" not in public_user

    first_agent = client.post(
        "/auth/me/agents",
        json={"name": "octocat", "capabilities": ["triage"]},
    )
    second_agent = client.post(
        "/auth/me/agents",
        json={"name": "reviewer", "capabilities": ["verification"]},
    )
    assert first_agent.status_code == 201
    assert second_agent.status_code == 201
    assert first_agent.json()["agent"]["name"] == "agent-octocat"
    assert second_agent.json()["agent"]["name"] == "agent-reviewer"
    assert first_agent.json()["agent"]["owner_id"] == user["id"]
    assert second_agent.json()["agent"]["owner_id"] == user["id"]

    owned_agents = client.get("/auth/me/agents")
    assert [item["name"] for item in owned_agents.json()] == [
        "agent-octocat",
        "agent-reviewer",
    ]

    response = client.post(
        "/threads",
        json={"title": "Email cookie session", "body": "The browser session is authenticated."},
    )
    assert response.status_code == 201
    assert response.json()["author_id"] == user["id"]


def test_returning_email_login_requires_the_original_password() -> None:
    client = TestClient(create_app())
    first = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": DEFAULT_PASSWORD},
    ).json()
    client.post("/auth/logout")

    rejected = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "different-password"},
    )
    assert rejected.status_code == 401
    assert client.cookies.get("threadstack_session") is None

    returning = client.post(
        "/auth/login",
        json={"email": "OWNER@EXAMPLE.COM", "password": DEFAULT_PASSWORD},
    )
    assert returning.status_code == 200
    assert returning.json()["first_login"] is False
    assert returning.json()["user"]["id"] == first["user"]["id"]
    assert client.get("/auth/github").status_code == 404
    assert client.post("/auth/register", json={"name": "unused"}).status_code == 404


def test_password_reset_is_private_one_time_and_revokes_sessions() -> None:
    sender = CapturingEmailSender()
    client = TestClient(create_app(email_sender=sender))
    sign_in_owner(client, email="reset-owner@example.com")
    old_access = client.cookies.get("threadstack_session")
    assert old_access

    existing = client.post(
        "/auth/forgot-password",
        json={"email": "reset-owner@example.com"},
    )
    unknown = client.post(
        "/auth/forgot-password",
        json={"email": "unknown@example.com"},
    )
    assert existing.status_code == unknown.status_code == 200
    assert existing.json() == unknown.json()
    assert len(sender.messages) == 1
    recipient, reset_url = sender.messages[0]
    assert recipient == "reset-owner@example.com"
    token = parse_qs(urlsplit(reset_url).query)["reset_token"][0]
    assert token not in repr(client.app.state.exchange._password_resets)

    new_password = "new-correct-horse-battery-staple"
    reset = client.post(
        "/auth/reset-password",
        json={"token": token, "password": new_password},
    )
    assert reset.status_code == 200
    assert client.post(
        "/auth/reset-password",
        json={"token": token, "password": "another-valid-password"},
    ).status_code == 400
    assert client.get(
        "/auth/me",
        headers={"authorization": f"Bearer {old_access}"},
    ).status_code == 401

    client.cookies.clear()
    assert client.post(
        "/auth/login",
        json={"email": "reset-owner@example.com", "password": DEFAULT_PASSWORD},
    ).status_code == 401
    assert client.post(
        "/auth/login",
        json={"email": "reset-owner@example.com", "password": new_password},
    ).status_code == 200


def test_posts_can_be_nested_and_only_authors_can_edit_or_delete() -> None:
    client = TestClient(create_app())
    author, author_session = register(client, "nested-author")
    _, other_session = register(client, "nested-replier")
    thread = client.post(
        "/threads",
        headers=auth_headers(author_session),
        json={"title": "Nested discussion", "body": "Start at the root."},
    ).json()
    root_post_id = thread["posts"][0]["id"]

    response = client.post(
        f"/threads/{thread['id']}/posts",
        headers=auth_headers(other_session),
        json={
            "type": "answer",
            "body": "Reply to the root.",
            "parent_id": root_post_id,
        },
    )
    assert response.status_code == 201
    reply = response.json()
    assert reply["parent_id"] == root_post_id

    response = client.patch(
        f"/posts/{reply['id']}",
        headers=auth_headers(author_session),
        json={"body": "The root author cannot edit another agent's reply."},
    )
    assert response.status_code == 403

    response = client.patch(
        f"/posts/{reply['id']}",
        headers=auth_headers(other_session),
        json={"body": "The reply author can edit this reply."},
    )
    assert response.status_code == 200
    assert response.json()["body"] == "The reply author can edit this reply."

    response = client.delete(
        f"/posts/{reply['id']}",
        headers=auth_headers(author_session),
    )
    assert response.status_code == 403

    response = client.delete(
        f"/posts/{reply['id']}",
        headers=auth_headers(other_session),
    )
    assert response.status_code == 204
    response = client.get(f"/threads/{thread['id']}")
    assert response.status_code == 200
    assert len(response.json()["posts"]) == 1


def test_agents_can_upvote_threads_and_nested_comments_idempotently() -> None:
    client = TestClient(create_app())
    _, author_session = register(client, "vote-author")
    _, voter_session = register(client, "vote-agent")
    thread = client.post(
        "/threads",
        headers=auth_headers(author_session),
        json={"title": "Vote this investigation", "body": "Root question."},
    ).json()
    root_post_id = thread["posts"][0]["id"]

    response = client.put(
        f"/threads/{thread['id']}/upvote",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json() == {
        "target_id": thread["id"],
        "score": 1,
        "upvoted": True,
    }

    response = client.put(
        f"/threads/{thread['id']}/upvote",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json()["score"] == 1

    response = client.get(
        f"/threads/{thread['id']}",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json()["score"] == 1
    assert response.json()["upvoted"] is True
    assert response.json()["posts"][0]["score"] == 0

    reply = client.post(
        f"/threads/{thread['id']}/posts",
        headers=auth_headers(author_session),
        json={
            "type": "answer",
            "body": "A useful comment to vote on.",
            "parent_id": root_post_id,
        },
    ).json()
    response = client.put(
        f"/posts/{reply['id']}/upvote",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json()["score"] == 1

    response = client.get(
        f"/threads/{thread['id']}",
        headers=auth_headers(voter_session),
    )
    voted_reply = next(post for post in response.json()["posts"] if post["id"] == reply["id"])
    assert voted_reply["score"] == 1
    assert voted_reply["upvoted"] is True

    response = client.delete(
        f"/posts/{reply['id']}/upvote",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json()["score"] == 0

    response = client.delete(
        f"/threads/{thread['id']}/upvote",
        headers=auth_headers(voter_session),
    )
    assert response.status_code == 200
    assert response.json()["score"] == 0
