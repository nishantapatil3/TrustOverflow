from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from .models import (
    DEFAULT_COMMUNITY_ID,
    AccountType,
    Agent,
    AgentCreate,
    AgentStatus,
    ClaimRequest,
    Community,
    CommunityCreate,
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


@dataclass(frozen=True)
class Session:
    agent_id: UUID
    expires_at: datetime


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
        self._sessions: dict[str, Session] = {}
        self._communities: dict[UUID, Community] = {
            DEFAULT_COMMUNITY_ID: Community(
                id=DEFAULT_COMMUNITY_ID,
                slug="general",
                name="General",
                description="The default community for cross-cutting agent questions.",
                created_at=utc_now(),
            )
        }
        self._threads: dict[UUID, Thread] = {}
        self._posts: dict[UUID, Post] = {}
        self._thread_upvotes: dict[UUID, set[UUID]] = {}
        self._post_upvotes: dict[UUID, set[UUID]] = {}

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
        upvoters = self._thread_upvotes.get(thread.id, set())
        return thread.model_copy(
            deep=True,
            update={
                "score": len(upvoters),
                "upvoted": viewer_id in upvoters if viewer_id is not None else False,
                "posts": [self._post_view(post, viewer_id) for post in thread.posts],
            },
        )

    def register_agent(self, owner_id: UUID, data: AgentCreate) -> tuple[Agent, str]:
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
            self._sessions = {
                digest: session
                for digest, session in self._sessions.items()
                if session.agent_id != agent.id
            }
            return agent.model_copy(deep=True), token

    def login_or_create_user(self, email: str, password: str) -> tuple[Agent, bool]:
        with self._lock:
            normalized_email = normalize_email(email)
            existing_id = self._email_users.get(normalized_email)
            if existing_id is not None:
                password_digest = self._user_passwords.get(existing_id)
                if password_digest is None or not verify_password(password, password_digest):
                    raise AuthenticationError("invalid email or password")
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
            return user.model_copy(deep=True), True

    def create_session(self, agent_id: UUID) -> tuple[str, datetime, Agent]:
        with self._lock:
            agent = self.require_agent(agent_id)
            access_token = secrets.token_urlsafe(32)
            expires_at = utc_now() + timedelta(hours=24)
            self._sessions[digest_secret(access_token)] = Session(
                agent_id=agent_id,
                expires_at=expires_at,
            )
            return access_token, expires_at, agent.model_copy(deep=True)

    def authenticate_session(self, access_token: str) -> Agent:
        with self._lock:
            token_digest = digest_secret(access_token)
            for agent_id, stored_digest in self._agent_keys.items():
                if hmac.compare_digest(stored_digest, token_digest):
                    return self.require_agent(agent_id).model_copy(deep=True)

            session_digest = token_digest
            session = self._sessions.get(session_digest)
            if session is None or session.expires_at <= utc_now():
                self._sessions.pop(session_digest, None)
                raise AuthenticationError("invalid or expired access token")
            return self.require_agent(session.agent_id).model_copy(deep=True)

    def authenticate_token(self, token: str) -> Agent:
        """Authenticate an agent credential or short-lived login session."""
        return self.authenticate_session(token)

    def revoke_session(self, access_token: str) -> None:
        with self._lock:
            self._sessions.pop(digest_secret(access_token), None)

    def list_agents(self) -> list[Agent]:
        with self._lock:
            return [agent.model_copy(deep=True) for agent in self._agents.values()]

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
                created_at=utc_now(),
            )
            self._communities[community.id] = community
            return community.model_copy(deep=True)

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
            self.require_agent(author_id)
            community_id = data.community_id or DEFAULT_COMMUNITY_ID
            self.require_community(community_id)
            now = utc_now()
            thread = Thread(
                id=uuid4(),
                community_id=community_id,
                title=data.title,
                tags=sorted({tag.casefold() for tag in data.tags}),
                author_id=author_id,
                created_at=now,
                updated_at=now,
            )
            root_post = Post(
                id=uuid4(),
                thread_id=thread.id,
                author_id=author_id,
                type=PostType.QUESTION,
                body=data.body,
                created_at=now,
            )
            thread.posts.append(root_post)
            self._threads[thread.id] = thread
            self._posts[root_post.id] = root_post
            return self._thread_view(thread)

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
        community_id: UUID | None = None,
        viewer_id: UUID | None = None,
    ) -> list[Thread]:
        with self._lock:
            normalized_query = query.casefold() if query else None
            normalized_tag = tag.casefold() if tag else None
            threads = []
            for thread in self._threads.values():
                if status is not None and thread.status is not status:
                    continue
                if community_id is not None and thread.community_id != community_id:
                    continue
                if normalized_tag is not None and normalized_tag not in thread.tags:
                    continue
                if normalized_query is not None:
                    searchable = " ".join(
                        [thread.title, *thread.tags, *(post.body for post in thread.posts)]
                    ).casefold()
                    if normalized_query not in searchable:
                        continue
                threads.append(self._thread_view(thread, viewer_id))
            return sorted(threads, key=lambda item: item.updated_at, reverse=True)

    def require_thread(self, thread_id: UUID) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise NotFoundError(f"thread {thread_id} was not found")
        return thread

    def get_thread(self, thread_id: UUID, viewer_id: UUID | None = None) -> Thread:
        with self._lock:
            return self._thread_view(self.require_thread(thread_id), viewer_id)

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(author_id)
            if thread.status in {ThreadStatus.RESOLVED, ThreadStatus.CLOSED}:
                raise ConflictError("cannot add a post to a resolved or closed thread")
            if data.parent_id is not None:
                parent = self._posts.get(data.parent_id)
                if parent is None or parent.thread_id != thread_id:
                    raise NotFoundError(
                        f"parent post {data.parent_id} was not found in this thread"
                    )

            post = Post(
                id=uuid4(),
                thread_id=thread_id,
                author_id=author_id,
                created_at=utc_now(),
                **data.model_dump(),
            )
            thread.posts.append(post)
            thread.status = ThreadStatus.IN_PROGRESS
            thread.updated_at = post.created_at
            self._posts[post.id] = post
            return self._post_view(post, author_id)

    def update_post(self, post_id: UUID, author_id: UUID, data: PostUpdate) -> Post:
        with self._lock:
            post = self._posts.get(post_id)
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self.require_agent(author_id)
            if post.author_id != author_id:
                raise PermissionDeniedError("only the post author can edit this post")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one post field must be provided")
            for field, value in changes.items():
                setattr(post, field, value)
            thread = self.require_thread(post.thread_id)
            thread.updated_at = utc_now()
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

    def update_thread(self, thread_id: UUID, author_id: UUID, data: ThreadUpdate) -> Thread:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(author_id)
            if thread.author_id != author_id:
                raise PermissionDeniedError("only the thread author can edit this thread")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one thread field must be provided")
            if "title" in changes:
                thread.title = changes["title"]
            if "tags" in changes:
                thread.tags = sorted({tag.casefold() for tag in changes["tags"]})
            if "body" in changes:
                root_post = next(
                    (post for post in thread.posts if post.parent_id is None),
                    None,
                )
                if root_post is None:
                    raise ConflictError("thread has no root post")
                root_post.body = changes["body"]
            thread.updated_at = utc_now()
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
            return self._post_view(post, verifier_id)

    def upvote_thread(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            self.require_thread(thread_id)
            self._thread_upvotes.setdefault(thread_id, set()).add(agent_id)
            upvoters = self._thread_upvotes[thread_id]
            return VoteResult(target_id=thread_id, score=len(upvoters), upvoted=True)

    def remove_thread_upvote(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            self.require_thread(thread_id)
            self._thread_upvotes.setdefault(thread_id, set()).discard(agent_id)
            upvoters = self._thread_upvotes[thread_id]
            return VoteResult(target_id=thread_id, score=len(upvoters), upvoted=False)

    def upvote_post(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            if post_id not in self._posts:
                raise NotFoundError(f"post {post_id} was not found")
            self._post_upvotes.setdefault(post_id, set()).add(agent_id)
            upvoters = self._post_upvotes[post_id]
            return VoteResult(target_id=post_id, score=len(upvoters), upvoted=True)

    def remove_post_upvote(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._lock:
            self.require_agent(agent_id)
            if post_id not in self._posts:
                raise NotFoundError(f"post {post_id} was not found")
            self._post_upvotes.setdefault(post_id, set()).discard(agent_id)
            upvoters = self._post_upvotes[post_id]
            return VoteResult(target_id=post_id, score=len(upvoters), upvoted=False)
