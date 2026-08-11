from fastapi.testclient import TestClient

from threadstack.api import create_app
from threadstack.models import AgentCreate
from threadstack.store import ExchangeStore

MCP_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-03-26",
}


def mcp_call(
    client: TestClient,
    request_id: int,
    name: str,
    arguments: dict,
    token: str | None = None,
) -> dict:
    headers = {**MCP_HEADERS}
    if token:
        headers["authorization"] = f"Bearer {token}"
    response = client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_owner_created_agent_uses_mcp_to_create_nested_threads() -> None:
    store = ExchangeStore()
    owner, _ = store.login_or_create_user(
        "mcp-owner@example.com",
        "correct-horse-battery-staple",
    )
    agent, token = store.register_agent(
        owner.id,
        AgentCreate(name="mcp-test-agent", description="MCP test agent"),
    )

    with TestClient(create_app(store=store)) as client:
        response = client.post(
            "/mcp/",
            headers={"accept": "application/json", "content-type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert response.status_code == 200

        unauthenticated = mcp_call(
            client,
            2,
            "create_thread",
            {"title": "Should not post", "body": "A token is required."},
        )
        assert unauthenticated["isError"] is True

        thread_result = mcp_call(
            client,
            3,
            "create_thread",
            {"title": "MCP nested thread", "body": "Root post."},
            token=token,
        )
        thread = thread_result["structuredContent"]
        root_post_id = thread["posts"][0]["id"]

        reply_result = mcp_call(
            client,
            4,
            "post_reply",
            {
                "thread_id": thread["id"],
                "body": "Nested MCP reply.",
                "parent_id": root_post_id,
            },
            token=token,
        )
        assert reply_result["structuredContent"]["parent_id"] == root_post_id

        thread_vote = mcp_call(
            client,
            5,
            "upvote_thread",
            {"thread_id": thread["id"]},
            token=token,
        )
        assert thread_vote["structuredContent"]["score"] == 1
        assert thread_vote["structuredContent"]["upvoted"] is True

        post_vote = mcp_call(
            client,
            6,
            "upvote_post",
            {"post_id": root_post_id},
            token=token,
        )
        assert post_vote["structuredContent"]["score"] == 1

        fetched = mcp_call(
            client,
            7,
            "get_thread",
            {"thread_id": thread["id"]},
            token=token,
        )["structuredContent"]
        assert fetched["score"] == 1
        root_post = next(post for post in fetched["posts"] if post["id"] == root_post_id)
        assert root_post["score"] == 1
        assert root_post["upvoted"] is True

        removed = mcp_call(
            client,
            8,
            "remove_post_upvote",
            {"post_id": root_post_id},
            token=token,
        )
        assert removed["structuredContent"]["score"] == 0

        profile = mcp_call(
            client,
            9,
            "get_agent_profile",
            {"agent_id": str(agent.id)},
            token=token,
        )["structuredContent"]
        assert profile["name"] == agent.name
        assert "email" not in profile

        directory = mcp_call(client, 11, "list_agents", {}, token=token)["structuredContent"]
        assert all("email" not in item for item in directory)

        updated_profile = mcp_call(
            client,
            12,
            "update_agent_profile",
            {
                "agent_id": str(agent.id),
                "bio": "MCP profile",
                "specialties": ["IAM"],
            },
            token=token,
        )["structuredContent"]
        assert updated_profile["bio"] == "MCP profile"
        assert updated_profile["specialties"] == ["iam"]

        semantic_results = mcp_call(
            client,
            13,
            "search_threads",
            {"query": "MCP nested", "semantic": True},
            token=token,
        )["structuredContent"]["result"]
        assert [item["id"] for item in semantic_results] == [thread["id"]]
