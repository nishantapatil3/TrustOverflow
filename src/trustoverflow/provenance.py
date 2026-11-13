"""Canonical payloads and Ed25519 message provenance."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .models import EvidenceRef, PostType, Provenance


class ProvenanceError(ValueError):
    """Raised when an identity key or message signature is invalid."""


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def thread_payload(
    *,
    title: str,
    body: str,
    tags: list[str],
    community_id: UUID | None,
) -> dict[str, Any]:
    return {
        "kind": "thread",
        "title": title,
        "body": body,
        "tags": sorted({tag.casefold() for tag in tags}),
        "community_id": str(community_id) if community_id is not None else None,
    }


def post_payload(
    *,
    thread_id: UUID,
    parent_id: UUID | None,
    post_type: PostType,
    body: str,
    evidence: list[EvidenceRef],
    confidence: float,
) -> dict[str, Any]:
    return {
        "kind": "post",
        "thread_id": str(thread_id),
        "parent_id": str(parent_id) if parent_id is not None else None,
        "type": post_type.value,
        "body": body,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "confidence": confidence,
    }


def _public_key(value: str) -> Ed25519PublicKey:
    try:
        if value.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(value.encode("utf-8"))
            if not isinstance(key, Ed25519PublicKey):
                raise ProvenanceError("public key must be Ed25519")
            return key
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as error:
        raise ProvenanceError("public key must be a PEM or base64 Ed25519 key") from error


def validate_public_key(value: str | None) -> None:
    if value:
        _public_key(value)


def build_provenance(
    *,
    signer_id: UUID,
    public_key: str | None,
    signature: str | None,
    payload: dict[str, Any],
) -> Provenance:
    digest = payload_hash(payload)
    if signature is None:
        return Provenance(
            signer_id=signer_id,
            payload_hash=digest,
            signature=None,
            verified=False,
        )
    if not public_key:
        raise ProvenanceError("a public key is required for signed messages")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
        _public_key(public_key).verify(signature_bytes, canonical_bytes(payload))
    except (ValueError, TypeError, InvalidSignature) as error:
        raise ProvenanceError("message signature could not be verified") from error
    return Provenance(
        signer_id=signer_id,
        payload_hash=digest,
        signature=signature,
        verified=True,
    )
