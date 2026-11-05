from __future__ import annotations

import json
import queue
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentCreate, DeploymentParameters
from uniform_dgxspark.services import external_access
from uniform_dgxspark.services.external_access import (
    ExternalAccessStartError,
    ServiceExternalAccessManager,
)
from uniform_dgxspark.state import DeploymentStatus


class _FakeStdout:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        for line in lines or []:
            self._lines.put(line)
        self.closed = False

    def readline(self) -> bytes:
        line = self._lines.get()
        return b"" if line is None else line

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._lines.put(None)


class _FakeProcess:
    def __init__(
        self,
        command: list[str],
        *,
        output: bytes | None = None,
        exit_code: int | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> None:
        self.args = command
        self.kwargs = kwargs or {}
        self.stdout = _FakeStdout([output] if output else [])
        self.returncode = exit_code
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = 0
        self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.terminate()


def _fake_popen_factory(
    created: list[_FakeProcess],
    *,
    output: bytes | None = None,
    exit_code: int | None = None,
):
    def factory(command, **kwargs):  # noqa: ANN001, ANN202
        process = _FakeProcess(
            command, output=output, exit_code=exit_code, kwargs=kwargs
        )
        created.append(process)
        return process

    return factory


def _manager(tmp_path: Path, *, timeout: float = 1.0) -> ServiceExternalAccessManager:
    binary = tmp_path / "cloudflared"
    binary.touch()
    return ServiceExternalAccessManager(
        tmp_path / "service-external-access.json",
        cloudflared_binary=str(binary),
        start_timeout_seconds=timeout,
    )


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
        client.app.state.store.transition(
            deployment.id,
            target_status,
            message="test",
        )
    return deployment.id


def test_parses_quick_tunnel_url_and_builds_publication_url(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    line = (
        "\x1b[32mINF\x1b[0m Requesting new quick Tunnel on https://uniform-test.trycloudflare.com"
    )

    assert manager._extract_public_host(line) == ("https://uniform-test.trycloudflare.com")
    assert (
        manager._public_base_url(
            "publication-123",
            "https://uniform-test.trycloudflare.com",
        )
        == "https://uniform-test.trycloudflare.com/publication-123/v1"
    )


def test_start_uses_authenticated_gateway_and_reuses_one_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(
            created,
            output=b"https://managed-test.trycloudflare.com\n",
        ),
    )
    manager = _manager(tmp_path)

    first = manager.start("publication-a")
    second = manager.start("publication-b")
    repeated = manager.start("publication-a")

    assert first.public_base_url == ("https://managed-test.trycloudflare.com/publication-a/v1")
    assert second.public_base_url == ("https://managed-test.trycloudflare.com/publication-b/v1")
    assert repeated.tunnel_state == "running"
    assert len(created) == 1
    assert created[0].args[-2:] == ["--url", "http://127.0.0.1:18181"]
    assert "http://127.0.0.1:8000" not in created[0].args

    manager.stop("publication-a")
    assert manager.status("publication-b").tunnel_state == "running"
    assert len(created) == 1
    manager.stop("publication-b")
    assert created[0].terminated is True


def test_start_times_out_and_stops_process(tmp_path: Path, monkeypatch) -> None:
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(created),
    )
    manager = _manager(tmp_path, timeout=0.05)

    with pytest.raises(ExternalAccessStartError, match="did not provide"):
        manager.start("publication-timeout")

    assert len(created) == 1
    assert created[0].terminated is True
    assert manager.status("publication-timeout").enabled is False


def test_start_reports_unexpected_process_exit(tmp_path: Path, monkeypatch) -> None:
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(created, exit_code=17),
    )
    manager = _manager(tmp_path)

    with pytest.raises(ExternalAccessStartError, match="exited with code 17"):
        manager.start("publication-exit")

    assert len(created) == 1
    assert manager.status("publication-exit").enabled is False


def test_restore_keeps_valid_subscriptions_and_removes_stale_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "service-external-access.json"
    state_path.write_text(
        json.dumps({"publication_ids": ["valid", "stale"]}),
        encoding="utf-8",
    )
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(
            created,
            output=b"https://restore-test.trycloudflare.com\n",
        ),
    )
    binary = tmp_path / "cloudflared"
    binary.touch()
    manager = ServiceExternalAccessManager(
        state_path,
        cloudflared_binary=str(binary),
        start_timeout_seconds=1,
    )

    manager.restore_for_enabled_publications({"valid"})

    assert manager.status("valid").tunnel_state == "running"
    assert manager.status("stale").enabled is False
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"publication_ids": ["valid"]}
    assert state_path.stat().st_mode & 0o777 == 0o600
    manager.shutdown()


def test_named_tunnel_uses_stable_host_and_does_not_expose_token_in_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(
            created,
            output=(
                b"token=named-tunnel-token\n"
                b"INF Registered tunnel connection connIndex=0\n"
            ),
        ),
    )
    binary = tmp_path / "cloudflared"
    binary.touch()
    manager = ServiceExternalAccessManager(
        tmp_path / "service-external-access.json",
        cloudflared_binary=str(binary),
        tunnel_token="named-tunnel-token",
        public_host="models.example.com",
        start_timeout_seconds=1,
    )

    status = manager.start("publication-named")

    assert status.mode == "named"
    assert status.public_base_url == "https://models.example.com/publication-named/v1"
    assert created[0].args == [
        str(binary),
        "tunnel",
        "--no-autoupdate",
        "run",
    ]
    assert created[0].kwargs["env"]["TUNNEL_TOKEN"] == "named-tunnel-token"  # type: ignore[index]
    assert "named-tunnel-token" not in created[0].args
    assert "named-tunnel-token" not in repr(status)
    assert all("named-tunnel-token" not in line for line in manager._log_lines)
    manager.shutdown()


