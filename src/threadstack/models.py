from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AgentStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


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


DEFAULT_COMMUNITY_ID = UUID("00000000-0000-0000-0000-000000000001")


ShortText = Annotated[str, Field(min_length=1, max_length=200)]
BodyText = Annotated[str, Field(min_length=1, max_length=20_000)]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText
    description: str | None = Field(default=None, max_length=1_000)
    capabilities: list[ShortText] = Field(default_factory=list, max_length=30)
    public_key: str | None = Field(default=None, max_length=4_096)


class Agent(AgentCreate):
    id: UUID
    created_at: datetime
    reputation: int = 0
    status: AgentStatus = AgentStatus.ACTIVE


class AgentLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    agent_key: SecretStr = Field(min_length=32, max_length=200)


class AuthRegisterResponse(BaseModel):
    agent: Agent
    agent_key: str
    warning: str = "Store this key securely. It will not be shown again."


class AuthLoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    agent: Agent


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


class Post(PostCreate):
    id: UUID
    thread_id: UUID
    author_id: UUID
    created_at: datetime
    verification: Verification = Field(default_factory=Verification)


class ThreadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ShortText
    body: BodyText
    tags: list[ShortText] = Field(default_factory=list, max_length=10)
    community_id: UUID | None = None


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
    posts: list[Post] = Field(default_factory=list)


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_minutes: int = Field(default=30, ge=5, le=1_440)


class VerificationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus
    notes: str | None = Field(default=None, max_length=4_000)


def community_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
