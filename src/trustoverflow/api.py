from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .email_delivery import EmailSender, email_sender_from_environment
from .limits import QuotaSet, RateLimitExceeded, request_limiter_from_environment
from .mcp_server import build_mcp_server
from .object_storage import ObjectStorage, build_object_storage
from .ports import ExchangePort
from .routes.agents import build_agents_router
from .routes.auth import build_router as build_auth_router
from .routes.communities import build_router as build_communities_router
from .routes.context import RouteContext, build_route_dependencies
from .routes.evidence import build_router as build_evidence_router
from .routes.threads import build_router as build_threads_router
from .storage.memory import ExchangeStore
from .storage.postgres import PostgresStore


def create_app(
    store: ExchangePort | None = None,
    database_url: str | None = None,
    frontend_url: str | None = None,
    object_storage: ObjectStorage | None = None,
    email_sender: EmailSender | None = None,
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
    evidence_storage = object_storage or build_object_storage()
    mailer = email_sender or email_sender_from_environment()
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
        title="TrustOverflow",
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

    route_context = RouteContext(
        exchange=exchange,
        evidence_storage=evidence_storage,
        mailer=mailer,
        quotas=quotas,
        web_url=web_url,
        password_reset_ttl=timedelta(
            seconds=int(os.getenv("PASSWORD_RESET_TTL_SECONDS", "1800"))
        ),
        session_cookie="trustoverflow_session",
        refresh_cookie="trustoverflow_refresh",
    )
    dependencies = build_route_dependencies(route_context)

    @application.get("/healthz", tags=["system"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    application.include_router(build_evidence_router(route_context, dependencies))
    application.include_router(build_auth_router(route_context, dependencies))
    application.include_router(build_agents_router(route_context, dependencies))
    application.include_router(build_communities_router(route_context, dependencies))
    application.include_router(build_threads_router(route_context, dependencies))

    # Mount after HTTP routes so FastMCP can keep its conventional /mcp path.
    application.mount("/", mcp_server.streamable_http_app())
    return application


app = create_app()
