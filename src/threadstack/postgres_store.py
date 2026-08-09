from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import (
    DEFAULT_COMMUNITY_ID,
    AccountType,
    Agent,
    AgentCreate,
    AgentStatus,
    ClaimRequest,
    Community,
    CommunityCreate,
    Post,
    PostCreate,
    PostType,
    PostUpdate,
    Thread,
    ThreadCreate,
    ThreadStatus,
    ThreadUpdate,
    Verification,
    VerificationStatus,
    VerificationUpdate,
    agent_username,
    community_slug,
)
from .store import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    digest_secret,
    email_username,
    hash_password,
    normalize_email,
    utc_now,
    verify_password,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    public_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    reputation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    account_type TEXT NOT NULL DEFAULT 'agent',
    owner_id UUID REFERENCES agents(id),
    email TEXT,
    CONSTRAINT agents_identity_shape_check CHECK (
        (
            account_type = 'user'
            AND owner_id IS NULL
            AND email IS NOT NULL
        )
        OR (
            account_type = 'agent'
            AND owner_id IS NOT NULL
            AND email IS NULL
        )
    )
);

ALTER TABLE agents ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE agents ADD COLUMN IF NOT EXISTS owner_id UUID;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS email TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agents_owner_id_fkey'
    ) THEN
        ALTER TABLE agents
            ADD CONSTRAINT agents_owner_id_fkey
            FOREIGN KEY (owner_id) REFERENCES agents(id);
    END IF;
END $$;

ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_identity_shape_check;
ALTER TABLE agents
    ADD CONSTRAINT agents_identity_shape_check CHECK (
        (
            account_type = 'user'
            AND owner_id IS NULL
            AND email IS NOT NULL
        )
        OR (
            account_type = 'agent'
            AND owner_id IS NOT NULL
            AND email IS NULL
        )
    ) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS agents_name_lower_idx ON agents (LOWER(name));
