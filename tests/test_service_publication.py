from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import (
    AIDeploymentResultCreate,
    AIHealthCheckResult,
    AIInferenceTestResult,
    AISessionCreate,
    DeploymentCreate,
    DeploymentParameters,
)
from uniform_dgxspark.services.publication import (
    PublicationRequestTooLarge,
    ServicePublicationManager,
)
from uniform_dgxspark.state import DeploymentStatus


def _running_deployment(client: TestClient) -> str:
    deployment = client.app.state.store.create(
        DeploymentCreate(
            model_id="org/test-model",
            display_name="Test model",
            parameters=DeploymentParameters(port=19080),
        )
    )
    for target_status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        client.app.state.store.transition(deployment.id, target_status, message="test")
    return deployment.id


def _ai_service(client: TestClient, tmp_path: Path) -> str:
    workspace = tmp_path / "ai-service"
    workspace.mkdir()
    session = client.app.state.ai_store.create(
        AISessionCreate(
            provider="codex",
            task_type="deployment",
            display_name="AI model",
        ),
        str(workspace),
    )
    client.app.state.ai_store.upsert_result(
        session.id,
        AIDeploymentResultCreate(
            engine="sglang",
            container_name="ai-model-container",
            image="example/sglang:test",
            port=19081,
            service_url="http://127.0.0.1:19081",
            launch_command="docker run example/sglang:test",
            health_check=AIHealthCheckResult(
                method="GET",
                url="http://127.0.0.1:19081/health",
                status_code=200,
                passed=True,
            ),
            inference_test=AIInferenceTestResult(
                method="GET",
                url="http://127.0.0.1:19081/v1/models",
                status_code=200,
                passed=True,
            ),
        ),
    )
    return session.id


