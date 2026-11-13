from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth import ACCESS_SESSION_TTL, REFRESH_SESSION_TTL
from ..email_delivery import EmailSender
from ..errors import AuthenticationError, ConflictError, NotFoundError, PermissionDeniedError
from ..limits import QuotaSet, RateLimitExceeded
from ..models import Agent
from ..object_storage import ObjectStorage
from ..ports import ExchangePort
from ..provenance import ProvenanceError


@dataclass(frozen=True, slots=True)
class RouteContext:
    exchange: ExchangePort
    evidence_storage: ObjectStorage
    mailer: EmailSender
    quotas: QuotaSet
    web_url: str
    password_reset_ttl: timedelta
    session_cookie: str
    refresh_cookie: str


@dataclass(frozen=True, slots=True)
class RouteDependencies:
    bearer: HTTPBearer
    current_agent: Callable[..., Agent]
    optional_agent: Callable[..., Agent | None]
    domain_error: Callable[[Exception], HTTPException]
    enforce_quota: Callable[[Agent, str], None]
    set_session_cookies: Callable[[Request, Response, str, str], None]


def build_route_dependencies(context: RouteContext) -> RouteDependencies:
    bearer = HTTPBearer(auto_error=False)

    def current_agent(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Agent:
        access_token = (
            credentials.credentials
            if credentials is not None
            else request.cookies.get(context.session_cookie)
        )
        if not access_token:
            raise HTTPException(
                status_code=401,
                detail="authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return context.exchange.authenticate_token(access_token)
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
            else request.cookies.get(context.session_cookie)
        )
        if not access_token:
            return None
        try:
            return context.exchange.authenticate_token(access_token)
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
            context.quotas.check(kind, agent.id)
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
            context.session_cookie,
            access_token,
            max_age=int(ACCESS_SESSION_TTL.total_seconds()),
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            context.refresh_cookie,
            refresh_token,
            max_age=int(REFRESH_SESSION_TTL.total_seconds()),
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/auth",
        )

    return RouteDependencies(
        bearer=bearer,
        current_agent=current_agent,
        optional_agent=optional_agent,
        domain_error=domain_error,
        enforce_quota=enforce_quota,
        set_session_cookies=set_session_cookies,
    )
