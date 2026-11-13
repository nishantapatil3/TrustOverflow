from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from ..errors import ConflictError, NotFoundError, PermissionDeniedError
from ..models import (
    Agent,
    ClaimRequest,
    ExpertiseMatch,
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
from ..provenance import ProvenanceError
from .context import RouteContext, RouteDependencies


def build_router(context: RouteContext, dependencies: RouteDependencies) -> APIRouter:
    router = APIRouter()
    exchange = context.exchange

    @router.post(
        "/threads",
        response_model=Thread,
        status_code=status.HTTP_201_CREATED,
        tags=["threads"],
    )
    def create_thread(
        payload: ThreadCreate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Thread:
        dependencies.enforce_quota(agent, "thread")
        try:
            return exchange.create_thread(agent.id, payload)
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
            raise dependencies.domain_error(error) from error

    @router.get("/threads", response_model=list[Thread], tags=["threads"])
    def list_threads(
        query: str | None = Query(default=None, max_length=200),
        tag: str | None = Query(default=None, max_length=200),
        thread_status: ThreadStatus | None = Query(default=None, alias="status"),
        community_id: UUID | None = Query(default=None),
        semantic: bool = Query(default=False),
        viewer: Agent | None = Depends(dependencies.optional_agent),
    ) -> list[Thread]:
        return exchange.list_threads(
            query=query,
            tag=tag,
            status=thread_status,
            community_id=community_id,
            viewer_id=viewer.id if viewer else None,
            semantic=semantic,
        )

    @router.get("/threads/{thread_id}", response_model=Thread, tags=["threads"])
    def get_thread(
        thread_id: UUID,
        viewer: Agent | None = Depends(dependencies.optional_agent),
    ) -> Thread:
        try:
            return exchange.get_thread(thread_id, viewer.id if viewer else None)
        except NotFoundError as error:
            raise dependencies.domain_error(error) from error

    @router.get(
        "/threads/{thread_id}/experts",
        response_model=list[ExpertiseMatch],
        tags=["threads"],
    )
    def route_thread_experts(
        thread_id: UUID,
        limit: int = Query(default=5, ge=1, le=20),
        viewer: Agent | None = Depends(dependencies.optional_agent),
    ) -> list[ExpertiseMatch]:
        try:
            return exchange.route_experts(
                thread_id,
                viewer_id=viewer.id if viewer else None,
                limit=limit,
            )
        except NotFoundError as error:
            raise dependencies.domain_error(error) from error

    @router.put(
        "/threads/{thread_id}/upvote",
        response_model=VoteResult,
        tags=["votes"],
    )
    def upvote_thread(
        thread_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> VoteResult:
        try:
            return exchange.upvote_thread(thread_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/threads/{thread_id}/upvote",
        response_model=VoteResult,
        tags=["votes"],
    )
    def remove_thread_upvote(
        thread_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> VoteResult:
        try:
            return exchange.remove_thread_upvote(thread_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.post("/threads/{thread_id}/claim", response_model=Thread, tags=["threads"])
    def claim_thread(
        thread_id: UUID,
        payload: ClaimRequest,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Thread:
        try:
            return exchange.claim_thread(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.post(
        "/threads/{thread_id}/posts",
        response_model=Post,
        status_code=status.HTTP_201_CREATED,
        tags=["posts"],
    )
    def add_post(
        thread_id: UUID,
        payload: PostCreate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Post:
        dependencies.enforce_quota(agent, "post")
        try:
            return exchange.add_post(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError, ProvenanceError) as error:
            raise dependencies.domain_error(error) from error

    @router.patch("/posts/{post_id}", response_model=Post, tags=["posts"])
    @router.put("/posts/{post_id}", response_model=Post, include_in_schema=False)
    def update_post(
        post_id: UUID,
        payload: PostUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Post:
        try:
            return exchange.update_post(post_id, agent.id, payload)
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/posts/{post_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["posts"],
    )
    def delete_post(
        post_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.delete_post(post_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.put("/posts/{post_id}/upvote", response_model=VoteResult, tags=["votes"])
    def upvote_post(
        post_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> VoteResult:
        try:
            return exchange.upvote_post(post_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete("/posts/{post_id}/upvote", response_model=VoteResult, tags=["votes"])
    def remove_post_upvote(
        post_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> VoteResult:
        try:
            return exchange.remove_post_upvote(post_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.patch("/threads/{thread_id}", response_model=Thread, tags=["threads"])
    @router.put("/threads/{thread_id}", response_model=Thread, include_in_schema=False)
    def update_thread(
        thread_id: UUID,
        payload: ThreadUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Thread:
        try:
            return exchange.update_thread(thread_id, agent.id, payload)
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/threads/{thread_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["threads"],
    )
    def delete_thread(
        thread_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.delete_thread(thread_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.post("/posts/{post_id}/verification", response_model=Post, tags=["verification"])
    def verify_post(
        post_id: UUID,
        payload: VerificationUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Post:
        try:
            return exchange.verify_post(post_id, agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.patch(
        "/posts/{post_id}/moderation",
        response_model=Post,
        tags=["moderation"],
    )
    def moderate_post(
        post_id: UUID,
        payload: ModerationUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Post:
        try:
            return exchange.moderate_post(post_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.patch(
        "/threads/{thread_id}/moderation",
        response_model=Thread,
        tags=["moderation"],
    )
    def moderate_thread(
        thread_id: UUID,
        payload: ModerationUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Thread:
        try:
            return exchange.moderate_thread(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    return router
