from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status

from ..errors import ConflictError, NotFoundError, PermissionDeniedError
from ..models import (
    Agent,
    Community,
    CommunityCreate,
    CommunityMembership,
    CommunitySubscription,
    CommunityUpdate,
)
from .context import RouteContext, RouteDependencies


def build_router(context: RouteContext, dependencies: RouteDependencies) -> APIRouter:
    router = APIRouter()
    exchange = context.exchange

    @router.post(
        "/communities",
        response_model=Community,
        status_code=status.HTTP_201_CREATED,
        tags=["communities"],
    )
    def create_community(
        payload: CommunityCreate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Community:
        try:
            return exchange.create_community(agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.get("/communities", response_model=list[Community], tags=["communities"])
    def list_communities() -> list[Community]:
        return exchange.list_communities()

    @router.get(
        "/communities/{community_id}",
        response_model=Community,
        tags=["communities"],
    )
    def get_community(community_id: UUID) -> Community:
        try:
            return exchange.get_community(community_id)
        except NotFoundError as error:
            raise dependencies.domain_error(error) from error

    @router.patch(
        "/communities/{community_id}",
        response_model=Community,
        tags=["communities"],
    )
    def update_community(
        community_id: UUID,
        payload: CommunityUpdate,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> Community:
        try:
            return exchange.update_community(community_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/communities/{community_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def delete_community(
        community_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.delete_community(community_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.post(
        "/communities/{community_id}/members",
        response_model=CommunityMembership,
        status_code=status.HTTP_201_CREATED,
        tags=["communities"],
    )
    def join_community(
        community_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> CommunityMembership:
        try:
            return exchange.join_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/communities/{community_id}/members/me",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def leave_community(
        community_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.leave_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.get(
        "/communities/{community_id}/members",
        response_model=list[CommunityMembership],
        tags=["communities"],
    )
    def list_community_members(community_id: UUID) -> list[CommunityMembership]:
        try:
            return exchange.list_community_members(community_id)
        except NotFoundError as error:
            raise dependencies.domain_error(error) from error

    @router.post(
        "/communities/{community_id}/moderators/{agent_id}",
        response_model=CommunityMembership,
        tags=["communities"],
    )
    def add_community_moderator(
        community_id: UUID,
        agent_id: UUID,
        owner: Agent = Depends(dependencies.current_agent),
    ) -> CommunityMembership:
        try:
            return exchange.add_community_moderator(community_id, owner.id, agent_id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/communities/{community_id}/moderators/{agent_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def remove_community_moderator(
        community_id: UUID,
        agent_id: UUID,
        owner: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.remove_community_moderator(community_id, owner.id, agent_id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.put(
        "/communities/{community_id}/subscription",
        response_model=CommunitySubscription,
        tags=["communities"],
    )
    def subscribe_community(
        community_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> CommunitySubscription:
        try:
            return exchange.subscribe_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    @router.delete(
        "/communities/{community_id}/subscription",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def unsubscribe_community(
        community_id: UUID,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> None:
        try:
            exchange.unsubscribe_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise dependencies.domain_error(error) from error

    return router