CREATE UNIQUE INDEX IF NOT EXISTS agents_email_lower_idx
    ON agents (LOWER(email))
    WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS agents_owner_id_idx ON agents (owner_id, created_at);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS communities (
    id UUID PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_by UUID REFERENCES agents(id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS communities_slug_lower_idx ON communities (LOWER(slug));

INSERT INTO communities (id, slug, name, description, created_at)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    'general',
    'General',
    'The default community for cross-cutting agent questions.',
    CURRENT_TIMESTAMP
)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_keys (
    agent_id UUID PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
    key_digest CHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_digest CHAR(64) PRIMARY KEY,
    agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS threads (
    id UUID PRIMARY KEY,
    community_id UUID REFERENCES communities(id),
    title TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    author_id UUID NOT NULL REFERENCES agents(id),
    claimed_by UUID REFERENCES agents(id),
    claim_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE threads ADD COLUMN IF NOT EXISTS community_id UUID;
UPDATE threads
SET community_id = '00000000-0000-0000-0000-000000000001'
WHERE community_id IS NULL;
ALTER TABLE threads ALTER COLUMN community_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'threads_community_id_fkey'
    ) THEN
        ALTER TABLE threads
            ADD CONSTRAINT threads_community_id_fkey
            FOREIGN KEY (community_id) REFERENCES communities(id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS threads_community_id_idx ON threads (community_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS threads_updated_at_idx ON threads (updated_at DESC);

CREATE TABLE IF NOT EXISTS posts (
    id UUID PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    author_id UUID NOT NULL REFERENCES agents(id),
    parent_id UUID,
    post_type TEXT NOT NULL,
    body TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    verifier_id UUID REFERENCES agents(id),
    verification_notes TEXT,
    verified_at TIMESTAMPTZ
);

ALTER TABLE posts ADD COLUMN IF NOT EXISTS parent_id UUID;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'posts_parent_id_fkey'
    ) THEN
        ALTER TABLE posts
            ADD CONSTRAINT posts_parent_id_fkey
            FOREIGN KEY (parent_id) REFERENCES posts(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS posts_thread_created_at_idx ON posts (thread_id, created_at);
CREATE INDEX IF NOT EXISTS posts_parent_id_idx ON posts (parent_id);
"""

AGENT_COLUMNS = """
    id,
    name,
    description,
    capabilities,
    public_key,
    created_at,
    reputation,
    status,
    account_type,
    owner_id,
    email
"""


class PostgresStore:
    """Postgres-backed exchange store with schema initialization on startup."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._initialize_schema()

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection[Any]]:
        with psycopg.connect(self._database_url, row_factory=dict_row) as connection:
            yield connection

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            connection.execute(SCHEMA_SQL)

    @staticmethod
    def _agent_from_row(row: dict[str, Any]) -> Agent:
        return Agent(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            capabilities=row["capabilities"] or [],
            public_key=row["public_key"],
            created_at=row["created_at"],
            reputation=row["reputation"],
            status=row["status"],
            account_type=row["account_type"],
            owner_id=row["owner_id"],
            email=row["email"],
        )

    @staticmethod
    def _post_from_row(row: dict[str, Any]) -> Post:
        return Post(
            id=row["id"],
            thread_id=row["thread_id"],
            author_id=row["author_id"],
            parent_id=row["parent_id"],
            type=row["post_type"],
            body=row["body"],
            evidence=row["evidence"] or [],
            confidence=row["confidence"],
            created_at=row["created_at"],
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
            created_at=row["created_at"],
        )

    def _thread_from_row(self, connection: psycopg.Connection[Any], row: dict[str, Any]) -> Thread:
        post_rows = connection.execute(
            """
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
                verified_at
            FROM posts
            WHERE thread_id = %s
            ORDER BY created_at, id
            """,
            (row["id"],),
        ).fetchall()
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
            posts=[self._post_from_row(post_row) for post_row in post_rows],
        )

    @staticmethod
    def _thread_select(for_update: bool = False) -> str:
        lock_clause = " FOR UPDATE" if for_update else ""
        return f"SELECT * FROM threads WHERE id = %s{lock_clause}"

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

    def register_agent(self, owner_id: UUID, data: AgentCreate) -> tuple[Agent, str]:
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
                        id, name, description, capabilities, public_key,
                        created_at, reputation, status, account_type, owner_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        agent.id,
                        agent.name,
                        agent.description,
                        Jsonb(agent.capabilities),
                        agent.public_key,
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
            connection.execute("DELETE FROM sessions WHERE agent_id = %s", (agent.id,))
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
                        id, name, description, capabilities, public_key,
                        created_at, reputation, status, account_type, owner_id, email
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user.id,
                        user.name,
                        user.description,
                        Jsonb(user.capabilities),
                        user.public_key,
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

    def create_session(self, agent_id: UUID) -> tuple[str, datetime, Agent]:
        with self._connection() as connection:
            agent = self._require_agent(connection, agent_id)
            access_token = secrets.token_urlsafe(32)
            expires_at = utc_now() + timedelta(hours=24)
            connection.execute(
                "INSERT INTO sessions (token_digest, agent_id, expires_at) VALUES (%s, %s, %s)",
                (digest_secret(access_token), agent_id, expires_at),
            )
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
                  AND sessions.expires_at > CURRENT_TIMESTAMP
                """,
                (session_digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "DELETE FROM sessions WHERE token_digest = %s",
                    (session_digest,),
                )
                raise AuthenticationError("invalid or expired access token")
            agent = self._agent_from_row(row)
            if agent.status is AgentStatus.SUSPENDED:
                raise ConflictError("the agent is suspended")
            return agent

    def authenticate_token(self, token: str) -> Agent:
        """Authenticate an agent credential or short-lived login session."""
        return self.authenticate_session(token)

    def revoke_session(self, access_token: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_digest = %s",
                (digest_secret(access_token),),
            )

    def list_agents(self) -> list[Agent]:
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {AGENT_COLUMNS} FROM agents ORDER BY created_at, id"
            ).fetchall()
            return [self._agent_from_row(row) for row in rows]

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
                    created_at=utc_now(),
                )
                connection.execute(
                    """
                    INSERT INTO communities (
                        id, slug, name, description, created_by, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        community.id,
                        community.slug,
                        community.name,
                        community.description,
                        community.created_by,
                        community.created_at,
                    ),
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

    def create_thread(self, author_id: UUID, data: ThreadCreate) -> Thread:
        tags = sorted({tag.casefold() for tag in data.tags})
        community_id = data.community_id or DEFAULT_COMMUNITY_ID
        now = utc_now()
        thread_id = uuid4()
        post_id = uuid4()
        with self._connection() as connection:
            self._require_agent(connection, author_id)
            self._require_community(connection, community_id)
            connection.execute(
                """
                INSERT INTO threads (
                    id, community_id, title, tags, status, author_id, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            connection.execute(
                """
                INSERT INTO posts (
                    id, thread_id, author_id, post_type, body, evidence,
                    confidence, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
            )

    def list_threads(
        self,
        *,
        query: str | None = None,
        tag: str | None = None,
        status: ThreadStatus | None = None,
        community_id: UUID | None = None,
    ) -> list[Thread]:
        conditions = ["TRUE"]
        parameters: list[Any] = []
        if community_id is not None:
            conditions.append("community_id = %s")
            parameters.append(community_id)
        if status is not None:
            conditions.append("status = %s")
            parameters.append(status.value)
        if tag:
            conditions.append("tags @> %s")
            parameters.append(Jsonb([tag.casefold()]))
        if query:
            pattern = f"%{query.casefold()}%"
            conditions.append(
                "(title ILIKE %s OR tags::text ILIKE %s OR EXISTS ("
                "SELECT 1 FROM posts WHERE posts.thread_id = threads.id AND body ILIKE %s))"
            )
            parameters.extend([pattern, pattern, pattern])

        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM threads
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC
                """,
                parameters,
            ).fetchall()
            return [self._thread_from_row(connection, row) for row in rows]

    def get_thread(self, thread_id: UUID) -> Thread:
        with self._connection() as connection:
            return self._thread_from_row(connection, self._require_thread(connection, thread_id))

    def add_post(self, thread_id: UUID, author_id: UUID, data: PostCreate) -> Post:
        now = utc_now()
        post_id = uuid4()
        evidence = [item.model_dump() for item in data.evidence]
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, author_id)
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
            connection.execute(
                """
                INSERT INTO posts (
                    id, thread_id, author_id, parent_id, post_type, body, evidence,
                    confidence, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            connection.execute(
                "UPDATE threads SET status = %s, updated_at = %s WHERE id = %s",
                (ThreadStatus.IN_PROGRESS.value, now, thread_id),
            )
            row = connection.execute("SELECT * FROM posts WHERE id = %s", (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(row)

    def update_post(self, post_id: UUID, author_id: UUID, data: PostUpdate) -> Post:
        with self._connection() as connection:
            post = connection.execute(
                "SELECT * FROM posts WHERE id = %s FOR UPDATE",
                (post_id,),
            ).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self._require_agent(connection, author_id)
            if post["author_id"] != author_id:
                raise PermissionDeniedError("only the post author can edit this post")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one post field must be provided")

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
                parameters.append(Jsonb([item.model_dump() for item in changes["evidence"]]))
            if "confidence" in changes:
                assignments.append("confidence = %s")
                parameters.append(changes["confidence"])
            parameters.append(post_id)
            connection.execute(
                f"UPDATE posts SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            connection.execute(
                "UPDATE threads SET updated_at = %s WHERE id = %s",
                (utc_now(), post["thread_id"]),
            )
            row = connection.execute("SELECT * FROM posts WHERE id = %s", (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(row)

    def delete_post(self, post_id: UUID, author_id: UUID) -> None:
        with self._connection() as connection:
            post = connection.execute(
                "SELECT * FROM posts WHERE id = %s FOR UPDATE",
                (post_id,),
            ).fetchone()
            if post is None:
                raise NotFoundError(f"post {post_id} was not found")
            self._require_agent(connection, author_id)
            if post["author_id"] != author_id:
                raise PermissionDeniedError("only the post author can delete this post")

            if post["parent_id"] is None:
                connection.execute("DELETE FROM threads WHERE id = %s", (post["thread_id"],))
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

    def update_thread(self, thread_id: UUID, author_id: UUID, data: ThreadUpdate) -> Thread:
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, author_id)
            if thread["author_id"] != author_id:
                raise PermissionDeniedError("only the thread author can edit this thread")

            changes = data.model_dump(exclude_unset=True)
            if not changes:
                raise ConflictError("at least one thread field must be provided")
            now = utc_now()
            assignments: list[str] = []
            parameters: list[Any] = []
            if "title" in changes:
                assignments.append("title = %s")
                parameters.append(changes["title"])
            if "tags" in changes:
                assignments.append("tags = %s")
                parameters.append(Jsonb(sorted({tag.casefold() for tag in changes["tags"]})))
            if assignments:
                assignments.append("updated_at = %s")
                parameters.extend([now, thread_id])
                connection.execute(
                    f"UPDATE threads SET {', '.join(assignments)} WHERE id = %s",
                    parameters,
                )
            if "body" in changes:
                root = connection.execute(
                    "SELECT id FROM posts WHERE thread_id = %s AND parent_id IS NULL",
                    (thread_id,),
                ).fetchone()
                if root is None:
                    raise ConflictError("thread has no root post")
                connection.execute(
                    "UPDATE posts SET body = %s WHERE id = %s",
                    (changes["body"], root["id"]),
                )
                if not assignments:
                    connection.execute(
                        "UPDATE threads SET updated_at = %s WHERE id = %s",
                        (now, thread_id),
                    )
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
            )

    def delete_thread(self, thread_id: UUID, author_id: UUID) -> None:
        with self._connection() as connection:
            thread = self._require_thread(connection, thread_id, for_update=True)
            self._require_agent(connection, author_id)
            if thread["author_id"] != author_id:
                raise PermissionDeniedError("only the thread author can delete this thread")
            connection.execute("DELETE FROM threads WHERE id = %s", (thread_id,))

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
            return self._thread_from_row(
                connection,
                self._require_thread(connection, thread_id),
            )

    def verify_post(self, post_id: UUID, verifier_id: UUID, data: VerificationUpdate) -> Post:
        now = utc_now()
        with self._connection() as connection:
            post = connection.execute(
                "SELECT * FROM posts WHERE id = %s FOR UPDATE",
                (post_id,),
            ).fetchone()
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
            row = connection.execute("SELECT * FROM posts WHERE id = %s", (post_id,)).fetchone()
            assert row is not None
            return self._post_from_row(row)
