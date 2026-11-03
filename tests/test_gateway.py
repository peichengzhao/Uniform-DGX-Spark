from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentCreate, DeploymentParameters
from uniform_dgxspark.state import DeploymentStatus

ACTION = {"X-Uniform-Action": "gateway-config"}


def _running_deployment(
    client: TestClient,
    *,
    model_id: str = "org/test-model",
    port: int = 19080,
) -> str:
    deployment = client.app.state.store.create(
        DeploymentCreate(
            model_id=model_id,
            display_name=model_id.rsplit("/", 1)[-1],
            parameters=DeploymentParameters(port=port),
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


class _JsonUpstream:
    status_code = 200
    headers = {"Content-Type": "application/json", "X-Request-ID": "gateway-request"}

    def __init__(self, payload: dict[str, object]) -> None:
        self._chunks = [json.dumps(payload).encode(), b""]
        self.closed = False

    async def aiter_raw(self):  # noqa: ANN201
        while chunk := self._chunks.pop(0):
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _FakeAsyncClient:
    def __init__(self, captured: dict[str, object], upstream: _JsonUpstream) -> None:
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


def _app(tmp_path: Path):
    return create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            publication_control_hosts=("testserver",),
        )
    )


def test_gateway_api_issues_independent_keys_and_disables_legacy_key(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        runtime.is_running = lambda _target_id: True
        deployment_id = _running_deployment(client)
        legacy_key = "udgs_legacy-shared-key"
        publication = client.app.state.publication_store.enable(
            "deployment",
            deployment_id,
            api_key_hash=hashlib.sha256(legacy_key.encode()).hexdigest(),
            key_hint="udgs_legacy…-key",
        )

        models = client.get("/api/v1/gateway/models")
        assert models.status_code == 200
        assert models.json()[0]["publication_id"] == publication.publication_id

        caller = client.post(
            "/api/v1/gateway/callers",
            json={"name": "算法组", "note": "独立 Key 测试"},
            headers=ACTION,
        )
        assert caller.status_code == 201
        consumer_id = caller.json()["consumer_id"]

        created = client.post(
            f"/api/v1/gateway/callers/{consumer_id}/tokens",
            json={
                "name": "同事笔记本",
                "publication_ids": [publication.publication_id],
                "token_limit": 1000,
            },
            headers=ACTION,
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        token = created.json()
        api_key = token["api_key"]
        assert api_key.startswith("udgs_")
        assert client.post(
            "/api/v1/gateway/callers", json={"name": "without action"}
        ).status_code == 403

        callers = client.get("/api/v1/gateway/callers").json()
        assert callers["total"] == 1
        assert callers["items"][0]["tokens"][0]["key_hint"] != api_key
        assert "api_key" not in callers["items"][0]["tokens"][0]
        database_bytes = (tmp_path / "uniform.db").read_bytes()
        assert api_key.encode() not in database_bytes

        disabled_legacy = client.delete(
            f"/api/v1/gateway/models/{publication.publication_id}/legacy-key",
            headers=ACTION,
        )
        assert disabled_legacy.status_code == 204
        publication = client.app.state.publication_store.get_enabled_by_id(
            publication.publication_id
        )
        assert publication is not None
        assert publication.api_key_hash == ""
        assert publication.key_hint == ""


def test_gateway_key_scope_revocation_and_usage_filters(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        runtime = client.app.state.runtime
        runtime.is_running = lambda _target_id: True
        deployment_id = _running_deployment(client)
        other_deployment_id = _running_deployment(
            client, model_id="org/other-model", port=19081
        )
        publication = client.app.state.publication_store.enable(
            "deployment",
            deployment_id,
            api_key_hash=hashlib.sha256(b"legacy").hexdigest(),
            key_hint="legacy",
        )
        other = client.app.state.publication_store.enable(
            "deployment",
            other_deployment_id,
            api_key_hash=hashlib.sha256(b"other-legacy").hexdigest(),
            key_hint="other",
        )
        consumer_id = client.post(
            "/api/v1/gateway/callers",
            json={"name": "业务系统"},
            headers=ACTION,
        ).json()["consumer_id"]
        first = client.post(
            f"/api/v1/gateway/callers/{consumer_id}/tokens",
            json={"name": "主 Key", "publication_ids": [publication.publication_id]},
            headers=ACTION,
        ).json()
        second = client.post(
            f"/api/v1/gateway/callers/{consumer_id}/tokens",
            json={"name": "备用 Key", "publication_ids": [publication.publication_id]},
            headers=ACTION,
        ).json()

        captured: dict[str, object] = {}
        payload = {
            "id": "completion-1",
            "model": "test-model",
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 7,
                "total_tokens": 12,
            },
        }
        monkeypatch.setattr(
            "uniform_dgxspark.services.publication.httpx.AsyncClient",
            lambda **_kwargs: _FakeAsyncClient(captured, _JsonUpstream(payload)),
        )

        manager = client.app.state.publication_manager
        with TestClient(manager.gateway_app) as gateway:
            path = f"/{publication.publication_id}/v1/chat/completions"
            response = gateway.post(
                path,
                json={"model": "test-model", "messages": []},
                headers={"Authorization": f"Bearer {first['api_key']}"},
            )
            assert response.status_code == 200
            assert response.json()["id"] == "completion-1"

            wrong_scope = gateway.post(
                f"/{other.publication_id}/v1/chat/completions",
                json={"model": "other-model", "messages": []},
                headers={"Authorization": f"Bearer {first['api_key']}"},
            )
            assert wrong_scope.status_code == 403

            revoked = client.delete(
                f"/api/v1/gateway/tokens/{first['token_id']}", headers=ACTION
            )
            assert revoked.status_code == 204
            denied = gateway.post(
                path,
                json={"model": "test-model", "messages": []},
                headers={"Authorization": f"Bearer {first['api_key']}"},
            )
            assert denied.status_code == 401
            still_allowed = gateway.post(
                path,
                json={"model": "test-model", "messages": []},
                headers={"Authorization": f"Bearer {second['api_key']}"},
            )
            assert still_allowed.status_code == 200

        limited = client.post(
            f"/api/v1/gateway/callers/{consumer_id}/tokens",
            json={
                "name": "限额 Key",
                "publication_ids": [publication.publication_id],
                "token_limit": 12,
            },
            headers=ACTION,
        ).json()
        with TestClient(manager.gateway_app) as gateway:
            path = f"/{publication.publication_id}/v1/chat/completions"
            assert gateway.post(
                path,
                json={"model": "test-model", "messages": []},
                headers={"Authorization": f"Bearer {limited['api_key']}"},
            ).status_code == 200
            exhausted = gateway.post(
                path,
                json={"model": "test-model", "messages": []},
                headers={"Authorization": f"Bearer {limited['api_key']}"},
            )
            assert exhausted.status_code == 429
            assert "quota" in exhausted.json()["error"]["message"]

        usage = client.get(
            "/api/v1/gateway/usage",
            params={"consumer_id": consumer_id, "publication_id": publication.publication_id},
        )
        assert usage.status_code == 200
        payload = usage.json()
        assert payload["summary"]["requests"] == 3
        assert payload["summary"]["total_tokens"] == 36
        assert {item["token_name"] for item in payload["items"]} == {
            "主 Key",
            "备用 Key",
            "限额 Key",
        }

        token_usage = client.get(
            "/api/v1/gateway/usage", params={"token_id": second["token_id"]}
        ).json()
        assert token_usage["summary"]["requests"] == 1
        assert token_usage["items"][0]["model"] == "test-model"

        limited_usage = client.get(
            "/api/v1/gateway/usage", params={"token_id": limited["token_id"]}
        ).json()
        assert limited_usage["summary"]["requests"] == 1
        assert limited_usage["summary"]["total_tokens"] == 12


def test_gateway_page_exposes_models_callers_and_usage_center(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        html = client.get("/").text
        assert 'data-view="gateway"' in html
        assert 'id="view-gateway"' in html
        assert "网关配置" in html
        assert 'id="gateway-caller-form"' in html
        assert 'id="gateway-token-form"' in html
        assert 'id="gateway-usage-model"' in html
        assert 'id="gateway-usage-caller"' in html
        assert 'id="gateway-usage-token"' in html