def test_public_health_failure_restarts_tunnel_and_increments_counter(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created: list[_FakeProcess] = []
    monkeypatch.setattr(
        external_access.subprocess,
        "Popen",
        _fake_popen_factory(
            created,
            output=b"https://health-test.trycloudflare.com\n",
        ),
    )
    manager = _manager(tmp_path)
    manager.health_failure_threshold = 1
    manager.start("publication-health")
    manager._probe_public_host = lambda _host: False  # type: ignore[method-assign]

    manager._watchdog_tick()

    status = manager.status("publication-health")
    assert len(created) == 2
    assert status.tunnel_state == "running"
    assert status.restart_count == 1
    assert status.consecutive_health_failures == 0
    manager.shutdown()


def test_external_api_requires_lan_access_and_protected_action_header(
    tmp_path: Path,
    monkeypatch,
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
        publication_manager = client.app.state.publication_manager
        publication_manager.ensure_gateway_started = lambda: None  # type: ignore[method-assign]
        publication_manager.stop_gateway_if_idle = lambda: None  # type: ignore[method-assign]
        publication_manager._port_is_loopback_only = lambda _name, _port: True  # type: ignore[method-assign]
        client.app.state.runtime.is_running = lambda _target_id: True
        client.app.state.runtime.is_named_container_running = lambda _name: True
        client.app.state.runtime.health = lambda _port, timeout=3.0: True
        external_manager: ServiceExternalAccessManager = client.app.state.external_access_manager

        def _fake_ensure_tunnel(self: ServiceExternalAccessManager) -> None:
            self._public_host = "https://api-test.trycloudflare.com"
            self._tunnel_state = "running"

        monkeypatch.setattr(
            ServiceExternalAccessManager,
            "_ensure_tunnel_locked",
            _fake_ensure_tunnel,
        )
        binary = tmp_path / "cloudflared-api"
        binary.touch()
        external_manager.cloudflared_binary = str(binary)
        deployment_id = _running_deployment(client)
        base_path = f"/api/v1/service-publications/deployment/{deployment_id}"
        external_path = f"{base_path}/external-access"
        protected_headers = {"X-Uniform-Action": "service-publication"}

        assert client.post(external_path).status_code == 403
        assert (
            client.post(
                external_path,
                headers={
                    "Host": "attacker.invalid",
                    "X-Uniform-Action": "service-publication",
                },
            ).status_code
            == 421
        )
        assert (
            client.post(
                external_path,
                headers=protected_headers,
            ).status_code
            == 409
        )

        published_response = client.post(base_path, headers=protected_headers)
        assert published_response.status_code == 201
        published = published_response.json()
        api_key = published["api_key"]
        publication_id = published["publication_id"]
        key_hint = published["key_hint"]
        publication = publication_manager.store.get_for_target(
            "deployment",
            deployment_id,
        )
        assert publication is not None

        enabled_response = client.post(external_path, headers=protected_headers)
        assert enabled_response.status_code == 200
        enabled = enabled_response.json()
        assert enabled["publication_id"] == publication_id
        assert enabled["key_hint"] == key_hint
        assert "api_key" not in enabled
        assert enabled["external_access_enabled"] is True
        assert enabled["external_access_state"] == "running"
        assert enabled["external_url"] == (
            f"https://api-test.trycloudflare.com/{publication_id}/v1"
        )
        assert publication_manager.verify_key(publication, api_key) is True

        disabled_response = client.delete(external_path, headers=protected_headers)
        assert disabled_response.status_code == 200
        disabled = disabled_response.json()
        assert disabled["publication_id"] == publication_id
        assert disabled["key_hint"] == key_hint
        assert disabled["external_access_enabled"] is False
        assert publication_manager.verify_key(publication, api_key) is True

        assert client.post(external_path).status_code == 403
        assert client.delete(external_path).status_code == 403
        revoked = client.delete(base_path, headers=protected_headers)
        assert revoked.status_code == 204
        revoked_status = client.get(external_path).json()
        assert revoked_status["enabled"] is False
        assert revoked_status["external_access_enabled"] is False
        assert revoked_status["external_access_state"] == "stopped"
        assert external_manager.status(publication_id).enabled is False


def test_static_ui_exposes_external_controls_and_shared_key_warning() -> None:
    root = Path(__file__).resolve().parents[1]
    web_root = root / "src" / "web"
    if not web_root.is_dir():
        web_root = root / "src" / "uniform_dgxspark" / "web"
    application = (web_root / "app.js").read_text(encoding="utf-8")
    index = (web_root / "index.html").read_text(encoding="utf-8")

    assert "外网访问" in application
    assert "复用当前局域网发布 ID 和 API Key" in application
    assert "重启后地址会变化" in application
    assert "不会暴露模型原始端口" in application
    assert 'id="service-publication-dialog"' in index
