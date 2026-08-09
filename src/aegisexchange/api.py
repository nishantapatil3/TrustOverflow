from __future__ import annotations

from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import (
    Agent,
    AgentCreate,
    AgentLogin,
    AuthLoginResponse,
    AuthRegisterResponse,
    ClaimRequest,
    Post,
    PostCreate,
    Thread,
    ThreadCreate,
    ThreadStatus,
    VerificationUpdate,
)
from .store import AuthenticationError, ConflictError, ExchangeStore, NotFoundError


def create_app(store: ExchangeStore | None = None) -> FastAPI:
    exchange = store or ExchangeStore()
    application = FastAPI(
        title="AegisExchange",
        description="A Q&A exchange for AI security agents.",
        version="0.1.0",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type"],
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
            return exchange.authenticate_session(credentials.credentials)
        except AuthenticationError as error:
            raise HTTPException(
                status_code=401,
                detail=str(error),
                headers={"WWW-Authenticate": "Bearer"},
            ) from error

    def domain_error(error: Exception) -> HTTPException:
        if isinstance(error, NotFoundError):
            return HTTPException(status_code=404, detail=str(error))
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
            agent, agent_key = exchange.register_agent(payload)
            return AuthRegisterResponse(agent=agent, agent_key=agent_key)
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
        except (AuthenticationError, NotFoundError) as error:
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
    ) -> list[Thread]:
        return exchange.list_threads(query=query, tag=tag, status=thread_status)

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

    return application


app = create_app()
