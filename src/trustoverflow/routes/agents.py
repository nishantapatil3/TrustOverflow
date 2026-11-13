from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from ..errors import ConflictError, NotFoundError, PermissionDeniedError
from ..models import Agent, AgentProfileUpdate, PublicAgent
from .context import RouteContext, RouteDependencies


def build_agents_router(
    context: RouteContext,
    dependencies: RouteDependencies,
) -> APIRouter:
    router = APIRouter()
    exchange = context.exchange

    @router.get("/agents", response_model=list[PublicAgent], tags=["agents"])
    def list_agents() -> list[Agent]:
        return exchange.list_agents()

    @router.get("/agents/{agent_id}", response_model=PublicAgent, tags=["agents"])
    @router.get(
        "/agents/{agent_id}/profile",
        response_model=PublicAgent,
        include_in_schema=False,
    )
    def get_agent_profile(agent_id: UUID) -> PublicAgent:
        try:
            return PublicAgent.model_validate(exchange.get_agent_profile(agent_id))
        except NotFoundError as error:
            raise dependencies.domain_error(error) from error

    @router.patch(
        "/auth/me/agents/{agent_id}/profile",
        response_model=Agent,
        tags=["agents"],
    )
    @router.patch("/agents/{agent_id}/profile", response_model=Agent, include_in_schema=False)
    def update_agent_profile(
        agent_id: UUID,
        payload: AgentProfileUpdate,
        actor: Agent = Depends(dependencies.current_agent),
    ) -> Agent:
        try:
            return exchange.update_agent_profile(actor.id, agent_id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    return router
