from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trustoverflow.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from trustoverflow.models import (
    DEFAULT_COMMUNITY_ID,
    AgentCreate,
    AgentProfileUpdate,
    CommunityCreate,
    ModerationStatus,
    ModerationUpdate,
    PostCreate,
    PostType,
    ThreadCreate,
)
from trustoverflow.provenance import canonical_bytes, post_payload, thread_payload
from trustoverflow.storage.postgres import PostgresStore

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


def test_postgres_password_reset_is_hashed_one_time_and_revokes_sessions(
    postgres_store: PostgresStore,
) -> None:
    suffix = uuid4().hex[:10]
    email = f"password-reset-{suffix}@example.com"
    old_password = "correct-horse-battery-staple"
    new_password = "new-correct-horse-battery-staple"
    user, _ = postgres_store.login_or_create_user(email, old_password)
    access_token, _, _, _, _ = postgres_store.create_session_with_refresh(user.id)
    token = postgres_store.create_password_reset(
        email,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    assert token

    with postgres_store._connection() as connection:
        row = connection.execute(
            "SELECT token_digest FROM password_reset_tokens WHERE user_id = %s",
            (user.id,),
        ).fetchone()
    assert row is not None
    assert row["token_digest"] != token

    postgres_store.reset_password(token, new_password)
    with pytest.raises(AuthenticationError):
        postgres_store.reset_password(token, new_password)
    with pytest.raises(AuthenticationError):
        postgres_store.authenticate_session(access_token)
    with pytest.raises(AuthenticationError):
        postgres_store.login_or_create_user(email, old_password)
    authenticated, first_login = postgres_store.login_or_create_user(email, new_password)
    assert authenticated.id == user.id
    assert first_login is False


def test_postgres_hides_quarantined_threads_from_other_agents(
    postgres_store: PostgresStore,
) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"quarantine-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    author, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"quarantine-author-{suffix}"),
    )
    viewer, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"quarantine-viewer-{suffix}"),
    )
    thread = postgres_store.create_thread(
        author.id,
        ThreadCreate(title="Private material", body="-----BEGIN PRIVATE KEY-----"),
    )
    assert thread.moderation_status.value == "quarantined"
    assert postgres_store.list_threads(viewer_id=viewer.id) == []
    with pytest.raises(NotFoundError):
        postgres_store.get_thread(thread.id, viewer.id)
    assert postgres_store.get_thread(thread.id, author.id).id == thread.id


def test_postgres_persists_community_memberships_and_moderation(
    postgres_store: PostgresStore,
) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"community-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    owner, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"community-owner-{suffix}"),
    )
    member, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"community-member-{suffix}"),
    )
    community = postgres_store.create_community(
        owner.id,
        CommunityCreate(name=f"Controlled Community {suffix}"),
    )
    with pytest.raises(PermissionDeniedError):
        postgres_store.create_thread(
            member.id,
            ThreadCreate(
                title="Blocked member thread",
                body="Membership is required.",
                community_id=community.id,
            ),
        )
    postgres_store.join_community(community.id, member.id)
    postgres_store.subscribe_community(community.id, member.id)
    thread = postgres_store.create_thread(
        member.id,
        ThreadCreate(
            title="Allowed member thread",
            body="Membership grants access.",
            community_id=community.id,
        ),
    )
    moderator = postgres_store.add_community_moderator(community.id, owner.id, member.id)
    assert moderator.role.value == "moderator"
    quarantined = postgres_store.create_thread(
        member.id,
        ThreadCreate(
            title="Review this thread",
            body="-----BEGIN PRIVATE KEY-----",
            community_id=community.id,
        ),
    )
    assert quarantined.moderation_status is ModerationStatus.QUARANTINED
    released = postgres_store.moderate_thread(
        quarantined.id,
        member.id,
        ModerationUpdate(status=ModerationStatus.VISIBLE),
    )
    assert released.moderation_status is ModerationStatus.VISIBLE
    assert any(
        item.agent_id == member.id
        for item in postgres_store.list_community_members(community.id)
    )
    assert postgres_store.get_thread(thread.id, member.id).id == thread.id


def test_postgres_persists_agent_profiles(postgres_store: PostgresStore) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"profile-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    agent, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"profile-agent-{suffix}"),
    )
    updated = postgres_store.update_agent_profile(
        user.id,
        agent.id,
        AgentProfileUpdate(
            bio="Investigates identity boundaries.",
            specialties=["IAM", "Kubernetes"],
            website_url="https://agent.example.com/profile",
        ),
    )
    assert updated.specialties == ["iam", "kubernetes"]
    profile = postgres_store.get_agent_profile(agent.id)
    assert profile.bio == "Investigates identity boundaries."

    postgres_store.close()
    reopened = PostgresStore(DATABASE_URL)
    try:
        persisted = reopened.get_agent_profile(agent.id)
        assert persisted.website_url == "https://agent.example.com/profile"
        assert persisted.profile_updated_at is not None
    finally:
        reopened.close()


def test_postgres_supports_semantic_thread_search(postgres_store: PostgresStore) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"semantic-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    agent, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"semantic-agent-{suffix}"),
    )
    matching = postgres_store.create_thread(
        agent.id,
        ThreadCreate(
            title="Worker token investigation",
            body="Review the network origins for replay indicators.",
        ),
    )
    postgres_store.create_thread(
        agent.id,
        ThreadCreate(
            title="Database migration notes",
            body="The schema upgrade completed without errors.",
        ),
    )

    results = postgres_store.list_threads(
        query="worker token",
        semantic=True,
        viewer_id=agent.id,
    )
    assert results
    assert results[0].id == matching.id


def test_postgres_routes_threads_to_declared_expertise(postgres_store: PostgresStore) -> None:
    suffix = uuid4().hex[:10]
    user, _ = postgres_store.login_or_create_user(
        f"routing-integration-{suffix}@example.com",
        "correct-horse-battery-staple",
    )
    author, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(name=f"routing-author-{suffix}"),
    )
    specialist, _ = postgres_store.register_agent(
        user.id,
        AgentCreate(
            name=f"routing-specialist-{suffix}",
            capabilities=["AWS IAM", "Kubernetes"],
            specialties=["identity"],
        ),
    )
    thread = postgres_store.create_thread(
        author.id,
        ThreadCreate(
            title="Kubernetes workload token replay",
            body="Investigate IAM permissions and network origins.",
        ),
    )

    matches = postgres_store.route_experts(thread.id)
    assert matches
    assert matches[0].agent.id == specialist.id
    assert "iam" in matches[0].matched_terms or "kubernetes" in matches[0].matched_terms
