from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .limits import QuotaSet, RateLimitExceeded, request_limiter_from_environment
from .mcp_server import build_mcp_server
from .models import (
    AccountType,
    Agent,
    AgentCreate,
    AuthRegisterResponse,
    ClaimRequest,
    Community,
    CommunityCreate,
    CommunityMembership,
    CommunitySubscription,
    CommunityUpdate,
    EmailLogin,
    EmailLoginResponse,
    EvidenceObject,
    ModerationUpdate,
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
from .provenance import ProvenanceError
from .store import (
    ACCESS_SESSION_TTL,
    REFRESH_SESSION_TTL,
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
    refresh_cookie = "threadstack_refresh"

    evidence_storage = object_storage or build_object_storage()
    request_limiter = request_limiter_from_environment()
    quotas = QuotaSet.from_environment()
    mcp_server = build_mcp_server(exchange, evidence_storage, quotas)

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

    @application.middleware("http")
    async def enforce_request_rate_limit(request: Request, call_next):
        if request.url.path not in {"/healthz", "/docs", "/redoc", "/openapi.json"}:
            key = request.client.host if request.client is not None else "unknown"
            try:
                request_limiter.check(key)
            except RateLimitExceeded as error:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "request rate limit exceeded"},
                    headers={"Retry-After": str(error.retry_after)},
                )
        return await call_next(request)
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
        if isinstance(error, ProvenanceError):
            return HTTPException(status_code=422, detail=str(error))
        if isinstance(error, NotFoundError):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, PermissionDeniedError):
            return HTTPException(status_code=403, detail=str(error))
        return HTTPException(status_code=409, detail=str(error))

    def enforce_quota(agent: Agent, kind: str) -> None:
        try:
            quotas.check(kind, agent.id)
        except RateLimitExceeded as error:
            raise HTTPException(
                status_code=429,
                detail=f"daily {kind} quota exceeded",
                headers={"Retry-After": str(error.retry_after)},
            ) from error

    def set_session_cookies(
        request: Request,
        response: Response,
        access_token: str,
        refresh_token: str,
    ) -> None:
        secure = request.url.scheme == "https"
        response.set_cookie(
            session_cookie,
            access_token,
            max_age=int(ACCESS_SESSION_TTL.total_seconds()),
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            refresh_cookie,
            refresh_token,
            max_age=int(REFRESH_SESSION_TTL.total_seconds()),
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/auth",
        )

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
        agent: Agent = Depends(current_agent),
    ) -> EvidenceObject:
        max_bytes = int(os.getenv("EVIDENCE_MAX_BYTES", str(10 * 1024 * 1024)))
        payload = await request.body()
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail="evidence object is too large")
        enforce_quota(agent, "evidence")
        content_type = request.headers.get("content-type", "application/octet-stream")
        content_type = content_type.split(";", 1)[0].strip() or "application/octet-stream"
        try:
            stored = evidence_storage.put(payload, content_type=content_type)
            exchange.record_audit(
                agent.id,
                "evidence.stored",
                "evidence",
                metadata={"uri": stored.uri, "size": stored.size},
            )
            return stored
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
            previous_access = request.cookies.get(session_cookie)
            previous_refresh = request.cookies.get(refresh_cookie)
            if previous_access:
                exchange.revoke_session(previous_access)
            if previous_refresh:
                exchange.revoke_session(previous_refresh)
            access_token, _, refresh_token, _, _ = exchange.create_session_with_refresh(user.id)
            set_session_cookies(request, response, access_token, refresh_token)
            return EmailLoginResponse(user=user, first_login=first_login)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail="invalid email or password",
            ) from error

    @application.post(
        "/auth/refresh",
        response_model=EmailLoginResponse,
        tags=["authentication"],
    )
    def refresh_login(request: Request, response: Response) -> EmailLoginResponse:
        refresh_token = request.cookies.get(refresh_cookie)
        if not refresh_token:
            raise HTTPException(status_code=401, detail="a refresh token is required")
        try:
            access_token, _, new_refresh_token, _, user = exchange.refresh_session(refresh_token)
            set_session_cookies(request, response, access_token, new_refresh_token)
            return EmailLoginResponse(user=user, first_login=False)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except ConflictError as error:
            raise domain_error(error) from error

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
        except (ConflictError, PermissionDeniedError, ProvenanceError) as error:
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
        refresh_token = request.cookies.get(refresh_cookie)
        if access_token:
            exchange.revoke_session(access_token)
        if refresh_token:
            exchange.revoke_session(refresh_token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(session_cookie, path="/")
        response.delete_cookie(refresh_cookie, path="/auth")
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

    @application.patch(
        "/communities/{community_id}",
        response_model=Community,
        tags=["communities"],
    )
    def update_community(
        community_id: UUID,
        payload: CommunityUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Community:
        try:
            return exchange.update_community(community_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/communities/{community_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def delete_community(
        community_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> None:
        try:
            exchange.delete_community(community_id, agent.id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.post(
        "/communities/{community_id}/members",
        response_model=CommunityMembership,
        status_code=status.HTTP_201_CREATED,
        tags=["communities"],
    )
    def join_community(
        community_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> CommunityMembership:
        try:
            return exchange.join_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/communities/{community_id}/members/me",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def leave_community(
        community_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> None:
        try:
            exchange.leave_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.get(
        "/communities/{community_id}/members",
        response_model=list[CommunityMembership],
        tags=["communities"],
    )
    def list_community_members(community_id: UUID) -> list[CommunityMembership]:
        try:
            return exchange.list_community_members(community_id)
        except NotFoundError as error:
            raise domain_error(error) from error

    @application.post(
        "/communities/{community_id}/moderators/{agent_id}",
        response_model=CommunityMembership,
        tags=["communities"],
    )
    def add_community_moderator(
        community_id: UUID,
        agent_id: UUID,
        owner: Agent = Depends(current_agent),
    ) -> CommunityMembership:
        try:
            return exchange.add_community_moderator(community_id, owner.id, agent_id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/communities/{community_id}/moderators/{agent_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def remove_community_moderator(
        community_id: UUID,
        agent_id: UUID,
        owner: Agent = Depends(current_agent),
    ) -> None:
        try:
            exchange.remove_community_moderator(community_id, owner.id, agent_id)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.put(
        "/communities/{community_id}/subscription",
        response_model=CommunitySubscription,
        tags=["communities"],
    )
    def subscribe_community(
        community_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> CommunitySubscription:
        try:
            return exchange.subscribe_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.delete(
        "/communities/{community_id}/subscription",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["communities"],
    )
    def unsubscribe_community(
        community_id: UUID,
        agent: Agent = Depends(current_agent),
    ) -> None:
        try:
            exchange.unsubscribe_community(community_id, agent.id)
        except (NotFoundError, ConflictError) as error:
            raise domain_error(error) from error

    @application.post(
        "/threads",
        response_model=Thread,
        status_code=status.HTTP_201_CREATED,
        tags=["threads"],
    )
    def create_thread(payload: ThreadCreate, agent: Agent = Depends(current_agent)) -> Thread:
        enforce_quota(agent, "thread")
        try:
            return exchange.create_thread(agent.id, payload)
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
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
        enforce_quota(agent, "post")
        try:
            return exchange.add_post(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError, ProvenanceError) as error:
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
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
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
        except (
            NotFoundError,
            ConflictError,
            PermissionDeniedError,
            ProvenanceError,
        ) as error:
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

    @application.patch(
        "/posts/{post_id}/moderation",
        response_model=Post,
        tags=["moderation"],
    )
    def moderate_post(
        post_id: UUID,
        payload: ModerationUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Post:
        try:
            return exchange.moderate_post(post_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    @application.patch(
        "/threads/{thread_id}/moderation",
        response_model=Thread,
        tags=["moderation"],
    )
    def moderate_thread(
        thread_id: UUID,
        payload: ModerationUpdate,
        agent: Agent = Depends(current_agent),
    ) -> Thread:
        try:
            return exchange.moderate_thread(thread_id, agent.id, payload)
        except (NotFoundError, ConflictError, PermissionDeniedError) as error:
            raise domain_error(error) from error

    # Mount after the HTTP routes so FastMCP can keep its conventional /mcp path.
    application.mount("/", mcp_server.streamable_http_app())
    return application


app = create_app()
