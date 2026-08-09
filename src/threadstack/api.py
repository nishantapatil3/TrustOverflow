from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .mcp_server import build_mcp_server
from .models import (
    Agent,
    AgentCreate,
    AgentLogin,
    AuthLoginResponse,
    AuthRegisterResponse,
    ClaimRequest,
    Community,
    CommunityCreate,
    Post,
    PostCreate,
    PostUpdate,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    VerificationUpdate,
)
from .postgres_store import PostgresStore
from .store import (
    AuthenticationError,
    ConflictError,
    ExchangeStore,
    NotFoundError,
    PermissionDeniedError,
)


def create_app(
    store: ExchangeStore | PostgresStore | None = None,
    database_url: str | None = None,
) -> FastAPI:
    if store is None:
        configured_database_url = database_url or os.getenv("DATABASE_URL")
        exchange = (
            PostgresStore(configured_database_url)
            if configured_database_url
            else ExchangeStore()
        )
    else:
        exchange = store

    mcp_server = build_mcp_server(exchange)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    application = FastAPI(
        title="ThreadStack",
        description="A Q&A exchange for AI security agents.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        expose_headers=["mcp-session-id"],
        allow_headers=[
            "authorization",
            "content-type",
            "mcp-protocol-version",
            "mcp-session-id",
            "last-event-id",
        ],
    )
    bearer = HTTPBearer(auto_error=False)

    def current_agent(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Agent:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="bearer authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return exchange.authenticate_token(credentials.credentials)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    def domain_error(error: Exception) -> HTTPException:
        if isinstance(error, NotFoundError):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, PermissionDeniedError):
            return HTTPException(status_code=403, detail=str(error))
        return HTTPException(status_code=409, detail=str(error))

    @application.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/auth/register",
        response_model=AuthRegisterResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["authentication"],
    )
    def register_agent(payload: AgentCreate) -> AuthRegisterResponse:
        try:
            agent, token = exchange.register_agent(payload)
            return AuthRegisterResponse(agent=agent, token=token, agent_key=token)
        except ConflictError as error:
            raise domain_error(error) from error

    @application.post("/auth/login", response_model=AuthLoginResponse, tags=["authentication"])
    def login_agent(payload: AgentLogin) -> AuthLoginResponse:
        try:
            access_token, expires_at, agent = exchange.login(
                payload.agent_id,
                payload.agent_key.get_secret_value(),
            )
            return AuthLoginResponse(
                access_token=access_token,
                expires_at=expires_at,
                agent=agent,
            )
        except (AuthenticationError, ConflictError, NotFoundError) as error:
            raise HTTPException(
                status_code=401,
                detail="invalid agent credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    @application.get("/auth/me", response_model=Agent, tags=["authentication"])
    def get_current_agent(agent: Agent = Depends(current_agent)) -> Agent:
        return agent

    @application.post(
        "/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["authentication"],
    )
    def logout_agent(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if credentials is not None:
            exchange.revoke_session(credentials.credentials)

    @application.get("/agents", response_model=list[Agent], tags=["agents"])
    def list_agents() -> list[Agent]:
        return exchange.list_agents()

    @application.post(
        "/communities",
        response_model=Community,
        status_code=status.HTTP_201_CREATED,
        tags=["communities"],
    )
    def create_community(
        payload: CommunityCreate,
        agent: Agent = Depends(current_agent),
    ) -> Community:
        try:
            return exchange.create_community(agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.get("/communities", response_model=list[Community], tags=["communities"])
    def list_communities() -> list[Community]:
        return exchange.list_communities()

    @application.get(
        "/communities/{community_id}",
        response_model=Community,
        tags=["communities"],
    )
    def get_community(community_id: UUID) -> Community:
        try:
            return exchange.get_community(community_id)
        except NotFoundError as error:
            raise domain_error(error) from error

    @application.post(
        "/threads",
        response_model=Thread,
        status_code=status.HTTP_201_CREATED,
        tags=["threads"],
    )
    def create_thread(payload: ThreadCreate, agent: Agent = Depends(current_agent)) -> Thread:
        try:
            return exchange.create_thread(agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.get("/threads", response_model=list[Thread], tags=["threads"])
    def list_threads(
        query: str | None = Query(default=None, max_length=200),
        tag: str | None = Query(default=None, max_length=200),
        thread_status: ThreadStatus | None = Query(default=None, alias="status"),
        community_id: UUID | None = Query(default=None),
    ) -> list[Thread]:
        return exchange.list_threads(
            query=query,
            tag=tag,
            status=thread_status,
            community_id=community_id,
        )

    @application.get("/threads/{thread_id}", response_model=Thread, tags=["threads"])
    def get_thread(thread_id: UUID) -> Thread:
        try:
            return exchange.get_thread(thread_id)
        except NotFoundError as error:
            raise domain_error(error) from error

    @application.post("/threads/{thread_id}/claim", response_model=Thread, tags=["threads"])
    def claim_thread(
        thread_id: UUID,
        payload: ClaimRequest,
        agent: Agent = Depends(current_agent),
    ) -> Thread:
        try:
            return exchange.claim_thread(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.post(
        "/threads/{thread_id}/posts",
        response_model=Post,
        status_code=status.HTTP_201_CREATED,
        tags=["posts"],
    )
    def add_post(
        thread_id: UUID,
        payload: PostCreate,
        agent: Agent = Depends(current_agent),
    ) -> Post:
        try:
            return exchange.add_post(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.patch("/posts/{post_id}", response_model=Post, tags=["posts"])
    @application.put("/posts/{post_id}", response_model=Post, include_in_schema=False)
    def update_post(
        post_id: UUID,
        payload: PostUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Post:
        try:
            return exchange.update_post(post_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/posts/{post_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["posts"],
    )
    def delete_post(post_id: UUID, agent: Agent = Depends(current_agent)) -> None:
        try:
            exchange.delete_post(post_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.patch("/threads/{thread_id}", response_model=Thread, tags=["threads"])
    @application.put("/threads/{thread_id}", response_model=Thread, include_in_schema=False)
    def update_thread(
        thread_id: UUID,
        payload: ThreadUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Thread:
        try:
            return exchange.update_thread(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/threads/{thread_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["threads"],
    )
    def delete_thread(thread_id: UUID, agent: Agent = Depends(current_agent)) -> None:
        try:
            exchange.delete_thread(thread_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.post("/posts/{post_id}/verification", response_model=Post, tags=["verification"])
    def verify_post(
        post_id: UUID,
        payload: VerificationUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Post:
        try:
            return exchange.verify_post(post_id, agent.id, payload)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    # Mount after the HTTP routes so FastMCP can keep its conventional /mcp path.
    application.mount("/", mcp_server.streamable_http_app())
    return application


app = create_app()
