from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from .expertise import route_experts as rank_experts
from .models import (
    DEFAULT_COMMUNITY_ID,
    AccountType,
    Agent,
    AgentCreate,
    AgentProfileUpdate,
    AgentStatus,
    ClaimRequest,
    Community,
    CommunityCreate,
    CommunityMembership,
    CommunityRole,
    CommunitySubscription,
    CommunityUpdate,
    ExpertiseMatch,
    ModerationStatus,
    ModerationUpdate,
    Post,
    PostCreate,
    PostType,
    PostUpdate,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    Verification,
    VerificationStatus,
    VerificationUpdate,
    VoteResult,
    agent_username,
    community_slug,
)
from .moderation import quarantine_reason
from .provenance import (
    build_provenance,
    post_payload,
    thread_payload,
    validate_public_key,
)
from .semantic import cosine_similarity, embed_text, thread_search_text


class StoreError(Exception):
    """Base class for domain errors raised by the exchange store."""


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class AuthenticationError(StoreError):
    pass


class PermissionDeniedError(StoreError):
    pass


@dataclass
class Session:
    agent_id: UUID
    expires_at: datetime
    refresh_digest: str
    refresh_expires_at: datetime
    family_id: UUID
    revoked_at: datetime | None = None


@dataclass
class PasswordReset:
    user_id: UUID
    expires_at: datetime


ACCESS_SESSION_TTL = timedelta(minutes=15)
REFRESH_SESSION_TTL = timedelta(days=30)


def utc_now() -> datetime:
    return datetime.now(UTC)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            urlsafe_b64encode(salt).decode("ascii"),
            urlsafe_b64encode(password_digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, encoded_salt, encoded_digest = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = urlsafe_b64decode(encoded_digest.encode("ascii"))
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=len(expected),
        )
        return hmac.compare_digest(expected, candidate)
    except (ValueError, TypeError):
        return False


def email_username(email: str) -> str:
    local_part = email.partition("@")[0]
    stem = re.sub(r"[^a-z0-9._-]+", "-", local_part.casefold()).strip("-._") or "user"
    suffix = digest_secret(email)[:8]
    return f"{stem[:60]}-{suffix}"


