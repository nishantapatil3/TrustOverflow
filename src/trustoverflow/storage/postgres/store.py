from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID, uuid4

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from ...auth import (
    ACCESS_SESSION_TTL,
    REFRESH_SESSION_TTL,
    digest_secret,
    email_username,
    hash_password,
    normalize_email,
    utc_now,
    verify_password,
)
from ...errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from ...expertise import route_experts as rank_experts
from ...models import (
    DEFAULT_COMMUNITY_ID,
    AccountType,
    Agent,
    AgentCreate,
    AgentProfileUpdate,
    AgentStatus,
    ClaimRequest,
    Community,
    CommunityCreate,
    CommunityMembership,
    CommunityRole,
    CommunitySubscription,
    CommunityUpdate,
    EvidenceRef,
    ExpertiseMatch,
    ModerationStatus,
    ModerationUpdate,
    Post,
    PostCreate,
    PostType,
    PostUpdate,
    Provenance,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    Verification,
    VerificationStatus,
    VerificationUpdate,
    VoteResult,
    agent_username,
    community_slug,
)
from ...moderation import quarantine_reason
from ...provenance import (
    build_provenance,
    post_payload,
    thread_payload,
    validate_public_key,
)
from ...semantic import embed_text, thread_search_text, vector_literal

AGENT_COLUMNS = """
    id,
    name,
    description,
    bio,
    capabilities,
    specialties,
    website_url,
    public_key,
    profile_updated_at,
    created_at,
    reputation,
    status,
    account_type,
    owner_id,
    email
"""

logger = logging.getLogger(__name__)
RetryResult = TypeVar("RetryResult")
RETRYABLE_DATABASE_ERRORS = (
    PoolTimeout,
    psycopg.OperationalError,
    SQLAlchemyOperationalError,
    TimeoutError,
)


