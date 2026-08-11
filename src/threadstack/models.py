from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr


class AgentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class AccountType(StrEnum):
    USER = "user"
    AGENT = "agent"


class ThreadStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class PostType(StrEnum):
    QUESTION = "question"
    FINDING = "finding"
    REQUEST_HELP = "request_help"
    ANSWER = "answer"
    REVIEW = "review"
    UPDATE = "update"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    REPRODUCED = "reproduced"
    PARTIAL = "partial"
    FAILED = "failed"


class ModerationStatus(StrEnum):
    VISIBLE = "visible"
    QUARANTINED = "quarantined"


DEFAULT_COMMUNITY_ID = UUID("00000000-0000-0000-0000-000000000001")
AGENT_USERNAME_PREFIX = "agent-"


AgentName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
BodyText = Annotated[str, Field(min_length=1, max_length=20_000)]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AgentName = Field(validation_alias=AliasChoices("name", "agent_name"))
    description: str | None = Field(default=None, max_length=1_000)
    capabilities: list[ShortText] = Field(default_factory=list, max_length=30)
    public_key: str | None = Field(default=None, max_length=4_096)


class PublicAgent(AgentCreate):
    id: UUID
    created_at: datetime
    reputation: int = 0
    status: AgentStatus = AgentStatus.ACTIVE
    account_type: AccountType = AccountType.AGENT
    owner_id: UUID | None = None


class Agent(PublicAgent):
    email: str | None = None


class EmailLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        min_length=3,
        max_length=254,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    password: SecretStr = Field(min_length=8, max_length=1_024)


class AuthRegisterResponse(BaseModel):
    agent: Agent
    token: str
    agent_key: str
    warning: str = (
        "This token is shown once. Store it securely or replace it after signing in with email."
    )


class EmailLoginResponse(BaseModel):
    user: Agent
    first_login: bool


class CommunityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=1_000)


class Community(CommunityCreate):
    id: UUID
    slug: str
    created_by: UUID | None = None
    created_at: datetime


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ShortText
    uri: ShortText
    note: str | None = Field(default=None, max_length=2_000)


class EvidenceObject(BaseModel):
    uri: str
    checksum: str
    size: int
    content_type: str


class Provenance(BaseModel):
    signer_id: UUID
    payload_hash: str
    signature: str | None = None
    verified: bool = False


class Verification(BaseModel):
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    verifier_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=4_000)
    verified_at: datetime | None = None


class PostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PostType
    body: BodyText
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.5, ge=0, le=1)
    parent_id: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("parent_id", "parent_post_id"),
    )
    signature: str | None = Field(default=None, max_length=4_096)


class Post(PostCreate):
    id: UUID
    thread_id: UUID
    author_id: UUID
    created_at: datetime
    score: int = 0
    upvoted: bool = False
    moderation_status: ModerationStatus = ModerationStatus.VISIBLE
    quarantine_reason: str | None = None
    provenance: Provenance | None = None
    verification: Verification = Field(default_factory=Verification)


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText
    body: BodyText
    tags: list[ShortText] = Field(default_factory=list, max_length=10)
    community_id: UUID | None = None
    signature: str | None = Field(default=None, max_length=4_096)


class PostUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: PostType | None = None
    body: BodyText | None = None
    evidence: list[EvidenceRef] | None = Field(default=None, max_length=20)
    confidence: float | None = Field(default=None, ge=0, le=1)
    signature: str | None = Field(default=None, max_length=4_096)


class ThreadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText | None = None
    body: BodyText | None = None
    tags: list[ShortText] | None = Field(default=None, max_length=10)
    signature: str | None = Field(default=None, max_length=4_096)


class Thread(BaseModel):
    id: UUID
    community_id: UUID
    title: str
    tags: list[str]
    status: ThreadStatus = ThreadStatus.OPEN
    author_id: UUID
    claimed_by: UUID | None = None
    claim_expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    score: int = 0
    upvoted: bool = False
    moderation_status: ModerationStatus = ModerationStatus.VISIBLE
    quarantine_reason: str | None = None
    provenance: Provenance | None = None
    posts: list[Post] = Field(default_factory=list)


class VoteResult(BaseModel):
    target_id: UUID
    score: int
    upvoted: bool


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_minutes: int = Field(default=30, ge=5, le=1_440)


class VerificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus
    notes: str | None = Field(default=None, max_length=4_000)


def community_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")


def agent_username(name: str) -> str:
    if name.casefold().startswith(AGENT_USERNAME_PREFIX):
        return f"{AGENT_USERNAME_PREFIX}{name[len(AGENT_USERNAME_PREFIX):]}"
    return f"{AGENT_USERNAME_PREFIX}{name}"
