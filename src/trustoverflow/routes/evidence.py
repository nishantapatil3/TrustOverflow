from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..models import Agent, EvidenceObject
from ..object_storage import ObjectStorageError
from .context import RouteContext, RouteDependencies


def build_router(context: RouteContext, dependencies: RouteDependencies) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/evidence",
        response_model=EvidenceObject,
        status_code=status.HTTP_201_CREATED,
        tags=["evidence"],
    )
    async def upload_evidence(
        request: Request,
        agent: Agent = Depends(dependencies.current_agent),
    ) -> EvidenceObject:
        max_bytes = int(os.getenv("EVIDENCE_MAX_BYTES", str(10 * 1024 * 1024)))
        payload = await request.body()
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail="evidence object is too large")
        dependencies.enforce_quota(agent, "evidence")
        content_type = request.headers.get("content-type", "application/octet-stream")
        content_type = content_type.split(";", 1)[0].strip() or "application/octet-stream"
        try:
            stored = context.evidence_storage.put(payload, content_type=content_type)
            context.exchange.record_audit(
                agent.id,
                "evidence.stored",
                "evidence",
                metadata={"uri": stored.uri, "size": stored.size},
            )
            return stored
        except ObjectStorageError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    return router
