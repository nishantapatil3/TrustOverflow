from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from threadstack.models import (
    DEFAULT_COMMUNITY_ID,
    AgentCreate,
    PostCreate,
    PostType,
    ThreadCreate,
)
from threadstack.postgres_store import PostgresStore
from threadstack.provenance import canonical_bytes, post_payload, thread_payload
from threadstack.store import AuthenticationError

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres_store() -> PostgresStore:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is not configured")
    store = PostgresStore(DATABASE_URL)
    try:
        yield store
    finally:
        store.close()


def test_postgres_persists_nested_posts_and_upvotes(postgres_store: PostgresStore) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    agent, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"integration-agent-{suffix}"),
    )
    thread = postgres_store.create_thread(
        agent.id,
        ThreadCreate(title="Persisted integration thread", body="Root question."),
    )
    comment = postgres_store.add_post(
        thread.id,
        agent.id,
        PostCreate(
            type=PostType.ANSWER,
            body="Nested persisted comment.",
            parent_id=thread.posts[0].id,
        ),
    )

    assert postgres_store.upvote_thread(thread.id, agent.id).score == 1
    assert postgres_store.upvote_post(comment.id, agent.id).score == 1

    postgres_store.close()
    reopened = PostgresStore(DATABASE_URL)
    try:
        persisted = reopened.get_thread(thread.id, agent.id)
        persisted_comment = next(post for post in persisted.posts if post.id == comment.id)
        assert persisted.score == 1
        assert persisted.upvoted is True
        assert persisted_comment.score == 1
        assert persisted_comment.upvoted is True
        with reopened._connection() as connection:
            actions = {
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM audit_events WHERE actor_id = %s",
                    (agent.id,),
                ).fetchall()
            }
        assert {"thread.created", "post.created", "thread.upvoted", "post.upvoted"} <= actions
    finally:
        reopened.close()


def test_postgres_persists_verified_message_provenance(postgres_store: PostgresStore) -> None:
    suffix = uuid4().hex[:10]
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    user, _ = postgres_store.login_or_create_user(
        f"signed-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    agent, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"signed-integration-agent-{suffix}", public_key=public_key),
    )
    title = "Signed persisted thread"
    body = "The signed persisted question."
    thread_signature = base64.b64encode(
        private_key.sign(
            canonical_bytes(
                thread_payload(
                    title=title,
                    body=body,
                    tags=["persistence"],
                    community_id=DEFAULT_COMMUNITY_ID,
                )
            )
        )
    ).decode("ascii")
    thread = postgres_store.create_thread(
        agent.id,
        ThreadCreate(title=title, body=body, tags=["persistence"], signature=thread_signature),
    )
    answer_body = "The signed persisted answer."
    answer_payload = post_payload(
        thread_id=thread.id,
        parent_id=thread.posts[0].id,
        post_type=PostType.ANSWER,
        body=answer_body,
        evidence=[],
        confidence=0.9,
    )
    answer_signature = base64.b64encode(
        private_key.sign(canonical_bytes(answer_payload))
    ).decode("ascii")
    postgres_store.add_post(
        thread.id,
        agent.id,
        PostCreate(
            type=PostType.ANSWER,
            body=answer_body,
            parent_id=thread.posts[0].id,
            confidence=0.9,
            signature=answer_signature,
        ),
    )

    postgres_store.close()
    reopened = PostgresStore(DATABASE_URL)
    try:
        persisted = reopened.get_thread(thread.id, agent.id)
        persisted_answer = next(post for post in persisted.posts if post.parent_id is not None)
        assert persisted.provenance is not None
        assert persisted.provenance.verified is True
        assert persisted_answer.provenance is not None
        assert persisted_answer.provenance.verified is True
        assert persisted_answer.provenance.signature == answer_signature
    finally:
        reopened.close()


def test_postgres_rotates_refresh_tokens_and_detects_reuse(
    postgres_store: PostgresStore,
) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"refresh-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    agent, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"refresh-integration-agent-{suffix}"),
    )
    access_token, _, refresh_token, _, _ = postgres_store.create_session_with_refresh(agent.id)
    assert postgres_store.authenticate_session(access_token).id == agent.id

    new_access, _, new_refresh, _, _ = postgres_store.refresh_session(refresh_token)
    assert new_access != access_token
    assert new_refresh != refresh_token
    assert postgres_store.authenticate_session(new_access).id == agent.id

    with pytest.raises(AuthenticationError, match="reuse"):
        postgres_store.refresh_session(refresh_token)
    with pytest.raises(AuthenticationError):
        postgres_store.refresh_session(new_refresh)
