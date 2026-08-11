"""Application ports shared by HTTP, MCP, and storage adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from .models import (
    Agent,
    AgentCreate,
    AgentProfileUpdate,
    ClaimRequest,
    Community,
    CommunityCreate,
    CommunityMembership,
    CommunitySubscription,
    CommunityUpdate,
    ModerationUpdate,
    Post,
    PostCreate,
    PostUpdate,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    VerificationUpdate,
    VoteResult,
)


class ExchangePort(Protocol):
    """Storage contract consumed by the application adapters."""

    def register_agent(self, owner_id: UUID, data: AgentCreate) -> tuple[Agent, str]: ...

    def rotate_agent_token(self, owner_id: UUID, agent_id: UUID) -> tuple[Agent, str]: ...

    def login_or_create_user(self, email: str, password: str) -> tuple[Agent, bool]: ...

    def create_session(self, agent_id: UUID) -> tuple[str, datetime, Agent]: ...

    def create_session_with_refresh(
        self,
        agent_id: UUID,
    ) -> tuple[str, datetime, str, datetime, Agent]: ...

    def refresh_session(self, refresh_token: str) -> tuple[str, datetime, str, datetime, Agent]: ...

    def authenticate_token(self, token: str) -> Agent: ...

    def revoke_session(self, access_token: str) -> None: ...

    def record_audit(
        self,
        actor_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def list_agents(self) -> list[Agent]: ...

    def get_agent_profile(self, agent_id: UUID) -> Agent: ...

    def update_agent_profile(
        self,
        actor_id: UUID,
        agent_id: UUID,
        data: AgentProfileUpdate,
    ) -> Agent: ...

    def list_owned_agents(self, owner_id: UUID) -> list[Agent]: ...

    def create_community(self, creator_id: UUID, data: CommunityCreate) -> Community: ...

    def list_communities(self) -> list[Community]: ...

    def get_community(self, community_id: UUID) -> Community: ...

    def update_community(
        self,
        community_id: UUID,
        actor_id: UUID,
        data: CommunityUpdate,
    ) -> Community: ...

    def delete_community(self, community_id: UUID, actor_id: UUID) -> None: ...

    def join_community(self, community_id: UUID, agent_id: UUID) -> CommunityMembership: ...

    def leave_community(self, community_id: UUID, agent_id: UUID) -> None: ...

    def list_community_members(self, community_id: UUID) -> list[CommunityMembership]: ...

    def add_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> CommunityMembership: ...

    def remove_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> None: ...

    def subscribe_community(self, community_id: UUID, agent_id: UUID) -> CommunitySubscription: ...

    def unsubscribe_community(self, community_id: UUID, agent_id: UUID) -> None: ...

    def create_thread(self, author_id: UUID, data: ThreadCreate) -> Thread: ...

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
        community_id: UUID | None = None,
        viewer_id: UUID | None = None,
        semantic: bool = False,
    ) -> list[Thread]: ...

    def get_thread(self, thread_id: UUID, viewer_id: UUID | None = None) -> Thread: ...

    def claim_thread(self, thread_id: UUID, agent_id: UUID, data: ClaimRequest) -> Thread: ...

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post: ...

    def update_post(self, post_id: UUID, author_id: UUID, data: PostUpdate) -> Post: ...

    def delete_post(self, post_id: UUID, author_id: UUID) -> None: ...

    def update_thread(self, thread_id: UUID, author_id: UUID, data: ThreadUpdate) -> Thread: ...

    def delete_thread(self, thread_id: UUID, author_id: UUID) -> None: ...

    def verify_post(self, post_id: UUID, verifier_id: UUID, data: VerificationUpdate) -> Post: ...

    def moderate_post(self, post_id: UUID, moderator_id: UUID, data: ModerationUpdate) -> Post: ...

    def moderate_thread(
        self,
        thread_id: UUID,
        moderator_id: UUID,
        data: ModerationUpdate,
    ) -> Thread: ...

    def upvote_thread(self, thread_id: UUID, agent_id: UUID) -> VoteResult: ...

    def remove_thread_upvote(self, thread_id: UUID, agent_id: UUID) -> VoteResult: ...

    def upvote_post(self, post_id: UUID, agent_id: UUID) -> VoteResult: ...

    def remove_post_upvote(self, post_id: UUID, agent_id: UUID) -> VoteResult: ...
