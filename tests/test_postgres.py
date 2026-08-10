from __future__ import annotations

import os
from uuid import uuid4

import pytest

from threadstack.models import AgentCreate, PostCreate, PostType, ThreadCreate
from threadstack.postgres_store import PostgresStore

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
    finally:
        reopened.close()
