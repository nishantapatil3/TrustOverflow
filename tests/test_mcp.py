from fastapi.testclient import TestClient

from threadstack.api import create_app

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


def test_mcp_registers_agents_and_creates_nested_threads() -> None:
    with TestClient(create_app()) as client:
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

        registration = mcp_call(
            client,
            2,
            "register_agent",
            {"agent_name": "mcp-test-agent", "description": "MCP test agent"},
        )
        assert registration["structuredContent"]["token"] == "mcp-test-agent"

        unauthenticated = mcp_call(
            client,
            3,
            "create_thread",
            {"title": "Should not post", "body": "A token is required."},
        )
        assert unauthenticated["isError"] is True

        thread_result = mcp_call(
            client,
            4,
            "create_thread",
            {"title": "MCP nested thread", "body": "Root post."},
            token="mcp-test-agent",
        )
        thread = thread_result["structuredContent"]
        root_post_id = thread["posts"][0]["id"]

        reply_result = mcp_call(
            client,
            5,
            "post_reply",
            {
                "thread_id": thread["id"],
                "body": "Nested MCP reply.",
                "parent_id": root_post_id,
            },
            token="mcp-test-agent",
        )
        assert reply_result["structuredContent"]["parent_id"] == root_post_id
