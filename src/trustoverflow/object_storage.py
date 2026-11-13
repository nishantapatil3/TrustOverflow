"""Object storage adapters for evidence bundles and run manifests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from .models import EvidenceObject


class ObjectStorageError(Exception):
    """Raised when an evidence object cannot be stored."""


class ObjectStorage(Protocol):
    def put(self, data: bytes, *, content_type: str) -> EvidenceObject: ...


def _object_key(data: bytes) -> str:
    return f"evidence/{hashlib.sha256(data).hexdigest()}"


class LocalObjectStorage:
    """Filesystem-backed object storage for local development and tests."""

    def __init__(self, root: str | Path, bucket: str = "local") -> None:
        self._root = Path(root)
        self._bucket = bucket
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, *, content_type: str) -> EvidenceObject:
        key = _object_key(data)
        destination = self._root / key
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                destination.write_bytes(data)
        except OSError as error:
            raise ObjectStorageError("evidence object could not be stored") from error
        return EvidenceObject(
            uri=f"object://{self._bucket}/{key}",
            checksum=key.rsplit("/", 1)[-1],
            size=len(data),
            content_type=content_type,
        )


class S3ObjectStorage:
    """S3-compatible object storage adapter."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region_name: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as error:
            raise ObjectStorageError(
                "boto3 is required when EVIDENCE_STORAGE_DRIVER=s3"
            ) from error
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
        )

    def put(self, data: bytes, *, content_type: str) -> EvidenceObject:
        key = _object_key(data)
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except Exception as error:
            raise ObjectStorageError("evidence object could not be stored") from error
        return EvidenceObject(
            uri=f"s3://{self._bucket}/{key}",
            checksum=key.rsplit("/", 1)[-1],
            size=len(data),
            content_type=content_type,
        )


def build_object_storage() -> ObjectStorage:
    driver = os.getenv("EVIDENCE_STORAGE_DRIVER", "local").casefold()
    if driver == "local":
        return LocalObjectStorage(
            os.getenv("EVIDENCE_STORAGE_PATH", "/tmp/trustoverflow-evidence")
        )
    if driver == "s3":
        bucket = os.getenv("EVIDENCE_S3_BUCKET")
        if not bucket:
            raise ObjectStorageError("EVIDENCE_S3_BUCKET is required for S3 storage")
        return S3ObjectStorage(
            bucket,
            endpoint_url=os.getenv("EVIDENCE_S3_ENDPOINT_URL") or None,
            region_name=os.getenv("AWS_REGION") or None,
        )
    raise ObjectStorageError(f"unsupported evidence storage driver: {driver}")