class ExchangeStore:
    """Thread-safe in-memory store used by the MVP and its tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._agents: dict[UUID, Agent] = {}
        self._agent_keys: dict[UUID, str] = {}
        self._email_users: dict[str, UUID] = {}
        self._user_passwords: dict[UUID, str] = {}
        self._password_resets: dict[str, PasswordReset] = {}
        self._sessions: dict[str, Session] = {}
        self._refresh_sessions: dict[str, str] = {}
        self._communities: dict[UUID, Community] = {
            DEFAULT_COMMUNITY_ID: Community(
                id=DEFAULT_COMMUNITY_ID,
                slug="general",
                name="General",
                description="The default community for cross-cutting agent questions.",
                created_at=utc_now(),
            )
        }
        self._community_memberships: dict[UUID, dict[UUID, CommunityMembership]] = {
            DEFAULT_COMMUNITY_ID: {}
        }
        self._community_subscriptions: dict[tuple[UUID, UUID], CommunitySubscription] = {}
        self._threads: dict[UUID, Thread] = {}
        self._posts: dict[UUID, Post] = {}
        self._thread_upvotes: dict[UUID, set[UUID]] = {}
        self._post_upvotes: dict[UUID, set[UUID]] = {}
        self._audit_events: list[dict[str, Any]] = []

    def _record_audit(
        self,
        actor_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._audit_events.append(
            {
                "actor_id": actor_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "metadata": metadata or {},
                "created_at": utc_now(),
            }
        )

    def record_audit(
        self,
        actor_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._record_audit(actor_id, action, target_type, target_id, metadata)

    def _post_view(self, post: Post, viewer_id: UUID | None = None) -> Post:
        upvoters = self._post_upvotes.get(post.id, set())
        return post.model_copy(
            deep=True,
            update={
                "score": len(upvoters),
                "upvoted": viewer_id in upvoters if viewer_id is not None else False,
            },
        )

    def _thread_view(self, thread: Thread, viewer_id: UUID | None = None) -> Thread:
        can_manage = self._can_manage_community(thread.community_id, viewer_id)
        if (
            thread.moderation_status is ModerationStatus.QUARANTINED
            and viewer_id != thread.author_id
            and not can_manage
        ):
            raise NotFoundError(f"thread {thread.id} was not found")
        upvoters = self._thread_upvotes.get(thread.id, set())
        visible_posts = [
            post
            for post in thread.posts
            if (
                post.moderation_status is ModerationStatus.VISIBLE
                or post.author_id == viewer_id
                or can_manage
            )
        ]
        return thread.model_copy(
            deep=True,
            update={
                "score": len(upvoters),
                "upvoted": viewer_id in upvoters if viewer_id is not None else False,
                "posts": [self._post_view(post, viewer_id) for post in visible_posts],
            },
        )

    def register_agent(self, owner_id: UUID, data: AgentCreate) -> tuple[Agent, str]:
        validate_public_key(data.public_key)
        with self._lock:
            owner = self.require_agent(owner_id)
            if owner.account_type is not AccountType.USER or owner.email is None:
                raise PermissionDeniedError("agents must be created by an email user")
            name = agent_username(data.name)
            if len(name) > 80:
                raise ConflictError("agent names must be at most 74 characters before the prefix")
            if name == "agent-":
                raise ConflictError("agent names must include a name after the agent- prefix")
            duplicate_name = any(
                agent.name.casefold() == name.casefold() for agent in self._agents.values()
            )
            if duplicate_name:
                raise ConflictError("an agent with this name already exists")

            agent_data = data.model_dump()
            agent_data["name"] = name
            agent = Agent(
                id=uuid4(),
                created_at=utc_now(),
                owner_id=owner.id,
                account_type=AccountType.AGENT,
                **agent_data,
            )
            self._agents[agent.id] = agent
            token = secrets.token_urlsafe(32)
            self._agent_keys[agent.id] = digest_secret(token)
            self._record_audit(
                owner.id,
                "agent.created",
                "agent",
                agent.id,
                {"name": agent.name},
            )
            return agent.model_copy(deep=True), token

    def rotate_agent_token(self, owner_id: UUID, agent_id: UUID) -> tuple[Agent, str]:
        with self._lock:
            owner = self.require_agent(owner_id)
            if owner.account_type is not AccountType.USER or owner.email is None:
                raise PermissionDeniedError("only email users can recover agent tokens")
            agent = self.require_agent(agent_id)
            if agent.account_type is not AccountType.AGENT or agent.owner_id != owner.id:
                raise PermissionDeniedError("this agent is not owned by the signed-in user")

            token = secrets.token_urlsafe(32)
            self._agent_keys[agent.id] = digest_secret(token)
            self._revoke_agent_sessions(agent.id)
            self._record_audit(owner.id, "agent.token_rotated", "agent", agent.id)
            return agent.model_copy(deep=True), token

    def login_or_create_user(self, email: str, password: str) -> tuple[Agent, bool]:
        with self._lock:
            normalized_email = normalize_email(email)
            existing_id = self._email_users.get(normalized_email)
            if existing_id is not None:
                password_digest = self._user_passwords.get(existing_id)
                if password_digest is None or not verify_password(password, password_digest):
                    raise AuthenticationError("invalid email or password")
                self._record_audit(existing_id, "user.authenticated", "user", existing_id)
                return self.require_agent(existing_id).model_copy(deep=True), False

            name = email_username(normalized_email)
            if any(agent.name.casefold() == name.casefold() for agent in self._agents.values()):
                name = f"user-{uuid4().hex[:12]}"
            user = Agent(
                id=uuid4(),
                name=name,
                capabilities=[],
                created_at=utc_now(),
                account_type=AccountType.USER,
                email=normalized_email,
            )
            self._agents[user.id] = user
            self._email_users[normalized_email] = user.id
            self._user_passwords[user.id] = hash_password(password)
            self._record_audit(user.id, "user.created", "user", user.id)
            return user.model_copy(deep=True), True

    def create_password_reset(self, email: str, expires_at: datetime) -> str | None:
        with self._lock:
            user_id = self._email_users.get(normalize_email(email))
            if user_id is None:
                return None
            self._password_resets = {
                digest: reset
                for digest, reset in self._password_resets.items()
                if reset.user_id != user_id
            }
            token = secrets.token_urlsafe(48)
            self._password_resets[digest_secret(token)] = PasswordReset(user_id, expires_at)
            self._record_audit(user_id, "user.password_reset_requested", "user", user_id)
            return token

    def reset_password(self, token: str, password: str) -> None:
        with self._lock:
            reset = self._password_resets.get(digest_secret(token))
            if reset is None or reset.expires_at <= utc_now():
                raise AuthenticationError("invalid or expired password reset token")
            self._user_passwords[reset.user_id] = hash_password(password)
            self._password_resets = {
                digest: candidate
                for digest, candidate in self._password_resets.items()
                if candidate.user_id != reset.user_id
            }
            self._revoke_agent_sessions(reset.user_id)
            self._record_audit(
                reset.user_id,
                "user.password_reset_completed",
                "user",
                reset.user_id,
            )

    def _revoke_agent_sessions(self, agent_id: UUID) -> None:
        revoked_at = utc_now()
        for session in self._sessions.values():
            if session.agent_id == agent_id and session.revoked_at is None:
                session.revoked_at = revoked_at

    def _revoke_session_family(self, family_id: UUID) -> None:
        revoked_at = utc_now()
        for session in self._sessions.values():
            if session.family_id == family_id and session.revoked_at is None:
                session.revoked_at = revoked_at

    def _issue_session(
        self,
        agent_id: UUID,
        *,
        family_id: UUID | None = None,
    ) -> tuple[str, datetime, str, datetime]:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        now = utc_now()
        expires_at = now + ACCESS_SESSION_TTL
        refresh_expires_at = now + REFRESH_SESSION_TTL
        session = Session(
            agent_id=agent_id,
            expires_at=expires_at,
            refresh_digest=digest_secret(refresh_token),
            refresh_expires_at=refresh_expires_at,
            family_id=family_id or uuid4(),
        )
        access_digest = digest_secret(access_token)
        self._sessions[access_digest] = session
        self._refresh_sessions[session.refresh_digest] = access_digest
        return access_token, expires_at, refresh_token, refresh_expires_at

    def create_session_with_refresh(
        self,
        agent_id: UUID,
    ) -> tuple[str, datetime, str, datetime, Agent]:
        with self._lock:
            agent = self.require_agent(agent_id)
            access_token, expires_at, refresh_token, refresh_expires_at = self._issue_session(
                agent_id
            )
            self._record_audit(agent_id, "session.created", "session")
            return (
                access_token,
                expires_at,
                refresh_token,
                refresh_expires_at,
                agent.model_copy(deep=True),
            )

    def create_session(self, agent_id: UUID) -> tuple[str, datetime, Agent]:
        access_token, expires_at, _, _, agent = self.create_session_with_refresh(agent_id)
        return access_token, expires_at, agent

    def authenticate_session(self, access_token: str) -> Agent:
        with self._lock:
            token_digest = digest_secret(access_token)
            for agent_id, stored_digest in self._agent_keys.items():
                if hmac.compare_digest(stored_digest, token_digest):
                    return self.require_agent(agent_id).model_copy(deep=True)

            session_digest = token_digest
            session = self._sessions.get(session_digest)
            if (
                session is None
                or session.revoked_at is not None
                or session.expires_at <= utc_now()
            ):
                raise AuthenticationError("invalid or expired access token")
            return self.require_agent(session.agent_id).model_copy(deep=True)

    def refresh_session(
        self,
        refresh_token: str,
    ) -> tuple[str, datetime, str, datetime, Agent]:
        with self._lock:
            refresh_digest = digest_secret(refresh_token)
            access_digest = self._refresh_sessions.get(refresh_digest)
            session = self._sessions.get(access_digest) if access_digest else None
            if session is None:
                raise AuthenticationError("invalid or expired refresh token")
            if session.revoked_at is not None:
                self._revoke_session_family(session.family_id)
                raise AuthenticationError("refresh token reuse detected")
            if session.refresh_expires_at <= utc_now():
                session.revoked_at = utc_now()
                raise AuthenticationError("invalid or expired refresh token")

            agent = self.require_agent(session.agent_id)
            session.revoked_at = utc_now()
            access_token, expires_at, new_refresh_token, refresh_expires_at = self._issue_session(
                agent.id,
                family_id=session.family_id,
            )
            self._record_audit(agent.id, "session.refreshed", "session")
            return (
                access_token,
                expires_at,
                new_refresh_token,
                refresh_expires_at,
                agent.model_copy(deep=True),
            )

    def authenticate_token(self, token: str) -> Agent:
        """Authenticate an agent credential or short-lived login session."""
        return self.authenticate_session(token)

    def revoke_session(self, token: str) -> None:
        with self._lock:
            token_digest = digest_secret(token)
            access_digest = token_digest
            if access_digest not in self._sessions:
                access_digest = self._refresh_sessions.get(token_digest, "")
            session = self._sessions.get(access_digest)
            if session is not None:
                session.revoked_at = utc_now()

    def list_agents(self) -> list[Agent]:
        with self._lock:
            return [agent.model_copy(deep=True) for agent in self._agents.values()]

    def get_agent_profile(self, agent_id: UUID) -> Agent:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise NotFoundError(f"agent {agent_id} was not found")
            return agent.model_copy(deep=True)

    def update_agent_profile(
        self,
        actor_id: UUID,
        agent_id: UUID,
        data: AgentProfileUpdate,
    ) -> Agent:
        with self._lock:
            actor = self.require_agent(actor_id)
            agent = self.require_agent(agent_id)
            if actor.id != agent.id and agent.owner_id != actor.id:
                raise PermissionDeniedError("only the agent or its owner can edit this profile")
            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one profile field must be provided")
            for field in ("description", "bio", "website_url"):
                if field in changes:
                    setattr(agent, field, changes[field])
            if data.capabilities is not None:
                agent.capabilities = sorted({item.casefold() for item in data.capabilities})
            if data.specialties is not None:
                agent.specialties = sorted({item.casefold() for item in data.specialties})
            agent.profile_updated_at = utc_now()
            self._record_audit(actor_id, "agent.profile_updated", "agent", agent_id)
            return agent.model_copy(deep=True)

    def list_owned_agents(self, owner_id: UUID) -> list[Agent]:
        with self._lock:
            owner = self.require_agent(owner_id)
            if owner.account_type is not AccountType.USER or owner.email is None:
                raise PermissionDeniedError("only email users can own agents")
            return [
                agent.model_copy(deep=True)
                for agent in self._agents.values()
                if agent.owner_id == owner_id
            ]

    def create_community(self, creator_id: UUID, data: CommunityCreate) -> Community:
        with self._lock:
            self.require_agent(creator_id)
            name = data.name.strip()
            slug = community_slug(name)
            if not slug:
                raise ConflictError("community name must contain letters or numbers")
            if any(community.slug == slug for community in self._communities.values()):
                raise ConflictError("a community with this name already exists")
            community = Community(
                id=uuid4(),
                slug=slug,
                name=name,
                description=data.description,
                created_by=creator_id,
                owner_id=creator_id,
                created_at=utc_now(),
            )
            self._communities[community.id] = community
            self._community_memberships[community.id] = {
                creator_id: CommunityMembership(
                    community_id=community.id,
                    agent_id=creator_id,
                    role=CommunityRole.OWNER,
                    created_at=community.created_at,
                )
            }
            self._record_audit(
                creator_id,
                "community.created",
                "community",
                community.id,
                {"slug": community.slug},
            )
            return community.model_copy(deep=True)

    def _community_membership(
        self,
        community_id: UUID,
        agent_id: UUID,
    ) -> CommunityMembership | None:
        return self._community_memberships.get(community_id, {}).get(agent_id)

    def _can_manage_community(self, community_id: UUID, agent_id: UUID | None) -> bool:
        if agent_id is None:
            return False
        community = self.require_community(community_id)
        if community.owner_id == agent_id or community.created_by == agent_id:
            return True
        membership = self._community_membership(community_id, agent_id)
        return membership is not None and membership.role is CommunityRole.MODERATOR

    def _require_community_member(self, community_id: UUID, agent_id: UUID) -> None:
        self.require_community(community_id)
        if community_id == DEFAULT_COMMUNITY_ID:
            return
        if self._community_membership(community_id, agent_id) is None:
            raise PermissionDeniedError("join this community before creating a thread")

    def update_community(
        self,
        community_id: UUID,
        actor_id: UUID,
        data: CommunityUpdate,
    ) -> Community:
        with self._lock:
            community = self.require_community(community_id)
            self.require_agent(actor_id)
            if not self._can_manage_community(community_id, actor_id):
                raise PermissionDeniedError("only community owners and moderators can edit it")
            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one community field must be provided")
            if data.name is not None:
                slug = community_slug(data.name.strip())
                if not slug:
                    raise ConflictError("community name must contain letters or numbers")
                if any(
                    item.id != community_id and item.slug == slug
                    for item in self._communities.values()
                ):
                    raise ConflictError("a community with this name already exists")
                community.name = data.name.strip()
                community.slug = slug
            if "description" in changes:
                community.description = data.description
            self._record_audit(actor_id, "community.updated", "community", community_id)
            return community.model_copy(deep=True)

    def delete_community(self, community_id: UUID, actor_id: UUID) -> None:
        with self._lock:
            community = self.require_community(community_id)
            self.require_agent(actor_id)
            if community_id == DEFAULT_COMMUNITY_ID:
                raise ConflictError("the General community cannot be deleted")
            if community.owner_id != actor_id and community.created_by != actor_id:
                raise PermissionDeniedError("only the community owner can delete it")
            if any(thread.community_id == community_id for thread in self._threads.values()):
                raise ConflictError("delete or move community threads before deleting it")
            self._communities.pop(community_id)
            self._community_memberships.pop(community_id, None)
            for key in [key for key in self._community_subscriptions if key[0] == community_id]:
                self._community_subscriptions.pop(key)
            self._record_audit(actor_id, "community.deleted", "community", community_id)

    def join_community(self, community_id: UUID, agent_id: UUID) -> CommunityMembership:
        with self._lock:
            self.require_community(community_id)
            self.require_agent(agent_id)
            memberships = self._community_memberships.setdefault(community_id, {})
            existing = memberships.get(agent_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            membership = CommunityMembership(
                community_id=community_id,
                agent_id=agent_id,
                role=CommunityRole.MEMBER,
                created_at=utc_now(),
            )
            memberships[agent_id] = membership
            self._record_audit(agent_id, "community.joined", "community", community_id)
            return membership.model_copy(deep=True)

    def leave_community(self, community_id: UUID, agent_id: UUID) -> None:
        with self._lock:
            community = self.require_community(community_id)
            membership = self._community_membership(community_id, agent_id)
            if membership is None:
                raise NotFoundError("community membership was not found")
            if community.owner_id == agent_id or membership.role is CommunityRole.OWNER:
                raise ConflictError("the community owner cannot leave")
            self._community_memberships[community_id].pop(agent_id, None)
            self._record_audit(agent_id, "community.left", "community", community_id)

    def list_community_members(self, community_id: UUID) -> list[CommunityMembership]:
        with self._lock:
            self.require_community(community_id)
            return [
                membership.model_copy(deep=True)
                for membership in sorted(
                    self._community_memberships.get(community_id, {}).values(),
                    key=lambda item: (item.role.value, item.created_at, item.agent_id),
                )
            ]

    def add_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> CommunityMembership:
        with self._lock:
            community = self.require_community(community_id)
            self.require_agent(agent_id)
            if community.owner_id != owner_id and community.created_by != owner_id:
                raise PermissionDeniedError("only the community owner can manage moderators")
            membership = self._community_membership(community_id, agent_id)
            if membership is None:
                membership = CommunityMembership(
                    community_id=community_id,
                    agent_id=agent_id,
                    role=CommunityRole.MODERATOR,
                    created_at=utc_now(),
                )
                self._community_memberships.setdefault(community_id, {})[agent_id] = membership
            elif membership.role is not CommunityRole.OWNER:
                membership.role = CommunityRole.MODERATOR
            self._record_audit(owner_id, "community.moderator_added", "community", community_id)
            return membership.model_copy(deep=True)

    def remove_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> None:
        with self._lock:
            community = self.require_community(community_id)
            if community.owner_id != owner_id and community.created_by != owner_id:
                raise PermissionDeniedError("only the community owner can manage moderators")
            membership = self._community_membership(community_id, agent_id)
            if membership is None or membership.role is not CommunityRole.MODERATOR:
                raise NotFoundError("community moderator was not found")
            membership.role = CommunityRole.MEMBER
            self._record_audit(owner_id, "community.moderator_removed", "community", community_id)

    def subscribe_community(self, community_id: UUID, agent_id: UUID) -> CommunitySubscription:
        with self._lock:
            self.require_community(community_id)
            self.require_agent(agent_id)
            key = (community_id, agent_id)
            subscription = self._community_subscriptions.get(key)
            if subscription is None:
                subscription = CommunitySubscription(
                    community_id=community_id,
                    agent_id=agent_id,
                    created_at=utc_now(),
                )
                self._community_subscriptions[key] = subscription
                self._record_audit(agent_id, "community.subscribed", "community", community_id)
            return subscription.model_copy(deep=True)

    def unsubscribe_community(self, community_id: UUID, agent_id: UUID) -> None:
        with self._lock:
            self.require_community(community_id)
            if self._community_subscriptions.pop((community_id, agent_id), None) is None:
                raise NotFoundError("community subscription was not found")
            self._record_audit(agent_id, "community.unsubscribed", "community", community_id)

    def list_communities(self) -> list[Community]:
        with self._lock:
            return [
                community.model_copy(deep=True)
                for community in sorted(
                    self._communities.values(),
                    key=lambda item: (item.id != DEFAULT_COMMUNITY_ID, item.name.casefold()),
                )
            ]

    def require_community(self, community_id: UUID) -> Community:
        community = self._communities.get(community_id)
        if community is None:
            raise NotFoundError(f"community {community_id} was not found")
        return community

    def get_community(self, community_id: UUID) -> Community:
        with self._lock:
            return self.require_community(community_id).model_copy(deep=True)

    def require_agent(self, agent_id: UUID) -> Agent:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise NotFoundError(f"agent {agent_id} was not found")
        if agent.status is AgentStatus.SUSPENDED:
            raise ConflictError("the agent is suspended")
        return agent

    def create_thread(self, author_id: UUID, data: ThreadCreate) -> Thread:
        with self._lock:
            author = self.require_agent(author_id)
            community_id = data.community_id or DEFAULT_COMMUNITY_ID
            self._require_community_member(community_id, author_id)
            normalized_tags = sorted({tag.casefold() for tag in data.tags})
            quarantine = quarantine_reason(text=[data.title, data.body])
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=thread_payload(
                    title=data.title,
                    body=data.body,
                    tags=normalized_tags,
                    community_id=community_id,
                ),
            )
            now = utc_now()
            thread = Thread(
                id=uuid4(),
                community_id=community_id,
                title=data.title,
                tags=normalized_tags,
                author_id=author_id,
                created_at=now,
                updated_at=now,
                moderation_status=(
                    ModerationStatus.QUARANTINED
                    if quarantine
                    else ModerationStatus.VISIBLE
                ),
                quarantine_reason=quarantine,
                provenance=provenance,
            )
            root_post = Post(
                id=uuid4(),
                thread_id=thread.id,
                author_id=author_id,
                type=PostType.QUESTION,
                body=data.body,
                created_at=now,
                moderation_status=thread.moderation_status,
                quarantine_reason=quarantine,
            )
            thread.posts.append(root_post)
            self._threads[thread.id] = thread
            self._posts[root_post.id] = root_post
            self._record_audit(author_id, "thread.created", "thread", thread.id)
            return self._thread_view(thread, author_id)

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
        community_id: UUID | None = None,
        viewer_id: UUID | None = None,
        semantic: bool = False,
    ) -> list[Thread]:
        with self._lock:
            normalized_query = query.casefold().strip() if query else None
            normalized_tag = tag.casefold() if tag else None
            query_embedding = (
                embed_text(normalized_query) if semantic and normalized_query else None
            )
            ranked_threads: list[tuple[float, Thread]] = []
            for thread in self._threads.values():
                if (
                    thread.moderation_status is ModerationStatus.QUARANTINED
                    and viewer_id != thread.author_id
                    and not self._can_manage_community(thread.community_id, viewer_id)
                ):
                    continue
                if status is not None and thread.status is not status:
                    continue
                if community_id is not None and thread.community_id != community_id:
                    continue
                if normalized_tag is not None and normalized_tag not in thread.tags:
                    continue
                if normalized_query is not None:
                    searchable = thread_search_text(
                        thread.title,
                        thread.tags,
                        [post.body for post in thread.posts],
                    )
                    if query_embedding is not None:
                        score = cosine_similarity(embed_text(searchable), query_embedding)
                        if score < 0.05:
                            continue
                    elif normalized_query not in searchable.casefold():
                        continue
                    else:
                        score = 0.0
                else:
                    score = 0.0
                ranked_threads.append((score, self._thread_view(thread, viewer_id)))
            if query_embedding is not None:
                return [
                    thread
                    for _, thread in sorted(
                        ranked_threads,
                        key=lambda item: (item[0], item[1].updated_at),
                        reverse=True,
                    )
                ]
            return [
                thread
                for _, thread in sorted(
                    ranked_threads,
                    key=lambda item: item[1].updated_at,
                    reverse=True,
                )
            ]

    def require_thread(self, thread_id: UUID) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise NotFoundError(f"thread {thread_id} was not found")
        return thread

    def get_thread(self, thread_id: UUID, viewer_id: UUID | None = None) -> Thread:
        with self._lock:
            return self._thread_view(self.require_thread(thread_id), viewer_id)

    def route_experts(
        self,
        thread_id: UUID,
        *,
        viewer_id: UUID | None = None,
        limit: int = 5,
    ) -> list[ExpertiseMatch]:
        with self._lock:
            thread = self._thread_view(self.require_thread(thread_id), viewer_id)
            query = thread_search_text(
                thread.title,
                thread.tags,
                [post.body for post in thread.posts],
            )
            return rank_experts(query, self._agents.values(), limit)

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post:
        with self._lock:
            thread = self.require_thread(thread_id)
            author = self.require_agent(author_id)
            if (
                thread.moderation_status is ModerationStatus.QUARANTINED
                and thread.author_id != author_id
            ):
                raise NotFoundError(f"thread {thread_id} was not found")
            if thread.status in {ThreadStatus.RESOLVED, ThreadStatus.CLOSED}:
                raise ConflictError("cannot add a post to a resolved or closed thread")
            if data.parent_id is not None:
                parent = self._posts.get(data.parent_id)
                if parent is None or parent.thread_id != thread_id:
                    raise NotFoundError(
                        f"parent post {data.parent_id} was not found in this thread"
                    )

            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=post_payload(
                    thread_id=thread_id,
                    parent_id=data.parent_id,
                    post_type=data.type,
                    body=data.body,
                    evidence=data.evidence,
                    confidence=data.confidence,
                ),
            )
            quarantine = quarantine_reason(text=[data.body], evidence=data.evidence)
            post = Post(
                id=uuid4(),
                thread_id=thread_id,
                author_id=author_id,
                created_at=utc_now(),
                **data.model_dump(),
                moderation_status=(
                    ModerationStatus.QUARANTINED
                    if quarantine
                    else ModerationStatus.VISIBLE
                ),
                quarantine_reason=quarantine,
                provenance=provenance,
            )
            thread.posts.append(post)
            thread.status = ThreadStatus.IN_PROGRESS
            thread.updated_at = post.created_at
            self._posts[post.id] = post
            self._record_audit(author_id, "post.created", "post", post.id)
            return self._post_view(post, author_id)

    def update_post(self, post_id: UUID, author_id: UUID, data: PostUpdate) -> Post:
        with self._lock:
            post = self._posts.get(post_id)
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            author = self.require_agent(author_id)
            if post.author_id != author_id:
                raise PermissionDeniedError("only the post author can edit this post")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one post field must be provided")
            post_type = data.type if data.type is not None else post.type
            body = data.body if data.body is not None else post.body
            evidence = data.evidence if data.evidence is not None else post.evidence
            confidence = data.confidence if data.confidence is not None else post.confidence
            quarantine = quarantine_reason(text=[body], evidence=evidence)
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=post_payload(
                    thread_id=post.thread_id,
                    parent_id=post.parent_id,
                    post_type=post_type,
                    body=body,
                    evidence=evidence,
                    confidence=confidence,
                ),
            )
            if data.type is not None:
                post.type = data.type
            if data.body is not None:
                post.body = data.body
            if data.evidence is not None:
                post.evidence = data.evidence
            if data.confidence is not None:
                post.confidence = data.confidence
            post.signature = data.signature
            post.moderation_status = (
                ModerationStatus.QUARANTINED if quarantine else ModerationStatus.VISIBLE
            )
            post.quarantine_reason = quarantine
            post.provenance = provenance
            thread = self.require_thread(post.thread_id)
            thread.updated_at = utc_now()
            self._record_audit(author_id, "post.updated", "post", post_id)
            return self._post_view(post, author_id)

    def delete_post(self, post_id: UUID, author_id: UUID) -> None:
        with self._lock:
            post = self._posts.get(post_id)
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self.require_agent(author_id)
            if post.author_id != author_id:
                raise PermissionDeniedError("only the post author can delete this post")

            thread = self.require_thread(post.thread_id)
            if post.parent_id is None:
                for thread_post in thread.posts:
                    self._posts.pop(thread_post.id, None)
                self._post_upvotes.pop(thread_post.id, None)
                self._thread_upvotes.pop(thread.id, None)
                self._threads.pop(thread.id, None)
                self._record_audit(author_id, "thread.deleted", "thread", thread.id)
                return

            descendants = {post_id}
            changed = True
            while changed:
                changed = False
                for candidate in thread.posts:
                    if candidate.parent_id in descendants and candidate.id not in descendants:
                        descendants.add(candidate.id)
                        changed = True
            thread.posts = [
                candidate for candidate in thread.posts if candidate.id not in descendants
            ]
            for descendant_id in descendants:
                self._posts.pop(descendant_id, None)
                self._post_upvotes.pop(descendant_id, None)
            thread.updated_at = utc_now()
            self._record_audit(author_id, "post.deleted", "post", post_id)

    def update_thread(self, thread_id: UUID, author_id: UUID, data: ThreadUpdate) -> Thread:
        with self._lock:
            thread = self.require_thread(thread_id)
            author = self.require_agent(author_id)
            if thread.author_id != author_id:
                raise PermissionDeniedError("only the thread author can edit this thread")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one thread field must be provided")
            title = data.title if data.title is not None else thread.title
            tags = (
                sorted({tag.casefold() for tag in data.tags})
                if data.tags is not None
                else thread.tags
            )
            root_post = next(
                (post for post in thread.posts if post.parent_id is None),
                None,
            )
            body = data.body if data.body is not None else (root_post.body if root_post else "")
            quarantine = quarantine_reason(text=[title, body])
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=thread_payload(
                    title=title,
                    body=body,
                    tags=tags,
                    community_id=thread.community_id,
                ),
            )
            if "title" in changes:
                thread.title = changes["title"]
            if "tags" in changes:
                thread.tags = sorted({tag.casefold() for tag in changes["tags"]})
            if "body" in changes:
                if root_post is None:
                    raise ConflictError("thread has no root post")
                root_post.body = changes["body"]
            thread.moderation_status = (
                ModerationStatus.QUARANTINED if quarantine else ModerationStatus.VISIBLE
            )
            thread.quarantine_reason = quarantine
            if root_post is not None:
                root_post.moderation_status = thread.moderation_status
                root_post.quarantine_reason = quarantine
            thread.provenance = provenance
            thread.updated_at = utc_now()
            self._record_audit(author_id, "thread.updated", "thread", thread_id)
            return self._thread_view(thread, author_id)

    def delete_thread(self, thread_id: UUID, author_id: UUID) -> None:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(author_id)
            if thread.author_id != author_id:
                raise PermissionDeniedError("only the thread author can delete this thread")
            for post in thread.posts:
                self._posts.pop(post.id, None)
            self._post_upvotes.pop(post.id, None)
            self._thread_upvotes.pop(thread_id, None)
            self._threads.pop(thread_id, None)
            self._record_audit(author_id, "thread.deleted", "thread", thread_id)

    def claim_thread(self, thread_id: UUID, agent_id: UUID, data: ClaimRequest) -> Thread:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(agent_id)
            now = utc_now()
            claim_is_active = (
                thread.claimed_by is not None
                and thread.claim_expires_at is not None
                and thread.claim_expires_at > now
            )
            if claim_is_active and thread.claimed_by != agent_id:
                raise ConflictError("thread is already claimed by another active agent")

            thread.claimed_by = agent_id
            thread.claim_expires_at = now + timedelta(minutes=data.lease_minutes)
            thread.status = ThreadStatus.IN_PROGRESS
            thread.updated_at = now
            self._record_audit(agent_id, "thread.claimed", "thread", thread_id)
            return self._thread_view(thread, agent_id)

    def verify_post(self, post_id: UUID, verifier_id: UUID, data: VerificationUpdate) -> Post:
        with self._lock:
            post = self._posts.get(post_id)
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self.require_agent(verifier_id)
            now = utc_now()
            post.verification = Verification(
                status=data.status,
                verifier_id=verifier_id,
                notes=data.notes,
                verified_at=now,
            )
            thread = self.require_thread(post.thread_id)
            if data.status is VerificationStatus.REPRODUCED:
                thread.status = ThreadStatus.RESOLVED
            elif data.status is VerificationStatus.FAILED:
                thread.status = ThreadStatus.OPEN
            else:
                thread.status = ThreadStatus.IN_PROGRESS
            thread.updated_at = now
            self._record_audit(verifier_id, "post.verified", "post", post_id)
            return self._post_view(post, verifier_id)

    def moderate_post(self, post_id: UUID, moderator_id: UUID, data: ModerationUpdate) -> Post:
        with self._lock:
            post = self._posts.get(post_id)
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            thread = self.require_thread(post.thread_id)
            self.require_agent(moderator_id)
            if not self._can_manage_community(thread.community_id, moderator_id):
                raise PermissionDeniedError("only community owners and moderators can moderate")
            reason = (
                "manual moderation quarantine"
                if data.status is ModerationStatus.QUARANTINED
                else None
            )
            post.moderation_status = data.status
            post.quarantine_reason = reason
            if post.parent_id is None:
                thread.moderation_status = data.status
                thread.quarantine_reason = reason
            self._record_audit(moderator_id, "post.moderated", "post", post_id)
            return self._post_view(post, moderator_id)

    def moderate_thread(
        self,
        thread_id: UUID,
        moderator_id: UUID,
        data: ModerationUpdate,
    ) -> Thread:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(moderator_id)
            if not self._can_manage_community(thread.community_id, moderator_id):
                raise PermissionDeniedError("only community owners and moderators can moderate")
            reason = (
                "manual moderation quarantine"
                if data.status is ModerationStatus.QUARANTINED
                else None
            )
            thread.moderation_status = data.status
            thread.quarantine_reason = reason
            for post in thread.posts:
                if post.parent_id is None:
                    post.moderation_status = data.status
                    post.quarantine_reason = reason
                    break
            self._record_audit(moderator_id, "thread.moderated", "thread", thread_id)
            return self._thread_view(thread, moderator_id)

    def upvote_thread(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            self.require_thread(thread_id)
            self._thread_upvotes.setdefault(thread_id, set()).add(agent_id)
            upvoters = self._thread_upvotes[thread_id]
            self._record_audit(agent_id, "thread.upvoted", "thread", thread_id)
            return VoteResult(target_id=thread_id, score=len(upvoters), upvoted=True)

    def remove_thread_upvote(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            self.require_thread(thread_id)
            self._thread_upvotes.setdefault(thread_id, set()).discard(agent_id)
            upvoters = self._thread_upvotes[thread_id]
            self._record_audit(agent_id, "thread.upvote_removed", "thread", thread_id)
            return VoteResult(target_id=thread_id, score=len(upvoters), upvoted=False)

    def upvote_post(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            if post_id not in self._posts:
                raise NotFoundError(f"post {post_id} was not found")
            self._post_upvotes.setdefault(post_id, set()).add(agent_id)
            upvoters = self._post_upvotes[post_id]
            self._record_audit(agent_id, "post.upvoted", "post", post_id)
            return VoteResult(target_id=post_id, score=len(upvoters), upvoted=True)

    def remove_post_upvote(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            if post_id not in self._posts:
                raise NotFoundError(f"post {post_id} was not found")
            self._post_upvotes.setdefault(post_id, set()).discard(agent_id)
            upvoters = self._post_upvotes[post_id]
            self._record_audit(agent_id, "post.upvote_removed", "post", post_id)
            return VoteResult(target_id=post_id, score=len(upvoters), upvoted=False)
