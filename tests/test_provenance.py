from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from threadstack.api import create_app
from threadstack.models import DEFAULT_COMMUNITY_ID, PostType
from threadstack.provenance import canonical_bytes, payload_hash, post_payload, thread_payload


def _keypair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_key, base64.b64encode(public_key).decode("ascii")


def _signature(private_key: Ed25519PrivateKey, payload: dict) -> str:
    return base64.b64encode(private_key.sign(canonical_bytes(payload))).decode("ascii")


def test_signed_thread_and_post_provenance_is_verified_and_tampering_is_rejected() -> None:
    private_key, public_key = _keypair()
    client = TestClient(create_app())
    client.post(
        "/auth/login",
        json={"email": "signed-owner@example.com", "password": "correct-horse-battery-staple"},
    )
    registration = client.post(
        "/auth/me/agents",
        json={"name": "signed-agent", "public_key": public_key},
    )
    assert registration.status_code == 201
    agent = registration.json()["agent"]
    token = registration.json()["token"]
    headers = {"authorization": f"Bearer {token}"}

    title = "Signed investigation"
    body = "The signed root question."
    tags = ["iam", "security"]
    thread_signature = _signature(
        private_key,
        thread_payload(
            title=title,
            body=body,
            tags=tags,
            community_id=DEFAULT_COMMUNITY_ID,
        ),
    )
    response = client.post(
        "/threads",
        headers=headers,
        json={"title": title, "body": body, "tags": tags, "signature": thread_signature},
    )
    assert response.status_code == 201
    thread = response.json()
    assert thread["provenance"] == {
        "signer_id": agent["id"],
        "payload_hash": payload_hash(
            thread_payload(
                title=title,
                body=body,
                tags=tags,
                community_id=DEFAULT_COMMUNITY_ID,
            )
        ),
        "signature": thread_signature,
        "verified": True,
    }

    post_body = "The signed answer."
    post_payload_value = post_payload(
        thread_id=thread["id"],
        parent_id=thread["posts"][0]["id"],
        post_type=PostType.ANSWER,
        body=post_body,
        evidence=[],
        confidence=0.8,
    )
    post_signature = _signature(private_key, post_payload_value)
    response = client.post(
        f"/threads/{thread['id']}/posts",
        headers=headers,
        json={
            "type": "answer",
            "body": post_body,
            "parent_id": thread["posts"][0]["id"],
            "confidence": 0.8,
            "signature": post_signature,
        },
    )
    assert response.status_code == 201
    assert response.json()["provenance"]["verified"] is True

    response = client.post(
        f"/threads/{thread['id']}/posts",
        headers=headers,
        json={
            "type": "answer",
            "body": "A tampered answer.",
            "parent_id": thread["posts"][0]["id"],
            "confidence": 0.8,
            "signature": post_signature,
        },
    )
    assert response.status_code == 422


def test_invalid_agent_public_key_is_rejected() -> None:
    client = TestClient(create_app())
    client.post(
        "/auth/login",
        json={"email": "invalid-key-owner@example.com", "password": "correct-horse-battery-staple"},
    )
    response = client.post(
        "/auth/me/agents",
        json={"name": "invalid-key-agent", "public_key": "not-a-key"},
    )
    assert response.status_code == 422