class PostgresStore:
    """Postgres-backed exchange store with pooled connections and migrations."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool_min_size = int(os.getenv("DATABASE_POOL_MIN_SIZE", "2"))
        self._pool_max_size = int(os.getenv("DATABASE_POOL_MAX_SIZE", "10"))
        self._connect_retries = max(1, int(os.getenv("DATABASE_CONNECT_RETRIES", "5")))
        self._retry_base_delay = max(
            0.0,
            float(os.getenv("DATABASE_RETRY_BASE_DELAY", "0.25")),
        )
        if self._pool_min_size < 1 or self._pool_max_size < self._pool_min_size:
            raise ValueError("DATABASE_POOL_MAX_SIZE must be at least DATABASE_POOL_MIN_SIZE")
        self._initialize_schema()
        self._pool = self._open_pool()
        self._semantic_vector_available = self._detect_semantic_vector()

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[Any]]:
        with ExitStack() as stack:
            connection = self._with_retry(
                lambda: stack.enter_context(self._pool.connection()),
                "database connection acquisition",
            )
            yield connection

    def _with_retry(
        self,
        operation: Callable[[], RetryResult],
        operation_name: str,
    ) -> RetryResult:
        for attempt in range(self._connect_retries):
            try:
                return operation()
            except RETRYABLE_DATABASE_ERRORS:
                if attempt + 1 >= self._connect_retries:
                    raise
                delay = min(self._retry_base_delay * (2**attempt), 5.0)
                logger.warning(
                    "%s failed; retrying in %.2f seconds (%d/%d)",
                    operation_name,
                    delay,
                    attempt + 1,
                    self._connect_retries,
                )
                time.sleep(delay)
        raise RuntimeError(f"{operation_name} did not return a result")

    def _initialize_schema(self) -> None:
        def migrate() -> None:
            config = Config()
            config.set_main_option(
                "script_location",
                str(Path(__file__).resolve().parent / "migrations"),
            )
            config.attributes["database_url"] = self._database_url
            command.upgrade(config, "head")

        self._with_retry(migrate, "database migration")

    def _open_pool(self) -> ConnectionPool:
        def open_pool() -> ConnectionPool:
            pool = ConnectionPool(
                self._database_url,
                kwargs={"row_factory": dict_row},
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                open=False,
            )
            try:
                pool.open(wait=True)
            except Exception:
                pool.close()
                raise
            return pool

        return self._with_retry(open_pool, "database pool startup")

    def close(self) -> None:
        self._pool.close()

    def _detect_semantic_vector(self) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'threads' AND column_name = 'semantic_embedding'
                ) AS available
                """
            ).fetchone()
            return bool(row and row["available"])

    def _refresh_thread_search(
        self,
        connection: psycopg.Connection[Any],
        thread_id: UUID,
    ) -> None:
        thread = connection.execute(
            "SELECT title, tags FROM threads WHERE id = %s",
            (thread_id,),
        ).fetchone()
        if thread is None:
            return
        posts = connection.execute(
            """
            SELECT body
            FROM posts
            WHERE thread_id = %s AND moderation_status = %s
            ORDER BY created_at, id
            """,
            (thread_id, ModerationStatus.VISIBLE.value),
        ).fetchall()
        search_text = thread_search_text(
            thread["title"],
            [str(tag) for tag in (thread["tags"] or [])],
            [post["body"] for post in posts],
        )
        connection.execute(
            "UPDATE threads SET search_vector = to_tsvector('simple', %s) WHERE id = %s",
            (search_text, thread_id),
        )
        if self._semantic_vector_available:
            connection.execute(
                "UPDATE threads SET semantic_embedding = %s::vector WHERE id = %s",
                (vector_literal(embed_text(search_text)), thread_id),
            )

    @staticmethod
    def _agent_from_row(row: dict[str, Any]) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            bio=row.get("bio"),
            capabilities=row["capabilities"] or [],
            specialties=row.get("specialties") or [],
            website_url=row.get("website_url"),
            public_key=row["public_key"],
            profile_updated_at=row.get("profile_updated_at"),
            created_at=row["created_at"],
            reputation=row["reputation"],
            status=row["status"],
            account_type=row["account_type"],
            owner_id=row["owner_id"],
            email=row["email"],
        )

    @staticmethod
    def _post_from_row(row: dict[str, Any], upvoted: bool = False) -> Post:
        provenance = (
            Provenance(
                signer_id=row["author_id"],
                payload_hash=row["payload_hash"],
                signature=row.get("signature"),
                verified=bool(row.get("signature_valid")),
            )
            if row.get("payload_hash") is not None
            else None
        )
        return Post(
            id=row["id"],
            thread_id=row["thread_id"],
            author_id=row["author_id"],
            parent_id=row["parent_id"],
            type=row["post_type"],
            body=row["body"],
            evidence=row["evidence"] or [],
            confidence=row["confidence"],
            signature=row.get("signature"),
            created_at=row["created_at"],
            score=int(row.get("score") or 0),
            upvoted=upvoted,
            moderation_status=row.get("moderation_status", ModerationStatus.VISIBLE.value),
            quarantine_reason=row.get("quarantine_reason"),
            provenance=provenance,
            verification=Verification(
                status=row["verification_status"],
                verifier_id=row["verifier_id"],
                notes=row["verification_notes"],
                verified_at=row["verified_at"],
            ),
        )

    @staticmethod
    def _community_from_row(row: dict[str, Any]) -> Community:
        return Community(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            created_by=row["created_by"],
            owner_id=row.get("owner_id", row["created_by"]),
            created_at=row["created_at"],
        )

    def _has_upvote(
        self,
        connection: psycopg.Connection[Any],
        agent_id: UUID | None,
        *,
        thread_id: UUID | None = None,
        post_id: UUID | None = None,
    ) -> bool:
        if agent_id is None:
            return False
        target_column = "thread_id" if thread_id is not None else "post_id"
        target_id = thread_id if thread_id is not None else post_id
        return (
            connection.execute(
                f"SELECT 1 FROM upvotes WHERE agent_id = %s AND {target_column} = %s",
                (agent_id, target_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _record_audit(
        connection: psycopg.Connection[Any],
        actor_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                actor_id, action, target_type, target_id, metadata, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (actor_id, action, target_type, target_id, Jsonb(metadata or {}), utc_now()),
        )

    def record_audit(
        self,
        actor_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            self._record_audit(connection, actor_id, action, target_type, target_id, metadata)

    def _thread_from_row(
        self,
        connection: psycopg.Connection[Any],
        row: dict[str, Any],
        viewer_id: UUID | None = None,
    ) -> Thread:
        can_manage = self._can_manage_community(connection, row["community_id"], viewer_id)
        if (
            row.get("moderation_status") == ModerationStatus.QUARANTINED.value
            and row["author_id"] != viewer_id
            and not can_manage
        ):
            raise NotFoundError(f"thread {row['id']} was not found")
        post_visibility = "TRUE"
        post_parameters: tuple[Any, ...] = (row["id"],)
        if not can_manage:
            post_visibility = "(moderation_status = %s OR author_id = %s)"
            post_parameters = (
                row["id"],
                ModerationStatus.VISIBLE.value,
                viewer_id,
            )
        post_rows = connection.execute(
            f"""
            SELECT
                id,
                thread_id,
                author_id,
                parent_id,
                post_type,
                body,
                evidence,
                confidence,
                created_at,
                verification_status,
                verifier_id,
                verification_notes,
                verified_at,
                signature,
                payload_hash,
                signature_valid,
                moderation_status,
                quarantine_reason,
                (SELECT COUNT(*)::int FROM upvotes WHERE upvotes.post_id = posts.id) AS score
            FROM posts
            WHERE thread_id = %s
              AND {post_visibility}
            ORDER BY created_at, id
            """,
            post_parameters,
        ).fetchall()
        provenance = (
            Provenance(
                signer_id=row["author_id"],
                payload_hash=row["payload_hash"],
                signature=row.get("signature"),
                verified=bool(row.get("signature_valid")),
            )
            if row.get("payload_hash") is not None
            else None
        )
        return Thread(
            id=row["id"],
            community_id=row["community_id"],
            title=row["title"],
            tags=row["tags"] or [],
            status=row["status"],
            author_id=row["author_id"],
            claimed_by=row["claimed_by"],
            claim_expires_at=row["claim_expires_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            score=int(row.get("score") or 0),
            upvoted=self._has_upvote(connection, viewer_id, thread_id=row["id"]),
            moderation_status=row.get("moderation_status", ModerationStatus.VISIBLE.value),
            quarantine_reason=row.get("quarantine_reason"),
            provenance=provenance,
            posts=[
                self._post_from_row(
                    post_row,
                    self._has_upvote(connection, viewer_id, post_id=post_row["id"]),
                )
                for post_row in post_rows
            ],
        )

    @staticmethod
    def _thread_select(for_update: bool = False) -> str:
        lock_clause = " FOR UPDATE" if for_update else ""
        return (
            "SELECT threads.*, "
            "(SELECT COUNT(*)::int FROM upvotes WHERE upvotes.thread_id = threads.id) AS score "
            f"FROM threads WHERE threads.id = %s{lock_clause}"
        )

    @staticmethod
    def _post_select(for_update: bool = False) -> str:
        lock_clause = " FOR UPDATE" if for_update else ""
        return (
            "SELECT posts.*, "
            "(SELECT COUNT(*)::int FROM upvotes WHERE upvotes.post_id = posts.id) AS score "
            f"FROM posts WHERE posts.id = %s{lock_clause}"
        )

    def _require_agent(self, connection: psycopg.Connection[Any], agent_id: UUID) -> Agent:
        row = connection.execute(
            f"SELECT {AGENT_COLUMNS} FROM agents WHERE id = %s",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"agent {agent_id} was not found")
        agent = self._agent_from_row(row)
        if agent.status is AgentStatus.SUSPENDED:
            raise ConflictError("the agent is suspended")
        return agent

    def _require_thread(
        self,
        connection: psycopg.Connection[Any],
        thread_id: UUID,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = connection.execute(self._thread_select(for_update), (thread_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"thread {thread_id} was not found")
        return row

    def _require_community(
        self,
        connection: psycopg.Connection[Any],
        community_id: UUID,
    ) -> Community:
        row = connection.execute(
            "SELECT * FROM communities WHERE id = %s",
            (community_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"community {community_id} was not found")
        return self._community_from_row(row)

    @staticmethod
    def _community_membership(
        connection: psycopg.Connection[Any],
        community_id: UUID,
        agent_id: UUID,
    ) -> dict[str, Any] | None:
        return connection.execute(
            """
            SELECT community_id, agent_id, role, created_at
            FROM community_memberships
            WHERE community_id = %s AND agent_id = %s
            """,
            (community_id, agent_id),
        ).fetchone()

    def _can_manage_community(
        self,
        connection: psycopg.Connection[Any],
        community_id: UUID,
        agent_id: UUID | None,
    ) -> bool:
        if agent_id is None:
            return False
        community = self._require_community(connection, community_id)
        if community.owner_id == agent_id or community.created_by == agent_id:
            return True
        membership = self._community_membership(connection, community_id, agent_id)
        return membership is not None and membership["role"] == CommunityRole.MODERATOR.value

    def _require_community_member(
        self,
        connection: psycopg.Connection[Any],
        community_id: UUID,
        agent_id: UUID,
    ) -> None:
        self._require_community(connection, community_id)
        if community_id == DEFAULT_COMMUNITY_ID:
            return
        if self._community_membership(connection, community_id, agent_id) is None:
            raise PermissionDeniedError("join this community before creating a thread")

    def register_agent(self, owner_id: UUID, data: AgentCreate) -> tuple[Agent, str]:
        validate_public_key(data.public_key)
        name = agent_username(data.name)
        if len(name) > 80:
            raise ConflictError("agent names must be at most 74 characters before the prefix")
        if name == "agent-":
            raise ConflictError("agent names must include a name after the agent- prefix")
        agent_data = data.model_dump()
        agent_data["name"] = name
        agent = Agent(
            id=uuid4(),
            created_at=utc_now(),
            owner_id=owner_id,
            account_type=AccountType.AGENT,
            **agent_data,
        )
        token = secrets.token_urlsafe(32)
        try:
            with self._connection() as connection:
                owner = self._require_agent(connection, owner_id)
                if owner.account_type is not AccountType.USER or owner.email is None:
                    raise PermissionDeniedError("agents must be created by an email user")
                duplicate = connection.execute(
                    "SELECT 1 FROM agents WHERE LOWER(name) = LOWER(%s)",
                    (agent.name,),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("an agent with this name already exists")
                connection.execute(
                    """
                    INSERT INTO agents (
                        id, name, description, bio, capabilities, specialties, website_url,
                        public_key, profile_updated_at, created_at, reputation, status,
                        account_type, owner_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        agent.id,
                        agent.name,
                        agent.description,
                        agent.bio,
                        Jsonb(agent.capabilities),
                        Jsonb(agent.specialties),
                        agent.website_url,
                        agent.public_key,
                        agent.profile_updated_at,
                        agent.created_at,
                        agent.reputation,
                        agent.status.value,
                        agent.account_type.value,
                        agent.owner_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO agent_keys (agent_id, key_digest) VALUES (%s, %s)",
                    (agent.id, digest_secret(token)),
                )
                self._record_audit(
                    connection,
                    owner_id,
                    "agent.created",
                    "agent",
                    agent.id,
                    {"name": agent.name},
                )
        except psycopg.errors.UniqueViolation as error:
            raise ConflictError("an agent with this name already exists") from error
        return agent, token

    def rotate_agent_token(self, owner_id: UUID, agent_id: UUID) -> tuple[Agent, str]:
        token = secrets.token_urlsafe(32)
        with self._connection() as connection:
            owner = self._require_agent(connection, owner_id)
            if owner.account_type is not AccountType.USER or owner.email is None:
                raise PermissionDeniedError("only email users can recover agent tokens")
            agent = self._require_agent(connection, agent_id)
            if agent.account_type is not AccountType.AGENT or agent.owner_id != owner.id:
                raise PermissionDeniedError(
                    "this agent is not owned by the signed-in user"
                )

            connection.execute(
                """
                INSERT INTO agent_keys (agent_id, key_digest) VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE SET key_digest = EXCLUDED.key_digest
                """,
                (agent.id, digest_secret(token)),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at = CURRENT_TIMESTAMP WHERE agent_id = %s",
                (agent.id,),
            )
            self._record_audit(connection, owner_id, "agent.token_rotated", "agent", agent.id)
            return agent, token

    def login_or_create_user(self, email: str, password: str) -> tuple[Agent, bool]:
        normalized_email = normalize_email(email)
        try:
            with self._connection() as connection:
                existing = connection.execute(
                    f"SELECT {AGENT_COLUMNS} FROM agents WHERE LOWER(email) = LOWER(%s)",
                    (normalized_email,),
                ).fetchone()
                if existing is not None:
                    credentials = connection.execute(
                        "SELECT password_hash FROM user_credentials WHERE user_id = %s",
                        (existing["id"],),
                    ).fetchone()
                    if credentials is None or not verify_password(
                        password,
                        credentials["password_hash"],
                    ):
                        raise AuthenticationError("invalid email or password")
                    self._record_audit(
                        connection,
                        existing["id"],
                        "user.authenticated",
                        "user",
                        existing["id"],
                    )
                    return self._agent_from_row(existing), False

                used_names = {
                    row["name"].casefold()
                    for row in connection.execute("SELECT name FROM agents").fetchall()
                }
                name = email_username(normalized_email)
                if name.casefold() in used_names:
                    name = f"user-{uuid4().hex[:12]}"

                user = Agent(
                    id=uuid4(),
                    name=name,
                    capabilities=[],
                    created_at=utc_now(),
                    account_type=AccountType.USER,
                    email=normalized_email,
                )
                connection.execute(
                    """
                    INSERT INTO agents (
                        id, name, description, bio, capabilities, specialties, website_url,
                        public_key, profile_updated_at, created_at, reputation, status,
                        account_type, owner_id, email
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user.id,
                        user.name,
                        user.description,
                        user.bio,
                        Jsonb(user.capabilities),
                        Jsonb(user.specialties),
                        user.website_url,
                        user.public_key,
                        user.profile_updated_at,
                        user.created_at,
                        user.reputation,
                        user.status.value,
                        user.account_type.value,
                        user.owner_id,
                        user.email,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO user_credentials (user_id, password_hash, created_at)
                    VALUES (%s, %s, %s)
                    """,
                    (user.id, hash_password(password), user.created_at),
                )
                self._record_audit(connection, user.id, "user.created", "user", user.id)
                return user, True
        except psycopg.errors.UniqueViolation as error:
            with self._connection() as connection:
                existing = connection.execute(
                    f"SELECT {AGENT_COLUMNS} FROM agents WHERE LOWER(email) = LOWER(%s)",
                    (normalized_email,),
                ).fetchone()
                if existing is not None:
                    credentials = connection.execute(
                        "SELECT password_hash FROM user_credentials WHERE user_id = %s",
                        (existing["id"],),
                    ).fetchone()
                    if credentials is not None and verify_password(
                        password,
                        credentials["password_hash"],
                    ):
                        return self._agent_from_row(existing), False
            raise AuthenticationError("invalid email or password") from error

    def create_password_reset(self, email: str, expires_at: datetime) -> str | None:
        normalized_email = normalize_email(email)
        with self._connection() as connection:
            user = connection.execute(
                "SELECT id FROM agents WHERE LOWER(email) = LOWER(%s)",
                (normalized_email,),
            ).fetchone()
            if user is None:
                return None
            token = secrets.token_urlsafe(48)
            connection.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = %s",
                (user["id"],),
            )
            connection.execute(
                """
                INSERT INTO password_reset_tokens (token_digest, user_id, expires_at)
                VALUES (%s, %s, %s)
                """,
                (digest_secret(token), user["id"], expires_at),
            )
            self._record_audit(
                connection,
                user["id"],
                "user.password_reset_requested",
                "user",
                user["id"],
            )
            return token

    def reset_password(self, token: str, password: str) -> None:
        with self._connection() as connection:
            reset = connection.execute(
                """
                SELECT user_id, expires_at
                FROM password_reset_tokens
                WHERE token_digest = %s
                FOR UPDATE
                """,
                (digest_secret(token),),
            ).fetchone()
            if reset is None or reset["expires_at"] <= utc_now():
                raise AuthenticationError("invalid or expired password reset token")
            connection.execute(
                "UPDATE user_credentials SET password_hash = %s WHERE user_id = %s",
                (hash_password(password), reset["user_id"]),
            )
            connection.execute(
                "DELETE FROM password_reset_tokens WHERE user_id = %s",
                (reset["user_id"],),
            )
            connection.execute(
                """
                UPDATE sessions
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE agent_id = %s AND revoked_at IS NULL
                """,
                (reset["user_id"],),
            )
            self._record_audit(
                connection,
                reset["user_id"],
                "user.password_reset_completed",
                "user",
                reset["user_id"],
            )

    def create_session_with_refresh(
        self,
        agent_id: UUID,
    ) -> tuple[str, datetime, str, datetime, Agent]:
        with self._connection() as connection:
            agent = self._require_agent(connection, agent_id)
            access_token = secrets.token_urlsafe(32)
            refresh_token = secrets.token_urlsafe(48)
            now = utc_now()
            expires_at = now + ACCESS_SESSION_TTL
            refresh_expires_at = now + REFRESH_SESSION_TTL
            connection.execute(
                """
                INSERT INTO sessions (
                    token_digest, agent_id, expires_at, refresh_digest,
                    refresh_expires_at, family_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    digest_secret(access_token),
                    agent_id,
                    expires_at,
                    digest_secret(refresh_token),
                    refresh_expires_at,
                    uuid4(),
                ),
            )
            self._record_audit(connection, agent_id, "session.created", "session", None)
            return access_token, expires_at, refresh_token, refresh_expires_at, agent

    def create_session(self, agent_id: UUID) -> tuple[str, datetime, Agent]:
        access_token, expires_at, _, _, agent = self.create_session_with_refresh(agent_id)
        return access_token, expires_at, agent

    def authenticate_session(self, access_token: str) -> Agent:
        session_digest = digest_secret(access_token)
        with self._connection() as connection:
            agent_row = connection.execute(
                f"""
                SELECT {AGENT_COLUMNS}
                FROM agent_keys
                JOIN agents ON agents.id = agent_keys.agent_id
                WHERE agent_keys.key_digest = %s
                """,
                (session_digest,),
            ).fetchone()
            if agent_row is not None:
                agent = self._agent_from_row(agent_row)
                if agent.status is AgentStatus.SUSPENDED:
                    raise ConflictError("the agent is suspended")
                return agent

            row = connection.execute(
                f"""
                SELECT {AGENT_COLUMNS}
                FROM sessions
                JOIN agents ON agents.id = sessions.agent_id
                WHERE sessions.token_digest = %s
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > CURRENT_TIMESTAMP
                """,
                (session_digest,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("invalid or expired access token")
            agent = self._agent_from_row(row)
            if agent.status is AgentStatus.SUSPENDED:
                raise ConflictError("the agent is suspended")
            return agent

    def refresh_session(
        self,
        refresh_token: str,
    ) -> tuple[str, datetime, str, datetime, Agent]:
        refresh_digest = digest_secret(refresh_token)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT sessions.*, {AGENT_COLUMNS}
                FROM sessions
                JOIN agents ON agents.id = sessions.agent_id
                WHERE sessions.refresh_digest = %s
                FOR UPDATE
                """,
                (refresh_digest,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("invalid or expired refresh token")
            if row["revoked_at"] is not None:
                connection.execute(
                    """
                    UPDATE sessions
                    SET revoked_at = CURRENT_TIMESTAMP
                    WHERE family_id = %s AND revoked_at IS NULL
                    """,
                    (row["family_id"],),
                )
                self._record_audit(
                    connection,
                    row["agent_id"],
                    "session.refresh_reuse_detected",
                    "session",
                )
                connection.commit()
                raise AuthenticationError("refresh token reuse detected")

            now = utc_now()
            if row["refresh_expires_at"] <= now:
                connection.execute(
                    "UPDATE sessions SET revoked_at = %s WHERE token_digest = %s",
                    (now, row["token_digest"]),
                )
                connection.commit()
                raise AuthenticationError("invalid or expired refresh token")

            agent = self._agent_from_row(row)
            if agent.status is AgentStatus.SUSPENDED:
                raise ConflictError("the agent is suspended")
            access_token = secrets.token_urlsafe(32)
            new_refresh_token = secrets.token_urlsafe(48)
            access_expires_at = now + ACCESS_SESSION_TTL
            refresh_expires_at = now + REFRESH_SESSION_TTL
            connection.execute(
                "UPDATE sessions SET revoked_at = %s WHERE token_digest = %s",
                (now, row["token_digest"]),
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    token_digest, agent_id, expires_at, refresh_digest,
                    refresh_expires_at, family_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    digest_secret(access_token),
                    row["agent_id"],
                    access_expires_at,
                    digest_secret(new_refresh_token),
                    refresh_expires_at,
                    row["family_id"],
                ),
            )
            self._record_audit(connection, agent.id, "session.refreshed", "session")
            return (
                access_token,
                access_expires_at,
                new_refresh_token,
                refresh_expires_at,
                agent,
            )

    def authenticate_token(self, token: str) -> Agent:
        """Authenticate an agent credential or short-lived login session."""
        return self.authenticate_session(token)

    def revoke_session(self, token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE token_digest = %s OR refresh_digest = %s
                """,
                (digest_secret(token), digest_secret(token)),
            )

    def list_agents(self) -> list[Agent]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents ORDER BY created_at, id"
            ).fetchall()
            return [self._agent_from_row(row) for row in rows]

    def get_agent_profile(self, agent_id: UUID) -> Agent:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents WHERE id = %s",
                (agent_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"agent {agent_id} was not found")
            return self._agent_from_row(row)

    def update_agent_profile(
        self,
        actor_id: UUID,
        agent_id: UUID,
        data: AgentProfileUpdate,
    ) -> Agent:
        with self._connection() as connection:
            actor = self._require_agent(connection, actor_id)
            agent = self._require_agent(connection, agent_id)
            if actor.id != agent.id and agent.owner_id != actor.id:
                raise PermissionDeniedError("only the agent or its owner can edit this profile")
            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one profile field must be provided")
            assignments: list[str] = []
            parameters: list[Any] = []
            for field in ("description", "bio", "website_url"):
                if field in changes:
                    assignments.append(f"{field} = %s")
                    parameters.append(changes[field])
            if data.capabilities is not None:
                assignments.append("capabilities = %s")
                parameters.append(Jsonb(sorted({item.casefold() for item in data.capabilities})))
            if data.specialties is not None:
                assignments.append("specialties = %s")
                parameters.append(Jsonb(sorted({item.casefold() for item in data.specialties})))
            assignments.append("profile_updated_at = %s")
            parameters.extend([utc_now(), agent_id])
            connection.execute(
                f"UPDATE agents SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            self._record_audit(connection, actor_id, "agent.profile_updated", "agent", agent_id)
            row = connection.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents WHERE id = %s",
                (agent_id,),
            ).fetchone()
            assert row is not None
            return self._agent_from_row(row)

    def list_owned_agents(self, owner_id: UUID) -> list[Agent]:
        with self._connection() as connection:
            owner = self._require_agent(connection, owner_id)
            if owner.account_type is not AccountType.USER or owner.email is None:
                raise PermissionDeniedError("only email users can own agents")
            rows = connection.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents WHERE owner_id = %s ORDER BY created_at, id",
                (owner_id,),
            ).fetchall()
            return [self._agent_from_row(row) for row in rows]

    def create_community(self, creator_id: UUID, data: CommunityCreate) -> Community:
        name = data.name.strip()
        slug = community_slug(name)
        if not slug:
            raise ConflictError("community name must contain letters or numbers")
        try:
            with self._connection() as connection:
                self._require_agent(connection, creator_id)
                duplicate = connection.execute(
                    "SELECT 1 FROM communities WHERE LOWER(slug) = LOWER(%s)",
                    (slug,),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("a community with this name already exists")
                community = Community(
                    id=uuid4(),
                    slug=slug,
                    name=name,
                    description=data.description,
                    created_by=creator_id,
                    owner_id=creator_id,
                    created_at=utc_now(),
                )
                connection.execute(
                    """
                    INSERT INTO communities (
                        id, slug, name, description, created_by, owner_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        community.id,
                        community.slug,
                        community.name,
                        community.description,
                        community.created_by,
                        community.owner_id,
                        community.created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO community_memberships (community_id, agent_id, role, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        community.id,
                        creator_id,
                        CommunityRole.OWNER.value,
                        community.created_at,
                    ),
                )
                self._record_audit(
                    connection,
                    creator_id,
                    "community.created",
                    "community",
                    community.id,
                    {"slug": community.slug},
                )
                return community
        except psycopg.errors.UniqueViolation as error:
            raise ConflictError("a community with this name already exists") from error

    def list_communities(self) -> list[Community]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM communities
                ORDER BY (id = %s) DESC, LOWER(name), id
                """,
                (DEFAULT_COMMUNITY_ID,),
            ).fetchall()
            return [self._community_from_row(row) for row in rows]

    def get_community(self, community_id: UUID) -> Community:
        with self._connection() as connection:
            return self._require_community(connection, community_id)

    def update_community(
        self,
        community_id: UUID,
        actor_id: UUID,
        data: CommunityUpdate,
    ) -> Community:
        with self._connection() as connection:
            self._require_community(connection, community_id)
            self._require_agent(connection, actor_id)
            if not self._can_manage_community(connection, community_id, actor_id):
                raise PermissionDeniedError("only community owners and moderators can edit it")
            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one community field must be provided")
            assignments: list[str] = []
            parameters: list[Any] = []
            if "name" in changes:
                name = changes["name"].strip()
                slug = community_slug(name)
                if not slug:
                    raise ConflictError("community name must contain letters or numbers")
                duplicate = connection.execute(
                    "SELECT 1 FROM communities WHERE LOWER(slug) = LOWER(%s) AND id <> %s",
                    (slug, community_id),
                ).fetchone()
                if duplicate is not None:
                    raise ConflictError("a community with this name already exists")
                assignments.extend(["name = %s", "slug = %s"])
                parameters.extend([name, slug])
            if "description" in changes:
                assignments.append("description = %s")
                parameters.append(changes["description"])
            parameters.append(community_id)
            connection.execute(
                f"UPDATE communities SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            self._record_audit(connection, actor_id, "community.updated", "community", community_id)
            row = connection.execute(
                "SELECT * FROM communities WHERE id = %s",
                (community_id,),
            ).fetchone()
            assert row is not None
            return self._community_from_row(row)

    def delete_community(self, community_id: UUID, actor_id: UUID) -> None:
        with self._connection() as connection:
            community = self._require_community(connection, community_id)
            self._require_agent(connection, actor_id)
            if community_id == DEFAULT_COMMUNITY_ID:
                raise ConflictError("the General community cannot be deleted")
            if community.owner_id != actor_id and community.created_by != actor_id:
                raise PermissionDeniedError("only the community owner can delete it")
            if connection.execute(
                "SELECT 1 FROM threads WHERE community_id = %s LIMIT 1",
                (community_id,),
            ).fetchone():
                raise ConflictError("delete or move community threads before deleting it")
            connection.execute(
                "DELETE FROM community_subscriptions WHERE community_id = %s",
                (community_id,),
            )
            connection.execute(
                "DELETE FROM community_memberships WHERE community_id = %s",
                (community_id,),
            )
            connection.execute("DELETE FROM communities WHERE id = %s", (community_id,))
            self._record_audit(connection, actor_id, "community.deleted", "community", community_id)

    def join_community(self, community_id: UUID, agent_id: UUID) -> CommunityMembership:
        with self._connection() as connection:
            self._require_community(connection, community_id)
            self._require_agent(connection, agent_id)
            connection.execute(
                """
                INSERT INTO community_memberships (community_id, agent_id, role, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (community_id, agent_id) DO NOTHING
                """,
                (community_id, agent_id, CommunityRole.MEMBER.value, utc_now()),
            )
            row = self._community_membership(connection, community_id, agent_id)
            assert row is not None
            self._record_audit(connection, agent_id, "community.joined", "community", community_id)
            return CommunityMembership(**row)

    def leave_community(self, community_id: UUID, agent_id: UUID) -> None:
        with self._connection() as connection:
            community = self._require_community(connection, community_id)
            membership = self._community_membership(connection, community_id, agent_id)
            if membership is None:
                raise NotFoundError("community membership was not found")
            if community.owner_id == agent_id or membership["role"] == CommunityRole.OWNER.value:
                raise ConflictError("the community owner cannot leave")
            connection.execute(
                "DELETE FROM community_memberships WHERE community_id = %s AND agent_id = %s",
                (community_id, agent_id),
            )
            self._record_audit(connection, agent_id, "community.left", "community", community_id)

    def list_community_members(self, community_id: UUID) -> list[CommunityMembership]:
        with self._connection() as connection:
            self._require_community(connection, community_id)
            rows = connection.execute(
                """
                SELECT community_id, agent_id, role, created_at
                FROM community_memberships
                WHERE community_id = %s
                ORDER BY role, created_at, agent_id
                """,
                (community_id,),
            ).fetchall()
            return [CommunityMembership(**row) for row in rows]

    def add_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> CommunityMembership:
        with self._connection() as connection:
            community = self._require_community(connection, community_id)
            self._require_agent(connection, agent_id)
            if community.owner_id != owner_id and community.created_by != owner_id:
                raise PermissionDeniedError("only the community owner can manage moderators")
            existing = self._community_membership(connection, community_id, agent_id)
            if existing is not None and existing["role"] == CommunityRole.OWNER.value:
                return CommunityMembership(**existing)
            connection.execute(
                """
                INSERT INTO community_memberships (community_id, agent_id, role, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (community_id, agent_id)
                DO UPDATE SET role = EXCLUDED.role
                """,
                (community_id, agent_id, CommunityRole.MODERATOR.value, utc_now()),
            )
            row = self._community_membership(connection, community_id, agent_id)
            assert row is not None
            self._record_audit(
                connection,
                owner_id,
                "community.moderator_added",
                "community",
                community_id,
            )
            return CommunityMembership(**row)

    def remove_community_moderator(
        self,
        community_id: UUID,
        owner_id: UUID,
        agent_id: UUID,
    ) -> None:
        with self._connection() as connection:
            community = self._require_community(connection, community_id)
            if community.owner_id != owner_id and community.created_by != owner_id:
                raise PermissionDeniedError("only the community owner can manage moderators")
            membership = self._community_membership(connection, community_id, agent_id)
            if membership is None or membership["role"] != CommunityRole.MODERATOR.value:
                raise NotFoundError("community moderator was not found")
            connection.execute(
                """
                UPDATE community_memberships SET role = %s
                WHERE community_id = %s AND agent_id = %s
                """,
                (CommunityRole.MEMBER.value, community_id, agent_id),
            )
            self._record_audit(
                connection,
                owner_id,
                "community.moderator_removed",
                "community",
                community_id,
            )

    def subscribe_community(self, community_id: UUID, agent_id: UUID) -> CommunitySubscription:
        with self._connection() as connection:
            self._require_community(connection, community_id)
            self._require_agent(connection, agent_id)
            created_at = utc_now()
            connection.execute(
                """
                INSERT INTO community_subscriptions (community_id, agent_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (community_id, agent_id) DO NOTHING
                """,
                (community_id, agent_id, created_at),
            )
            row = connection.execute(
                """
                SELECT community_id, agent_id, created_at
                FROM community_subscriptions
                WHERE community_id = %s AND agent_id = %s
                """,
                (community_id, agent_id),
            ).fetchone()
            assert row is not None
            self._record_audit(
                connection,
                agent_id,
                "community.subscribed",
                "community",
                community_id,
            )
            return CommunitySubscription(**row)

    def unsubscribe_community(self, community_id: UUID, agent_id: UUID) -> None:
        with self._connection() as connection:
            self._require_community(connection, community_id)
            deleted = connection.execute(
                """
                DELETE FROM community_subscriptions
                WHERE community_id = %s AND agent_id = %s
                RETURNING community_id
                """,
                (community_id, agent_id),
            ).fetchone()
            if deleted is None:
                raise NotFoundError("community subscription was not found")
            self._record_audit(
                connection,
                agent_id,
                "community.unsubscribed",
                "community",
                community_id,
            )

    def create_thread(self, author_id: UUID, data: ThreadCreate) -> Thread:
        tags = sorted({tag.casefold() for tag in data.tags})
        community_id = data.community_id or DEFAULT_COMMUNITY_ID
        now = utc_now()
        thread_id = uuid4()
        post_id = uuid4()
        with self._connection() as connection:
            author = self._require_agent(connection, author_id)
            self._require_community_member(connection, community_id, author_id)
            quarantine = quarantine_reason(text=[data.title, data.body])
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=thread_payload(
                    title=data.title,
                    body=data.body,
                    tags=tags,
                    community_id=community_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO threads (
                    id, community_id, title, tags, status, author_id, created_at, updated_at,
                    signature, payload_hash, signature_valid, moderation_status, quarantine_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    thread_id,
                    community_id,
                    data.title,
                    Jsonb(tags),
                    ThreadStatus.OPEN.value,
                    author_id,
                    now,
                    now,
                    provenance.signature,
                    provenance.payload_hash,
                    provenance.verified,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value
                    ),
                    quarantine,
                ),
            )
            connection.execute(
                """
                INSERT INTO posts (
                    id, thread_id, author_id, post_type, body, evidence,
                    confidence, created_at, moderation_status, quarantine_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    post_id,
                    thread_id,
                    author_id,
                    PostType.QUESTION.value,
                    data.body,
                    Jsonb([]),
                    0.5,
                    now,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value
                    ),
                    quarantine,
                ),
            )
            self._refresh_thread_search(connection, thread_id)
            self._record_audit(connection, author_id, "thread.created", "thread", thread_id)
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                author_id,
            )

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
        community_id: UUID | None = None,
        viewer_id: UUID | None = None,
        semantic: bool = False,
    ) -> list[Thread]:
        if viewer_id is None:
            conditions = ["moderation_status = %s"]
            parameters: list[Any] = [ModerationStatus.VISIBLE.value]
        else:
            conditions = [
                """
                (
                    moderation_status = %s
                    OR author_id = %s
                    OR EXISTS (
                        SELECT 1 FROM communities AS viewer_community
                        WHERE viewer_community.id = threads.community_id
                          AND viewer_community.owner_id = %s
                    )
                    OR EXISTS (
                        SELECT 1 FROM community_memberships AS viewer_membership
                        WHERE viewer_membership.community_id = threads.community_id
                          AND viewer_membership.agent_id = %s
                          AND viewer_membership.role = %s
                    )
                )
                """
            ]
            parameters = [
                ModerationStatus.VISIBLE.value,
                viewer_id,
                viewer_id,
                viewer_id,
                CommunityRole.MODERATOR.value,
            ]
        if community_id is not None:
            conditions.append("community_id = %s")
            parameters.append(community_id)
        if status is not None:
            conditions.append("status = %s")
            parameters.append(status.value)
        if tag:
            conditions.append("tags @> %s")
            parameters.append(Jsonb([tag.casefold()]))
        order_by = "updated_at DESC"
        order_parameters: list[Any] = []
        if query and query.strip() and semantic:
            query_text = query.strip()
            if self._semantic_vector_available:
                conditions.append(
                    "(semantic_embedding IS NOT NULL OR "
                    "search_vector @@ websearch_to_tsquery('simple', %s))"
                )
                parameters.append(query_text)
                order_by = (
                    "semantic_embedding <=> %s::vector ASC NULLS LAST, "
                    "ts_rank_cd(search_vector, websearch_to_tsquery('simple', %s)) DESC, "
                    "updated_at DESC"
                )
                order_parameters.extend([vector_literal(embed_text(query_text)), query_text])
            else:
                conditions.append("search_vector @@ websearch_to_tsquery('simple', %s)")
                parameters.append(query_text)
                order_by = (
                    "ts_rank_cd(search_vector, websearch_to_tsquery('simple', %s)) DESC, "
                    "updated_at DESC"
                )
                order_parameters.append(query_text)
        elif query and query.strip():
            pattern = f"%{query.casefold()}%"
            post_visibility = "posts.moderation_status = %s"
            post_parameters: list[Any] = [ModerationStatus.VISIBLE.value]
            if viewer_id is not None:
                post_visibility = "(posts.moderation_status = %s OR posts.author_id = %s)"
                post_parameters.append(viewer_id)
            conditions.append(
                "(title ILIKE %s OR tags::text ILIKE %s OR EXISTS ("
                f"SELECT 1 FROM posts WHERE posts.thread_id = threads.id AND {post_visibility} "
                "AND body ILIKE %s))"
            )
            parameters.extend([pattern, pattern, *post_parameters, pattern])

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT threads.*,
                    (SELECT COUNT(*)::int FROM upvotes
                     WHERE upvotes.thread_id = threads.id) AS score
                FROM threads
                WHERE {' AND '.join(conditions)}
                ORDER BY {order_by}
                """,
                [*parameters, *order_parameters],
            ).fetchall()
            return [self._thread_from_row(connection, row, viewer_id) for row in rows]

    def get_thread(self, thread_id: UUID, viewer_id: UUID | None = None) -> Thread:
        with self._connection() as connection:
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                viewer_id,
            )

    def route_experts(
        self,
        thread_id: UUID,
        *,
        viewer_id: UUID | None = None,
        limit: int = 5,
    ) -> list[ExpertiseMatch]:
        with self._connection() as connection:
            thread = self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                viewer_id,
            )
            rows = connection.execute(
                f"""
                SELECT {AGENT_COLUMNS}
                FROM agents
                WHERE account_type = %s AND status = %s
                ORDER BY created_at
                """,
                (AccountType.AGENT.value, AgentStatus.ACTIVE.value),
            ).fetchall()
            query = thread_search_text(
                thread.title,
                thread.tags,
                [post.body for post in thread.posts],
            )
            return rank_experts(
                query,
                [self._agent_from_row(row) for row in rows],
                limit,
            )

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post:
        now = utc_now()
        post_id = uuid4()
        evidence = [item.model_dump() for item in data.evidence]
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            author = self._require_agent(connection, author_id)
            if (
                thread["moderation_status"] == ModerationStatus.QUARANTINED.value
                and thread["author_id"] != author_id
            ):
                raise NotFoundError(f"thread {thread_id} was not found")
            if thread["status"] in {ThreadStatus.RESOLVED.value, ThreadStatus.CLOSED.value}:
                raise ConflictError("cannot add a post to a resolved or closed thread")
            if data.parent_id is not None:
                parent = connection.execute(
                    "SELECT thread_id FROM posts WHERE id = %s",
                    (data.parent_id,),
                ).fetchone()
                if parent is None or parent["thread_id"] != thread_id:
                    raise NotFoundError(
                        f"parent post {data.parent_id} was not found in this thread"
                    )
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=post_payload(
                    thread_id=thread_id,
                    parent_id=data.parent_id,
                    post_type=data.type,
                    body=data.body,
                    evidence=data.evidence,
                    confidence=data.confidence,
                ),
            )
            quarantine = quarantine_reason(text=[data.body], evidence=data.evidence)
            connection.execute(
                """
                INSERT INTO posts (
                    id, thread_id, author_id, parent_id, post_type, body, evidence,
                    confidence, created_at, signature, payload_hash, signature_valid,
                    moderation_status, quarantine_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    post_id,
                    thread_id,
                    author_id,
                    data.parent_id,
                    data.type.value,
                    data.body,
                    Jsonb(evidence),
                    data.confidence,
                    now,
                    provenance.signature,
                    provenance.payload_hash,
                    provenance.verified,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value
                    ),
                    quarantine,
                ),
            )
            connection.execute(
                "UPDATE threads SET status = %s, updated_at = %s WHERE id = %s",
                (ThreadStatus.IN_PROGRESS.value, now, thread_id),
            )
            self._refresh_thread_search(connection, thread_id)
            self._record_audit(connection, author_id, "post.created", "post", post_id)
            row = connection.execute(self._post_select(), (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(row)

    def update_post(self, post_id: UUID, author_id: UUID, data: PostUpdate) -> Post:
        with self._connection() as connection:
            post = connection.execute(self._post_select(for_update=True), (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            author = self._require_agent(connection, author_id)
            if post["author_id"] != author_id:
                raise PermissionDeniedError("only the post author can edit this post")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one post field must be provided")

            post_type = data.type if data.type is not None else PostType(post["post_type"])
            evidence_refs = (
                data.evidence
                if data.evidence is not None
                else [EvidenceRef.model_validate(item) for item in (post["evidence"] or [])]
            )
            body = data.body if data.body is not None else post["body"]
            confidence = data.confidence if data.confidence is not None else post["confidence"]
            quarantine = quarantine_reason(text=[body], evidence=evidence_refs)
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=post_payload(
                    thread_id=post["thread_id"],
                    parent_id=post["parent_id"],
                    post_type=post_type,
                    body=body,
                    evidence=evidence_refs,
                    confidence=confidence,
                ),
            )

            assignments: list[str] = []
            parameters: list[Any] = []
            if "type" in changes:
                assignments.append("post_type = %s")
                parameters.append(changes["type"].value)
            if "body" in changes:
                assignments.append("body = %s")
                parameters.append(changes["body"])
            if "evidence" in changes:
                assignments.append("evidence = %s")
                parameters.append(Jsonb([item.model_dump() for item in data.evidence or []]))
            if "confidence" in changes:
                assignments.append("confidence = %s")
                parameters.append(changes["confidence"])
            assignments.extend(
                [
                    "signature = %s",
                    "payload_hash = %s",
                    "signature_valid = %s",
                    "moderation_status = %s",
                    "quarantine_reason = %s",
                ]
            )
            parameters.extend(
                [
                    provenance.signature,
                    provenance.payload_hash,
                    provenance.verified,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value
                    ),
                    quarantine,
                ]
            )
            parameters.append(post_id)
            connection.execute(
                f"UPDATE posts SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            connection.execute(
                "UPDATE threads SET updated_at = %s WHERE id = %s",
                (utc_now(), post["thread_id"]),
            )
            self._refresh_thread_search(connection, post["thread_id"])
            self._record_audit(connection, author_id, "post.updated", "post", post_id)
            row = connection.execute(self._post_select(), (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(
                row,
                self._has_upvote(connection, author_id, post_id=post_id),
            )

    def delete_post(self, post_id: UUID, author_id: UUID) -> None:
        with self._connection() as connection:
            post = connection.execute(self._post_select(for_update=True), (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self._require_agent(connection, author_id)
            if post["author_id"] != author_id:
                raise PermissionDeniedError("only the post author can delete this post")

            if post["parent_id"] is None:
                connection.execute("DELETE FROM threads WHERE id = %s", (post["thread_id"],))
                self._record_audit(
                    connection,
                    author_id,
                    "thread.deleted",
                    "thread",
                    post["thread_id"],
                )
                return

            connection.execute(
                """
                WITH RECURSIVE descendants AS (
                    SELECT id FROM posts WHERE id = %s
                    UNION ALL
                    SELECT child.id
                    FROM posts AS child
                    JOIN descendants AS parent ON child.parent_id = parent.id
                )
                DELETE FROM posts WHERE id IN (SELECT id FROM descendants)
                """,
                (post_id,),
            )
            connection.execute(
                "UPDATE threads SET updated_at = %s WHERE id = %s",
                (utc_now(), post["thread_id"]),
            )
            self._refresh_thread_search(connection, post["thread_id"])
            self._record_audit(connection, author_id, "post.deleted", "post", post_id)

    def update_thread(self, thread_id: UUID, author_id: UUID, data: ThreadUpdate) -> Thread:
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            author = self._require_agent(connection, author_id)
            if thread["author_id"] != author_id:
                raise PermissionDeniedError("only the thread author can edit this thread")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one thread field must be provided")
            now = utc_now()
            root = connection.execute(
                "SELECT id, body FROM posts WHERE thread_id = %s AND parent_id IS NULL",
                (thread_id,),
            ).fetchone()
            if root is None:
                raise ConflictError("thread has no root post")
            title = data.title if data.title is not None else thread["title"]
            tags = (
                sorted({tag.casefold() for tag in data.tags})
                if data.tags is not None
                else thread["tags"] or []
            )
            body = data.body if data.body is not None else root["body"]
            quarantine = quarantine_reason(text=[title, body])
            provenance = build_provenance(
                signer_id=author_id,
                public_key=author.public_key,
                signature=data.signature,
                payload=thread_payload(
                    title=title,
                    body=body,
                    tags=tags,
                    community_id=thread["community_id"],
                ),
            )
            assignments: list[str] = []
            parameters: list[Any] = []
            if "title" in changes:
                assignments.append("title = %s")
                parameters.append(changes["title"])
            if "tags" in changes:
                assignments.append("tags = %s")
                parameters.append(Jsonb(sorted({tag.casefold() for tag in changes["tags"]})))
            assignments.extend(
                [
                    "signature = %s",
                    "payload_hash = %s",
                    "signature_valid = %s",
                    "moderation_status = %s",
                    "quarantine_reason = %s",
                    "updated_at = %s",
                ]
            )
            parameters.extend(
                [
                    provenance.signature,
                    provenance.payload_hash,
                    provenance.verified,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value
                    ),
                    quarantine,
                    now,
                    thread_id,
                ]
            )
            connection.execute(
                f"UPDATE threads SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            if "body" in changes:
                connection.execute(
                    """
                    UPDATE posts
                    SET body = %s, moderation_status = %s, quarantine_reason = %s
                    WHERE id = %s
                    """,
                    (
                        changes["body"],
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value,
                        quarantine,
                        root["id"],
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE posts
                    SET moderation_status = %s, quarantine_reason = %s
                    WHERE id = %s
                    """,
                    (
                        ModerationStatus.QUARANTINED.value
                        if quarantine
                        else ModerationStatus.VISIBLE.value,
                        quarantine,
                        root["id"],
                    ),
                )
            self._refresh_thread_search(connection, thread_id)
            self._record_audit(connection, author_id, "thread.updated", "thread", thread_id)
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                author_id,
            )

    def delete_thread(self, thread_id: UUID, author_id: UUID) -> None:
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, author_id)
            if thread["author_id"] != author_id:
                raise PermissionDeniedError("only the thread author can delete this thread")
            connection.execute("DELETE FROM threads WHERE id = %s", (thread_id,))
            self._record_audit(connection, author_id, "thread.deleted", "thread", thread_id)

    def claim_thread(self, thread_id: UUID, agent_id: UUID, data: ClaimRequest) -> Thread:
        now = utc_now()
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, agent_id)
            claim_is_active = (
                thread["claimed_by"] is not None
                and thread["claim_expires_at"] is not None
                and thread["claim_expires_at"] > now
            )
            if claim_is_active and thread["claimed_by"] != agent_id:
                raise ConflictError("thread is already claimed by another active agent")
            expires_at = now + timedelta(minutes=data.lease_minutes)
            connection.execute(
                """
                UPDATE threads
                SET claimed_by = %s, claim_expires_at = %s,
                    status = %s, updated_at = %s
                WHERE id = %s
                """,
                (agent_id, expires_at, ThreadStatus.IN_PROGRESS.value, now, thread_id),
            )
            self._record_audit(connection, agent_id, "thread.claimed", "thread", thread_id)
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                agent_id,
            )

    def verify_post(self, post_id: UUID, verifier_id: UUID, data: VerificationUpdate) -> Post:
        now = utc_now()
        with self._connection() as connection:
            post = connection.execute(self._post_select(for_update=True), (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self._require_agent(connection, verifier_id)
            if data.status is VerificationStatus.REPRODUCED:
                thread_status = ThreadStatus.RESOLVED.value
            elif data.status is VerificationStatus.FAILED:
                thread_status = ThreadStatus.OPEN.value
            else:
                thread_status = ThreadStatus.IN_PROGRESS.value
            connection.execute(
                """
                UPDATE posts
                SET verification_status = %s, verifier_id = %s,
                    verification_notes = %s, verified_at = %s
                WHERE id = %s
                """,
                (data.status.value, verifier_id, data.notes, now, post_id),
            )
            connection.execute(
                "UPDATE threads SET status = %s, updated_at = %s WHERE id = %s",
                (thread_status, now, post["thread_id"]),
            )
            self._record_audit(connection, verifier_id, "post.verified", "post", post_id)
            row = connection.execute(self._post_select(), (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(
                row,
                self._has_upvote(connection, verifier_id, post_id=post_id),
            )

    def moderate_post(self, post_id: UUID, moderator_id: UUID, data: ModerationUpdate) -> Post:
        with self._connection() as connection:
            post = connection.execute(self._post_select(for_update=True), (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            thread = self._require_thread(connection, post["thread_id"], for_update=True)
            self._require_agent(connection, moderator_id)
            if not self._can_manage_community(connection, thread["community_id"], moderator_id):
                raise PermissionDeniedError("only community owners and moderators can moderate")
            reason = (
                "manual moderation quarantine"
                if data.status is ModerationStatus.QUARANTINED
                else None
            )
            connection.execute(
                """
                UPDATE posts SET moderation_status = %s, quarantine_reason = %s
                WHERE id = %s
                """,
                (data.status.value, reason, post_id),
            )
            if post["parent_id"] is None:
                connection.execute(
                    """
                    UPDATE threads SET moderation_status = %s, quarantine_reason = %s
                    WHERE id = %s
                    """,
                    (data.status.value, reason, post["thread_id"]),
                )
            self._refresh_thread_search(connection, post["thread_id"])
            self._record_audit(connection, moderator_id, "post.moderated", "post", post_id)
            row = connection.execute(self._post_select(), (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(
                row,
                self._has_upvote(connection, moderator_id, post_id=post_id),
            )

    def moderate_thread(
        self,
        thread_id: UUID,
        moderator_id: UUID,
        data: ModerationUpdate,
    ) -> Thread:
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, moderator_id)
            if not self._can_manage_community(connection, thread["community_id"], moderator_id):
                raise PermissionDeniedError("only community owners and moderators can moderate")
            reason = (
                "manual moderation quarantine"
                if data.status is ModerationStatus.QUARANTINED
                else None
            )
            connection.execute(
                """
                UPDATE threads SET moderation_status = %s, quarantine_reason = %s
                WHERE id = %s
                """,
                (data.status.value, reason, thread_id),
            )
            connection.execute(
                """
                UPDATE posts
                SET moderation_status = %s, quarantine_reason = %s
                WHERE thread_id = %s AND parent_id IS NULL
                """,
                (data.status.value, reason, thread_id),
            )
            self._refresh_thread_search(connection, thread_id)
            self._record_audit(connection, moderator_id, "thread.moderated", "thread", thread_id)
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
                moderator_id,
            )

    def upvote_thread(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._connection() as connection:
            self._require_agent(connection, agent_id)
            self._require_thread(connection, thread_id)
            connection.execute(
                """
                INSERT INTO upvotes (agent_id, thread_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (agent_id, thread_id, utc_now()),
            )
            score = connection.execute(
                "SELECT COUNT(*)::int AS score FROM upvotes WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()["score"]
            self._record_audit(connection, agent_id, "thread.upvoted", "thread", thread_id)
            return VoteResult(target_id=thread_id, score=score, upvoted=True)

    def remove_thread_upvote(self, thread_id: UUID, agent_id: UUID) -> VoteResult:
        with self._connection() as connection:
            self._require_agent(connection, agent_id)
            self._require_thread(connection, thread_id)
            connection.execute(
                "DELETE FROM upvotes WHERE agent_id = %s AND thread_id = %s",
                (agent_id, thread_id),
            )
            score = connection.execute(
                "SELECT COUNT(*)::int AS score FROM upvotes WHERE thread_id = %s",
                (thread_id,),
            ).fetchone()["score"]
            self._record_audit(connection, agent_id, "thread.upvote_removed", "thread", thread_id)
            return VoteResult(target_id=thread_id, score=score, upvoted=False)

    def upvote_post(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._connection() as connection:
            self._require_agent(connection, agent_id)
            post = connection.execute("SELECT id FROM posts WHERE id = %s", (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            connection.execute(
                """
                INSERT INTO upvotes (agent_id, post_id, created_at)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (agent_id, post_id, utc_now()),
            )
            score = connection.execute(
                "SELECT COUNT(*)::int AS score FROM upvotes WHERE post_id = %s",
                (post_id,),
            ).fetchone()["score"]
            self._record_audit(connection, agent_id, "post.upvoted", "post", post_id)
            return VoteResult(target_id=post_id, score=score, upvoted=True)

    def remove_post_upvote(self, post_id: UUID, agent_id: UUID) -> VoteResult:
        with self._connection() as connection:
            self._require_agent(connection, agent_id)
            post = connection.execute("SELECT id FROM posts WHERE id = %s", (post_id,)).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            connection.execute(
                "DELETE FROM upvotes WHERE agent_id = %s AND post_id = %s",
                (agent_id, post_id),
            )
            score = connection.execute(
                "SELECT COUNT(*)::int AS score FROM upvotes WHERE post_id = %s",
                (post_id,),
            ).fetchone()["score"]
            self._record_audit(connection, agent_id, "post.upvote_removed", "post", post_id)
            return VoteResult(target_id=post_id, score=score, upvoted=False)
