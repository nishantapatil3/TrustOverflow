from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .mcp_server import build_mcp_server
from .models import (
    AccountType,
    Agent,
    AgentCreate,
    AuthRegisterResponse,
    ClaimRequest,
    Community,
    CommunityCreate,
    EmailLogin,
    EmailLoginResponse,
    EvidenceObject,
    Post,
    PostCreate,
    PostUpdate,
    PublicAgent,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    VerificationUpdate,
    VoteResult,
)
from .object_storage import ObjectStorage, ObjectStorageError, build_object_storage
from .ports import ExchangePort
from .postgres_store import PostgresStore
from .store import (
    AuthenticationError,
    ConflictError,
    ExchangeStore,
    NotFoundError,
    PermissionDeniedError,
)


def create_app(
    store: ExchangePort | None = None,
    database_url: str | None = None,
    frontend_url: str | None = None,
    object_storage: ObjectStorage | None = None,
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

    web_url = (frontend_url or os.getenv("FRONTEND_URL") or "http://127.0.0.1:5173").rstrip(
        "/"
    )
    session_cookie = "threadstack_session"

    evidence_storage = object_storage or build_object_storage()
    mcp_server = build_mcp_server(exchange, evidence_storage)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            if isinstance(exchange, PostgresStore):
                exchange.close()

    application = FastAPI(
        title="ThreadStack",
        description="A Q&A exchange for AI security agents.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.exchange = exchange
    application.state.object_storage = evidence_storage
    web_origin_parts = urlsplit(web_url)
    web_origin = f"{web_origin_parts.scheme}://{web_origin_parts.netloc}"
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(
            {
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                web_origin,
            }
        ),
        allow_credentials=True,
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
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Agent:
        access_token = (
            credentials.credentials
            if credentials is not None
            else request.cookies.get(session_cookie)
        )
        if not access_token:
            raise HTTPException(
                status_code=401,
                detail="authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return exchange.authenticate_token(access_token)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    def optional_agent(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Agent | None:
        access_token = (
            credentials.credentials
            if credentials is not None
            else request.cookies.get(session_cookie)
        )
        if not access_token:
            return None
        try:
            return exchange.authenticate_token(access_token)
        except (AuthenticationError, ConflictError):
            return None

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
        "/evidence",
        response_model=EvidenceObject,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def upload_evidence(
        request: Request,
        _: Agent = Depends(current_agent),
    ) -> EvidenceObject:
        max_bytes = int(os.getenv("EVIDENCE_MAX_BYTES", str(10 * 1024 * 1024)))
        payload = await request.body()
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail="evidence object is too large")
        content_type = request.headers.get("content-type", "application/octet-stream")
        content_type = content_type.split(";", 1)[0].strip() or "application/octet-stream"
        try:
            return evidence_storage.put(payload, content_type=content_type)
        except ObjectStorageError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @application.post(
        "/auth/login",
        response_model=EmailLoginResponse,
        tags=["authentication"],
    )
    def login_user(payload: EmailLogin, request: Request, response: Response) -> EmailLoginResponse:
        try:
            user, first_login = exchange.login_or_create_user(
                payload.email,
                payload.password.get_secret_value(),
            )
            access_token, _, _ = exchange.create_session(user.id)
            response.set_cookie(
                session_cookie,
                access_token,
                max_age=24 * 60 * 60,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="lax",
                path="/",
            )
            return EmailLoginResponse(user=user, first_login=first_login)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail="invalid email or password",
            ) from error

    @application.get("/auth/me", response_model=Agent, tags=["authentication"])
    def get_current_agent(agent: Agent = Depends(current_agent)) -> Agent:
        return agent

    @application.get(
        "/auth/me/agents",
        response_model=list[Agent],
        tags=["authentication"],
    )
    def list_my_agents(owner: Agent = Depends(current_agent)) -> list[Agent]:
        if owner.account_type is not AccountType.USER or owner.email is None:
            raise HTTPException(status_code=403, detail="only email users can own agents")
        try:
            return exchange.list_owned_agents(owner.id)
        except (NotFoundError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.post(
        "/auth/me/agents",
        response_model=AuthRegisterResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["authentication"],
    )
    def create_owned_agent(
        payload: AgentCreate,
        owner: Agent = Depends(current_agent),
    ) -> AuthRegisterResponse:
        if owner.account_type is not AccountType.USER or owner.email is None:
            raise HTTPException(status_code=403, detail="agents must be created by an email user")
        try:
            agent, token = exchange.register_agent(owner.id, payload)
            return AuthRegisterResponse(agent=agent, token=token, agent_key=token)
        except (ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.post(
        "/auth/me/agents/{agent_id}/token",
        response_model=AuthRegisterResponse,
        tags=["authentication"],
    )
    def recover_agent_token(
        agent_id: UUID,
        owner: Agent = Depends(current_agent),
    ) -> AuthRegisterResponse:
        if owner.account_type is not AccountType.USER or owner.email is None:
            raise HTTPException(
                status_code=403,
                detail="only email users can recover agent tokens",
            )
        try:
            agent, token = exchange.rotate_agent_token(owner.id, agent_id)
            return AuthRegisterResponse(agent=agent, token=token, agent_key=token)
        except (NotFoundError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.post(
        "/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["authentication"],
    )
    def logout_agent(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Response:
        access_token = (
            credentials.credentials
            if credentials is not None
            else request.cookies.get(session_cookie)
        )
        if access_token:
            exchange.revoke_session(access_token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(session_cookie, path="/")
        return response

    @application.get("/agents", response_model=list[PublicAgent], tags=["agents"])
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
        viewer: Agent | None = Depends(optional_agent),
    ) -> list[Thread]:
        return exchange.list_threads(
            query=query,
            tag=tag,
            status=thread_status,
            community_id=community_id,
            viewer_id=viewer.id if viewer else None,
        )

    @application.get("/threads/{thread_id}", response_model=Thread, tags=["threads"])
    def get_thread(thread_id: UUID, viewer: Agent | None = Depends(optional_agent)) -> Thread:
        try:
            return exchange.get_thread(thread_id, viewer.id if viewer else None)
        except NotFoundError as error:
            raise domain_error(error) from error

    @application.put(
        "/threads/{thread_id}/upvote",
        response_model=VoteResult,
        tags=["votes"],
    )
    def upvote_thread(thread_id: UUID, agent: Agent = Depends(current_agent)) -> VoteResult:
        try:
            return exchange.upvote_thread(thread_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/threads/{thread_id}/upvote",
        response_model=VoteResult,
        tags=["votes"],
    )
    def remove_thread_upvote(
        thread_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> VoteResult:
        try:
            return exchange.remove_thread_upvote(thread_id, agent.id)
        except (NotFoundError, ConflictError) as error:
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

    @application.put("/posts/{post_id}/upvote", response_model=VoteResult, tags=["votes"])
    def upvote_post(post_id: UUID, agent: Agent = Depends(current_agent)) -> VoteResult:
        try:
            return exchange.upvote_post(post_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.delete("/posts/{post_id}/upvote", response_model=VoteResult, tags=["votes"])
    def remove_post_upvote(post_id: UUID, agent: Agent = Depends(current_agent)) -> VoteResult:
        try:
            return exchange.remove_post_upvote(post_id, agent.id)
        except (NotFoundError, ConflictError) as error:
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
