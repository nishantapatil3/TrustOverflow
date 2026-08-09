from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid4

from .models import (
    Agent,
    AgentCreate,
    AgentStatus,
    ClaimRequest,
    Post,
    PostCreate,
    PostType,
    Thread,
    ThreadCreate,
    ThreadStatus,
    Verification,
    VerificationStatus,
    VerificationUpdate,
)


class StoreError(Exception):
    """Base class for domain errors raised by the exchange store."""


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class AuthenticationError(StoreError):
    pass


@dataclass(frozen=True)
class Session:
    agent_id: UUID
    expires_at: datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ExchangeStore:
    """Thread-safe in-memory store used by the MVP and its tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._agents: dict[UUID, Agent] = {}
        self._agent_keys: dict[UUID, str] = {}
        self._sessions: dict[str, Session] = {}
        self._threads: dict[UUID, Thread] = {}
        self._posts: dict[UUID, Post] = {}

    def register_agent(self, data: AgentCreate) -> tuple[Agent, str]:
        with self._lock:
            duplicate_name = any(
                agent.name.casefold() == data.name.casefold() for agent in self._agents.values()
            )
            if duplicate_name:
                raise ConflictError("an agent with this name already exists")

            agent_key = secrets.token_urlsafe(32)
            agent = Agent(id=uuid4(), created_at=utc_now(), **data.model_dump())
            self._agents[agent.id] = agent
            self._agent_keys[agent.id] = digest_secret(agent_key)
            return agent.model_copy(deep=True), agent_key

    def login(self, agent_id: UUID, agent_key: str) -> tuple[str, datetime, Agent]:
        with self._lock:
            agent = self.require_agent(agent_id)
            stored_digest = self._agent_keys.get(agent_id)
            key_digest = digest_secret(agent_key)
            if stored_digest is None or not hmac.compare_digest(stored_digest, key_digest):
                raise AuthenticationError("invalid agent credentials")

            access_token = secrets.token_urlsafe(32)
            expires_at = utc_now() + timedelta(hours=24)
            self._sessions[digest_secret(access_token)] = Session(
                agent_id=agent_id,
                expires_at=expires_at,
            )
            return access_token, expires_at, agent.model_copy(deep=True)

    def authenticate_session(self, access_token: str) -> Agent:
        with self._lock:
            session_digest = digest_secret(access_token)
            session = self._sessions.get(session_digest)
            if session is None or session.expires_at <= utc_now():
                self._sessions.pop(session_digest, None)
                raise AuthenticationError("invalid or expired access token")
            return self.require_agent(session.agent_id).model_copy(deep=True)

    def revoke_session(self, access_token: str) -> None:
        with self._lock:
            self._sessions.pop(digest_secret(access_token), None)

    def list_agents(self) -> list[Agent]:
        with self._lock:
            return [agent.model_copy(deep=True) for agent in self._agents.values()]

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
            now = utc_now()
            thread = Thread(
                id=uuid4(),
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
            return thread.model_copy(deep=True)

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
    ) -> list[Thread]:
        with self._lock:
            normalized_query = query.casefold() if query else None
            normalized_tag = tag.casefold() if tag else None
            threads = []
            for thread in self._threads.values():
                if status is not None and thread.status is not status:
                    continue
                if normalized_tag is not None and normalized_tag not in thread.tags:
                    continue
                if normalized_query is not None:
                    searchable = " ".join(
                        [thread.title, *thread.tags, *(post.body for post in thread.posts)]
                    ).casefold()
                    if normalized_query not in searchable:
                        continue
                threads.append(thread.model_copy(deep=True))
            return sorted(threads, key=lambda item: item.updated_at, reverse=True)

    def require_thread(self, thread_id: UUID) -> Thread:
        thread = self._threads.get(thread_id)
        if thread is None:
            raise NotFoundError(f"thread {thread_id} was not found")
        return thread

    def get_thread(self, thread_id: UUID) -> Thread:
        with self._lock:
            return self.require_thread(thread_id).model_copy(deep=True)

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post:
        with self._lock:
            thread = self.require_thread(thread_id)
            self.require_agent(author_id)
            if thread.status in {ThreadStatus.RESOLVED, ThreadStatus.CLOSED}:
                raise ConflictError("cannot add a post to a resolved or closed thread")

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
            return post.model_copy(deep=True)

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
            return thread.model_copy(deep=True)

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
            return post.model_copy(deep=True)
