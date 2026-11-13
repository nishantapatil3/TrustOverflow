from __future__ import annotations

from fastapi.testclient import TestClient

from trustoverflow.api import create_app
from trustoverflow.object_storage import LocalObjectStorage


def test_local_object_storage_deduplicates_by_checksum(tmp_path) -> None:
    storage = LocalObjectStorage(tmp_path)

    first = storage.put(b"receipt", content_type="text/plain")
    second = storage.put(b"receipt", content_type="text/plain")

    assert first == second
    assert (tmp_path / "evidence" / first.checksum).read_bytes() == b"receipt"


def test_authenticated_agent_can_upload_evidence(tmp_path) -> None:
    client = TestClient(create_app(object_storage=LocalObjectStorage(tmp_path)))
    login = client.post(
        "/auth/login",
        json={"email": "evidence-owner@example.com", "password": "correct-horse-battery-staple"},
    )
    assert login.status_code == 200

    response = client.post(
        "/evidence",
        content=b"reproduction receipt",
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 201
    assert response.json()["content_type"] == "text/plain"
    assert response.json()["size"] == len(b"reproduction receipt")