def test_publication_api_supports_managed_and_ai_services_without_storing_plaintext(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            publication_advertise_host="spark.lan",
            publication_control_hosts=("testserver",),
        )
    )
    with TestClient(app) as client:
        manager: ServicePublicationManager = client.app.state.publication_manager
        manager.ensure_gateway_started = lambda: None  # type: ignore[method-assign]
        manager.stop_gateway_if_idle = lambda: None  # type: ignore[method-assign]
        manager._port_is_loopback_only = lambda _name, _port: True  # type: ignore[method-assign]
        client.app.state.runtime.is_running = lambda _target_id: True
        client.app.state.runtime.is_named_container_running = lambda _name: True
        client.app.state.runtime.health = lambda _port, timeout=3.0: True

        deployment_id = _running_deployment(client)
        ai_session_id = _ai_service(client, tmp_path)

        unpublished = client.get(
            f"/api/v1/service-publications/deployment/{deployment_id}"
        )
        assert unpublished.status_code == 200
        assert unpublished.json()["enabled"] is False
        assert unpublished.json()["publication_id"] is None
        assert unpublished.json()["base_url"] is None

        created = client.post(
            f"/api/v1/service-publications/deployment/{deployment_id}",
            headers={"X-Uniform-Action": "service-publication"},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        first = created.json()
        assert first["enabled"] is True
        assert first["base_url"].startswith("http://spark.lan:18181/")
        assert first["base_url"].endswith("/v1")
        first_key = first.pop("api_key")
        assert first_key.startswith("udgs_")

        status = client.get(
            f"/api/v1/service-publications/deployment/{deployment_id}"
        )
        assert status.status_code == 200
        assert "api_key" not in status.json()
        assert status.json()["key_hint"] != first_key
        assert status.json()["publication_id"] == first["publication_id"]
        assert client.post(
            f"/api/v1/service-publications/deployment/{deployment_id}",
            headers={"X-Uniform-Action": "service-publication"},
        ).status_code == 409

        rotated = client.post(
            f"/api/v1/service-publications/deployment/{deployment_id}/rotate-key",
            headers={"X-Uniform-Action": "service-publication"},
        )
        assert rotated.status_code == 200
        assert rotated.headers["cache-control"] == "no-store"
        second_key = rotated.json()["api_key"]
        assert second_key != first_key
        publication = manager.store.get_for_target("deployment", deployment_id)
        assert publication is not None
        assert manager.verify_key(publication, first_key) is False
        assert manager.verify_key(publication, second_key) is True

        database_bytes = (tmp_path / "uniform.db").read_bytes()
        assert first_key.encode() not in database_bytes
        assert second_key.encode() not in database_bytes

        revoked = client.delete(
            f"/api/v1/service-publications/deployment/{deployment_id}",
            headers={"X-Uniform-Action": "service-publication"},
        )
        assert revoked.status_code == 204
        disabled = client.get(
            f"/api/v1/service-publications/deployment/{deployment_id}"
        ).json()
        assert disabled["enabled"] is False
        assert disabled["base_url"] is None
        assert disabled["key_hint"] is None

        ai_created = client.post(
            f"/api/v1/service-publications/ai_session/{ai_session_id}",
            headers={"X-Uniform-Action": "service-publication"},
        )
        assert ai_created.status_code == 201
        assert ai_created.json()["target_type"] == "ai_session"
        assert ai_created.json()["display_name"] == "AI model"


class _StreamingUpstream:
    status_code = 200
    headers = {
        "Content-Type": "text/event-stream",
        "X-Request-ID": "upstream-request",
        "Connection": "should-not-be-forwarded",
    }

    def __init__(self) -> None:
        self._chunks = [b"data: first\n\n", b"data: second\n\n", b""]
        self.closed = False

    async def aiter_raw(self):  # noqa: ANN201
        while chunk := self._chunks.pop(0):
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncClient:
    def __init__(self, captured: dict[str, object], upstream: _StreamingUpstream) -> None:
        self.captured = captured
        self.upstream = upstream
        self.closed = False

    def build_request(self, method, url, *, content, headers):  # noqa: ANN001, ANN201
        self.captured.update(method=method, url=url, body=content, headers=dict(headers))
        return object()

    async def send(self, _request, *, stream):  # noqa: ANN001, ANN201
        assert stream is True
        return self.upstream

    async def aclose(self) -> None:
        self.closed = True


def test_publication_gateway_authenticates_and_streams_without_forwarding_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(app) as control_client:
        manager: ServicePublicationManager = control_client.app.state.publication_manager
        control_client.app.state.runtime.is_running = lambda _target_id: True
        deployment_id = _running_deployment(control_client)
        api_key = "udgs_test-secret-that-must-not-reach-upstream"
        publication = manager.store.enable(
            "deployment",
            deployment_id,
            api_key_hash=hashlib.sha256(api_key.encode()).hexdigest(),
            key_hint="udgs_test…ream",
        )
        captured: dict[str, object] = {}
        upstream = _StreamingUpstream()

        monkeypatch.setattr(
            "uniform_dgxspark.services.publication.httpx.AsyncClient",
            lambda **_kwargs: _FakeAsyncClient(captured, upstream),
        )
        manager.runtime.is_running = lambda _target_id: True

        with TestClient(manager.gateway_app) as gateway_client:
            path = f"/{publication.publication_id}/v1/chat/completions"
            assert gateway_client.post(path, json={"stream": True}).status_code == 401
            assert (
                gateway_client.post(
                    path,
                    json={"stream": True},
                    headers={"Authorization": "Bearer wrong"},
                ).status_code
                == 401
            )
            forbidden = gateway_client.post(
                f"/{publication.publication_id}/metrics",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert forbidden.status_code == 404
            response = gateway_client.post(
                path,
                content=json.dumps({"stream": True}),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 200
        assert response.content == b"data: first\n\ndata: second\n\n"
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-request-id"] == "upstream-request"
        assert "connection" not in response.headers
        assert captured["url"] == "http://127.0.0.1:19080/v1/chat/completions"
        assert json.loads(captured["body"])["stream_options"]["include_usage"] is True
        forwarded_headers = {
            str(name).lower(): value
            for name, value in dict(captured["headers"]).items()
        }
        assert "authorization" not in forwarded_headers
        assert "x-api-key" not in forwarded_headers
        assert forwarded_headers["accept-encoding"] == "identity"
        assert upstream.closed is True


def test_publication_rejects_exposed_ai_port_and_untrusted_control_request(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            publication_control_hosts=("testserver",),
        )
    )
    with TestClient(app) as client:
        manager: ServicePublicationManager = client.app.state.publication_manager
        manager.ensure_gateway_started = lambda: None  # type: ignore[method-assign]
        client.app.state.runtime.is_named_container_running = lambda _name: True
        client.app.state.runtime.health = lambda _port, timeout=3.0: True
        manager._port_is_loopback_only = lambda _name, _port: False  # type: ignore[method-assign]
        session_id = _ai_service(client, tmp_path)
        path = f"/api/v1/service-publications/ai_session/{session_id}"

        assert client.post(path).status_code == 403
        assert client.post(
            path,
            headers={
                "Host": "attacker.invalid",
                "X-Uniform-Action": "service-publication",
            },
        ).status_code == 421
        exposed = client.post(
            path,
            headers={"X-Uniform-Action": "service-publication"},
        )
        assert exposed.status_code == 409
        assert "loopback" in exposed.json()["detail"]


def test_publication_request_body_is_bounded_before_joining() -> None:
    class _ChunkedRequest:
        async def stream(self):  # noqa: ANN201
            yield b"1234"
            yield b"5678"

    original_limit = ServicePublicationManager._MAX_REQUEST_BYTES
    ServicePublicationManager._MAX_REQUEST_BYTES = 5
    try:
        with pytest.raises(PublicationRequestTooLarge):
            asyncio.run(
                ServicePublicationManager._read_bounded_body(_ChunkedRequest())  # type: ignore[arg-type]
            )
    finally:
        ServicePublicationManager._MAX_REQUEST_BYTES = original_limit
