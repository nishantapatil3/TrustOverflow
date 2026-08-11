from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials

from ..errors import AuthenticationError, ConflictError, NotFoundError, PermissionDeniedError
from ..models import (
    AccountType,
    Agent,
    AgentCreate,
    AuthRegisterResponse,
    EmailLogin,
    EmailLoginResponse,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
)
from ..provenance import ProvenanceError
from .context import RouteContext, RouteDependencies

logger = logging.getLogger(__name__)


def build_router(context: RouteContext, dependencies: RouteDependencies) -> APIRouter:
    router = APIRouter()
    exchange = context.exchange

    @router.post("/auth/login", response_model=EmailLoginResponse, tags=["authentication"])
    def login_user(payload: EmailLogin, request: Request, response: Response) -> EmailLoginResponse:
        try:
            user, first_login = exchange.login_or_create_user(
                payload.email,
                payload.password.get_secret_value(),
            )
            previous_access = request.cookies.get(context.session_cookie)
            previous_refresh = request.cookies.get(context.refresh_cookie)
            if previous_access:
                exchange.revoke_session(previous_access)
            if previous_refresh:
                exchange.revoke_session(previous_refresh)
            access_token, _, refresh_token, _, _ = exchange.create_session_with_refresh(user.id)
            dependencies.set_session_cookies(request, response, access_token, refresh_token)
            return EmailLoginResponse(user=user, first_login=first_login)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail="invalid email or password") from error

    @router.post(
        "/auth/forgot-password",
        response_model=MessageResponse,
        tags=["authentication"],
    )
    def forgot_password(payload: PasswordResetRequest) -> MessageResponse:
        token = exchange.create_password_reset(
            payload.email,
            datetime.now(UTC) + context.password_reset_ttl,
        )
        if token:
            reset_url = f"{context.web_url}/?{urlencode({'reset_token': token})}"
            try:
                context.mailer.send_password_reset(payload.email, reset_url)
            except Exception:
                logger.exception("password reset email delivery failed")
        return MessageResponse(
            message="If an account exists for that email, a reset link has been sent."
        )

    @router.post(
        "/auth/reset-password",
        response_model=MessageResponse,
        tags=["authentication"],
    )
    def reset_password(payload: PasswordResetConfirm) -> MessageResponse:
        try:
            exchange.reset_password(payload.token, payload.password.get_secret_value())
        except AuthenticationError as error:
            raise HTTPException(
                status_code=400,
                detail="invalid or expired password reset link",
            ) from error
        return MessageResponse(message="Password reset. Sign in with your new password.")

    @router.post("/auth/refresh", response_model=EmailLoginResponse, tags=["authentication"])
    def refresh_login(request: Request, response: Response) -> EmailLoginResponse:
        refresh_token = request.cookies.get(context.refresh_cookie)
        if not refresh_token:
            raise HTTPException(status_code=401, detail="a refresh token is required")
        try:
            access_token, _, new_refresh_token, _, user = exchange.refresh_session(refresh_token)
            dependencies.set_session_cookies(request, response, access_token, new_refresh_token)
            return EmailLoginResponse(user=user, first_login=False)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        except ConflictError as error:
            raise dependencies.domain_error(error) from error

    @router.get("/auth/me", response_model=Agent, tags=["authentication"])
    def get_current_agent(agent: Agent = Depends(dependencies.current_agent)) -> Agent:
        return agent

    @router.get("/auth/me/agents", response_model=list[Agent], tags=["authentication"])
    def list_my_agents(owner: Agent = Depends(dependencies.current_agent)) -> list[Agent]:
        if owner.account_type is not AccountType.USER or owner.email is None:
            raise HTTPException(status_code=403, detail="only email users can own agents")
        try:
            return exchange.list_owned_agents(owner.id)
        except (NotFoundError, PermissionDeniedError) as error:
            raise dependencies.domain_error(error) from error

    @router.post(
        "/auth/me/agents",
        response_model=AuthRegisterResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["authentication"],
    )
    def create_owned_agent(
        payload: AgentCreate,
        owner: Agent = Depends(dependencies.current_agent),
    ) -> AuthRegisterResponse:
        if owner.account_type is not AccountType.USER or owner.email is None:
            raise HTTPException(status_code=403, detail="agents must be created by an email user")
        try:
            agent, token = exchange.register_agent(owner.id, payload)
            return AuthRegisterResponse(agent=agent, token=token, agent_key=token)
        except (ConflictError, PermissionDeniedError, ProvenanceError) as error:
            raise dependencies.domain_error(error) from error

    @router.post(
        "/auth/me/agents/{agent_id}/token",
        response_model=AuthRegisterResponse,
        tags=["authentication"],
    )
    def recover_agent_token(
        agent_id: UUID,
        owner: Agent = Depends(dependencies.current_agent),
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
            raise dependencies.domain_error(error) from error

    @router.post(
        "/auth/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["authentication"],
    )
    def logout_agent(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(dependencies.bearer),
    ) -> Response:
        access_token = (
            credentials.credentials
            if credentials is not None
            else request.cookies.get(context.session_cookie)
        )
        refresh_token = request.cookies.get(context.refresh_cookie)
        if access_token:
            exchange.revoke_session(access_token)
        if refresh_token:
            exchange.revoke_session(refresh_token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(context.session_cookie, path="/")
        response.delete_cookie(context.refresh_cookie, path="/auth")
        return response

    return router
