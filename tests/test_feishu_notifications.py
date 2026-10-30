from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.feishu_api import FeishuCallbackLogFilter
from uniform_dgxspark.feishu_models import (
    CALLBACK_PATH,
    FeishuBindingDiagnostic,
    FeishuConfigUpdate,
)
from uniform_dgxspark.main import create_app
from uniform_dgxspark.services.feishu_notifications import (
    MESSAGE_URL,
    TOKEN_URL,
    USER_URL,
    FeishuError,
    FeishuNotifications,
)


class MockFeishu:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.failures: list[int] = []
        self.name = "测试接收人 <img src=x onerror=window.__feishuInjected=true>"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?", 1)[0]
        if url == TOKEN_URL:
            fields = parse_qs(request.content.decode())
            assert fields["client_id"] == ["cli_test"]
            assert fields["client_secret"] == ["private-secret-do-not-echo"]
            grant = fields["grant_type"][0]
            if grant == "client_credentials":
                assert fields["scope"] == ["im:message:send_as_bot"]
            else:
                assert grant == "authorization_code"
                assert len(fields["code_verifier"][0]) >= 43
            return httpx.Response(
                200, json={"code": 0, "access_token": "private-token", "expires_in": 120}
            )
        if url == USER_URL:
            assert request.headers["Authorization"] == "Bearer private-token"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "open_id": "ou_test",
                        "name": self.name,
                        "mobile": "must-not-persist",
                    },
                },
            )
        assert url == MESSAGE_URL
        assert request.url.params["receive_id_type"] == "open_id"
        body = json.loads(request.content)
        assert body["receive_id"] == "ou_test"
        assert request.headers["Authorization"] == "Bearer private-token"
        if self.failures:
            failure = self.failures.pop(0)
            return httpx.Response(
                failure if failure < 600 else 200,
                json={"code": failure, "msg": "private-secret-do-not-echo private-token"},
            )
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "test-message"}})

    def messages(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if str(r.url).startswith(MESSAGE_URL)]


def configuration(origin: str = "http://localhost", *, enabled: bool = False) -> FeishuConfigUpdate:
    return FeishuConfigUpdate(
        app_id="cli_test",
        app_secret="private-secret-do-not-echo",
        redirect_uri=origin + CALLBACK_PATH,
        enabled=enabled,
        rules=[
            {
                "metric": "gpu_temperature",
                "threshold": 85,
                "recovery": 80,
                "duration_seconds": 10,
                "cooldown_seconds": 60,
            }
        ],
    )


@pytest.fixture
def manager(tmp_path: Path):
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    mock = MockFeishu()
    service = FeishuNotifications(
        database,
        tmp_path,
        SimpleNamespace(snapshot=lambda: {}),
        SimpleNamespace(snapshot=lambda: {"items": []}),
        transport=httpx.MockTransport(mock),
    )
    yield service, mock
    service.stop()


def bind(service: FeishuNotifications) -> None:
    url = service.begin_binding("browser-nonce")
    params = parse_qs(urlsplit(url).query)
    assert urlsplit(url).path == "/oauth/v3/authorize"
    assert params["code_challenge_method"] == ["S256"]
    expected = (
        base64.urlsafe_b64encode(
            hashlib.sha256(service._oauth[params["state"][0]]["verifier"].encode()).digest()
        )
        .decode()
        .rstrip("=")
    )
    assert params["code_challenge"] == [expected]
    assert "scope" not in params  # Only identity, no chat access or offline user credentials.
    assert "app_secret" not in params
    service.complete_binding(params["state"][0], "one-time-code", "browser-nonce")


def test_v3_pkce_rfc_vector_and_strict_token_exchange(manager) -> None:
    service, mock = manager
    service.configure(configuration())
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    with patch(
        "uniform_dgxspark.services.feishu_notifications.secrets.token_urlsafe",
        side_effect=["local-state", verifier],
    ):
        url = service.begin_binding("browser-nonce")
    parsed = urlsplit(url)
    parameters = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "accounts.feishu.cn"
    assert parsed.path == "/oauth/v3/authorize"
    assert parameters["code_challenge"] == [expected]
    assert parameters["code_challenge_method"] == ["S256"]
    assert parameters["redirect_uri"] == [configuration().redirect_uri]

    def strict_provider(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            fields = parse_qs(request.content.decode())
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            assert fields["code_verifier"] == [verifier]
            assert fields["client_id"] == [parameters["client_id"][0]]
            assert fields["redirect_uri"] == [parameters["redirect_uri"][0]]
            assert fields["grant_type"] == ["authorization_code"]
            assert (
                base64.urlsafe_b64encode(
                    hashlib.sha256(fields["code_verifier"][0].encode()).digest()
                )
                .decode()
                .rstrip("=")
                == expected
            )
        return mock(request)

    service._client.close()
    service._client = httpx.Client(transport=httpx.MockTransport(strict_provider))
    service.complete_binding("local-state", "one-time-code", "browser-nonce")
    diagnostic = service.status()["last_binding"]
    assert diagnostic["stage"] == "complete" and diagnostic["result"] == "success"
    assert all(value is True for value in diagnostic["checks"].values())
    assert not mock.messages()


def test_pkce_provider_failure_is_recorded_without_credentials_or_fallback(manager, caplog) -> None:
    service, mock = manager
    service.configure(configuration())
    url = service.begin_binding("browser-nonce")
    parameters = parse_qs(urlsplit(url).query)
    state = parameters["state"][0]
    verifier = service._oauth[state]["verifier"]
    private_code = "private-one-time-code-never-log"
    request_id = "20261027120000ABCDEF0123456789ABCDEF0123456789"

    def reject(request: httpx.Request) -> httpx.Response:
        mock.requests.append(request)
        assert str(request.url) == TOKEN_URL
        return httpx.Response(
            400,
            headers={"x-tt-logid": request_id},
            json={
                "code": 20049,
                "error": "invalid_grant",
                "error_description": "private-secret-do-not-echo " + verifier + " " + private_code,
            },
        )

    service._client.close()
    service._client = httpx.Client(transport=httpx.MockTransport(reject))
    with caplog.at_level(logging.INFO), pytest.raises(FeishuError, match="20049") as caught:
        service.complete_binding(state, private_code, "browser-nonce")
    diagnostic = service.status()["last_binding"]
    assert caught.value.binding_attempt_id == diagnostic["attempt_id"]
    assert diagnostic["stage"] == "user_token" and diagnostic["result"] == "failed"
    assert diagnostic["error_code"] == 20049 and diagnostic["http_status"] == 400
    assert diagnostic["request_id"] == request_id
    assert all(value is True for value in diagnostic["checks"].values())
    assert len(mock.requests) == 1  # No v2 fallback, retry or insecure verifier-free exchange.
    assert not service.status()["recipient"] and not service.status()["config"]["enabled"]
    public = json.dumps(service.status()) + caplog.text + service.diagnostic_path.read_text()
    for sensitive in (
        "private-secret-do-not-echo",
        verifier,
        private_code,
        state,
        parameters["code_challenge"][0],
    ):
        assert sensitive not in public
    assert service.diagnostic_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bad_verifier", ["different-verifier" * 4, "short", "非 ASCII 校验字符串"])
def test_local_pkce_mismatch_fails_before_network(manager, bad_verifier) -> None:
    service, mock = manager
    service.configure(configuration())
    state = parse_qs(urlsplit(service.begin_binding("browser-nonce")).query)["state"][0]
    service._oauth[state]["verifier"] = bad_verifier
    with pytest.raises(FeishuError, match="本地授权参数"):
        service.complete_binding(state, "code", "browser-nonce")
    assert not mock.requests
    assert service.status()["last_binding"]["reason"] == "local_pkce_mismatch"
    assert service.status()["last_binding"]["checks"]["pkce_challenge_matches"] is False


def test_binding_diagnostic_survives_restart_and_denial_does_not_exchange_code(manager) -> None:
    service, mock = manager
    service.configure(configuration())
    state = parse_qs(urlsplit(service.begin_binding("browser-nonce")).query)["state"][0]
    with pytest.raises(FeishuError):
        service.complete_binding(state, "", "browser-nonce", authorization_error="access_denied")
    assert not mock.requests
    assert service.status()["last_binding"]["reason"] == "user_denied"
    replacement = FeishuNotifications(
        service.database,
        service.path.parent.parent,
        service.metrics,
        service.cluster,
        transport=httpx.MockTransport(mock),
    )
    try:
        assert replacement.status()["last_binding"] == service.status()["last_binding"]
    finally:
        replacement.stop()


def test_invalid_or_insecure_saved_diagnostic_is_ignored(manager) -> None:
    service, _ = manager
    service.configure(configuration())
    service.diagnostic_path.write_text('{"private_secret":"must-not-return"}')
    service.diagnostic_path.chmod(0o644)
    replacement = FeishuNotifications(
        service.database,
        service.path.parent.parent,
        service.metrics,
        service.cluster,
    )
    try:
        assert replacement.status()["last_binding"] is None
        assert "must-not-return" not in json.dumps(replacement.status())
    finally:
        replacement.stop()


def test_binding_diagnostic_schema_rejects_unsanitized_provider_data() -> None:
    with pytest.raises(ValueError):
        FeishuBindingDiagnostic.model_validate(
            {
                "attempt_id": "0" * 36,
                "stage": "user_token",
                "result": "failed",
                "checked_at": 1.0,
                "request_id": "private-authorization-code",
                "error_description": "private-secret",
            }
        )


def ready(service: FeishuNotifications) -> None:
    service.configure(configuration())
    bind(service)
    service.test_message()
    service.configure(configuration(enabled=True))


def sample(now: float, temperature: float | None = 90) -> dict:
    return {"timestamp": now, "gpu": {"available": True, "temperature_c": temperature}}


def tick(service: FeishuNotifications, now: float, temperature: float | None = 90) -> None:
    service.tick(now=now, local=sample(now, temperature), remote=[])


def alerts(service: FeishuNotifications) -> list[dict]:
    return [event for event in service.events()["items"] if event["kind"] != "test"]


def test_opt_in_gates_and_private_storage(manager) -> None:
    service, mock = manager
    tick(service, 1000)
    assert not mock.requests
    assert not service.path.exists()
    service.configure(configuration())
    assert service.path.stat().st_mode & 0o777 == 0o600
    assert service.path.parent.stat().st_mode & 0o777 == 0o700
    assert "app_secret" not in service.status()["config"]
    assert "private-secret" not in json.dumps(service.status())
    with pytest.raises(FeishuError, match="扫码绑定"):
        service.configure(configuration(enabled=True))
    bind(service)
    with pytest.raises(FeishuError, match="测试消息"):
        service.configure(configuration(enabled=True))
    before = len(mock.messages())
    tick(service, 1000)
    assert len(mock.messages()) == before == 0
    service.test_message()
    with pytest.raises(FeishuError, match="30 秒"):
        service.test_message()
    assert len(mock.messages()) == 1


def test_continuous_threshold_cooldown_hysteresis_and_recovery(manager) -> None:
    service, mock = manager
    ready(service)
    tick(service, 1000)
    tick(service, 1005, 79)  # A short spike is not an alert.
    assert not alerts(service)
    tick(service, 1010)
    tick(service, 1020)
    tick(service, 1040, 83)  # Hysteresis keeps the incident active.
    tick(service, 1060)
    assert [e["kind"] for e in alerts(service)] == ["alert"]
    tick(service, 1080)
    assert [e["kind"] for e in alerts(service)] == ["reminder", "alert"]
    tick(service, 1100, 79)
    tick(service, 1120, 79)
    assert [e["kind"] for e in alerts(service)] == ["recovery", "reminder", "alert"]
    assert len(mock.messages()) == 4
    assert all(e["delivery"] == "sent" for e in alerts(service))


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True])
def test_unknown_measurements_do_not_act_as_zero_or_recover(manager, missing) -> None:
    service, _ = manager
    ready(service)
    tick(service, 1000)
    tick(service, 1010)
    tick(service, 1020, missing)
    service.tick(now=1060, local=sample(1010, 0), remote=[])
    assert len(alerts(service)) == 1
    tick(service, 1070, 79)
    assert alerts(service)[0]["kind"] == "recovery"


def test_duplicate_and_gapped_samples_cannot_establish_duration(manager) -> None:
    service, _ = manager
    ready(service)
    tick(service, 1000)
    service.tick(now=1010, local=sample(1000), remote=[])
    assert not alerts(service)
    tick(service, 1040)
    assert not alerts(service)
    tick(service, 1050)
    assert len(alerts(service)) == 1


def test_failed_notifications_retry_same_uuid_and_stop_after_three(manager) -> None:
    service, mock = manager
    ready(service)
    mock.failures = [503, 429, 503]
    tick(service, 1000)
    tick(service, 1010)
    tick(service, 1030)
    tick(service, 1040)
    tick(service, 1060, 83)
    tick(service, 1080, 83)
    tick(service, 1100, 83)
    event = alerts(service)[0]
    assert event["delivery"] == "failed" and event["attempts"] == 3
    assert "private-token" not in event["error"]
    retries = mock.messages()[1:]
    assert len(retries) == 3 and len({m["uuid"] for m in retries}) == 1


def test_permission_failure_is_not_retried_or_marked_verified(manager) -> None:
    service, mock = manager
    service.configure(configuration())
    bind(service)
    mock.failures = [230027]
    with pytest.raises(FeishuError, match="发消息权限"):
        service.test_message()
    assert not service.status()["recipient"]["verified_at"]
    assert service.events()["items"][0]["delivery"] == "failed"
    assert "private-token" not in json.dumps(service.status())


def test_recovery_cancels_pending_warning_and_removal_cancels_old_notifications(manager) -> None:
    service, mock = manager
    ready(service)
    mock.failures = [503]
    tick(service, 1000)
    tick(service, 1010)
    tick(service, 1020, 70)
    events = alerts(service)
    assert events[0]["kind"] == "recovery" and events[0]["delivery"] == "sent"
    assert events[1]["delivery"] == "cancelled"
    service.unbind()
    assert not service.status()["recipient"] and not service.status()["config"]["enabled"]
    previous = len(mock.messages())
    tick(service, 1040)
    assert len(mock.messages()) == previous


def test_restart_retains_recipient_config_and_cooldown_but_not_user_tokens(manager) -> None:
    service, mock = manager
    ready(service)
    tick(service, 1000)
    tick(service, 1010)
    replacement = FeishuNotifications(
        service.database,
        service.path.parent.parent,
        service.metrics,
        service.cluster,
        transport=httpx.MockTransport(mock),
    )
    try:
        tick(replacement, 1030)
        assert len(alerts(replacement)) == 1
        assert replacement.status()["recipient"]["verified_at"]
        assert "private-token" not in service.path.read_text()
        with service.database.connect() as c:
            assert "mobile" not in dict(c.execute("SELECT * FROM feishu_recipient").fetchone())
    finally:
        replacement.stop()


def test_credentials_changed_clear_binding_and_invalidate_oauth(manager) -> None:
    service, _ = manager
    ready(service)
    update = configuration()
    update.app_secret = "new-private-secret"
    service.configure(update)
    assert not service.status()["recipient"]
    with pytest.raises(FeishuError):
        service.configure(configuration(enabled=True))


@pytest.mark.parametrize("code", [False, [], None, "0"])
def test_malformed_error_codes_are_never_success(manager, code) -> None:
    service, _ = manager
    service._client.close()
    service._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": code, "access_token": "must-not-accept"})
        )
    )
    with pytest.raises(FeishuError, match="有效错误码"):
        service._request("POST", TOKEN_URL)


def test_remote_removal_cancels_pending_alert_without_sending_fake_recovery(manager) -> None:
    service, mock = manager
    ready(service)
    update = configuration(enabled=True)
    update.rules = [type(update.rules[0])(metric="offline", duration_seconds=10)]
    service.configure(update)
    mock.failures = [503]
    for now in [1000, 1010]:
        service.tick(
            now=now,
            local=sample(now),
            remote=[
                {
                    "id": "remote",
                    "label": "远端",
                    "online": False,
                    "observed_at": now,
                }
            ],
        )
    service.tick(now=1020, local=sample(1020), remote=[])
    assert len(alerts(service)) == 1 and alerts(service)[0]["delivery"] == "cancelled"


def test_oauth_state_is_browser_bound_single_use_and_expires(manager) -> None:
    service, mock = manager
    service.configure(configuration())
    url = service.begin_binding("browser-nonce")
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(FeishuError, match="当前浏览器"):
        service.complete_binding(state, "code", "wrong-browser")
    assert not mock.requests
    service.complete_binding(state, "code", "browser-nonce")
    with pytest.raises(FeishuError):
        service.complete_binding(state, "code", "browser-nonce")
    state = parse_qs(urlsplit(service.begin_binding("browser-nonce")).query)["state"][0]
    service._oauth[state]["expires"] = time.time() - 1
    with pytest.raises(FeishuError):
        service.complete_binding(state, "code", "browser-nonce")


def test_offline_and_online_remote_nodes_use_fresh_observations(manager) -> None:
    service, _ = manager
    ready(service)
    update = configuration(enabled=True)
    update.rules = [type(update.rules[0])(metric="offline", duration_seconds=10)]
    service.configure(update)
    for now in [1000, 1010]:
        node = {"id": "remote", "label": "远端", "online": False, "observed_at": now}
        service.tick(now=now, local=sample(now), remote=[node])
    assert alerts(service)[0]["kind"] == "alert"
    service.tick(
        now=1020, local=sample(1020), remote=[{**node, "online": True, "observed_at": 1020}]
    )
    assert alerts(service)[0]["kind"] == "recovery"


def test_access_logs_remove_oauth_code_and_state() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        20,
        "",
        0,
        "%s %s %s %s %s",
        ("client", "GET", CALLBACK_PATH + "?code=secret&state=nonce", "1.1", 200),
        None,
    )
    assert FeishuCallbackLogFilter().filter(record)
    assert "secret" not in record.getMessage() and "nonce" not in record.getMessage()


def test_api_auth_csrf_capability_secret_redaction_and_callback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "uniform_dgxspark.services.docker_runtime.DockerRuntime.discover_ai_services", lambda _: []
    )
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False, api_token="api-test-key"))
    mock = MockFeishu()
    with TestClient(app, base_url="http://localhost") as client:
        service = app.state.feishu_notifications
        service._client.close()
        service._client = httpx.Client(transport=httpx.MockTransport(mock))
        assert client.get("/api/v1/system/feishu").status_code == 401
        client.headers["X-API-Key"] = "api-test-key"
        assert client.get("/api/v1/system/feishu").json()["configured"] is False
        body = configuration().model_dump(mode="json", exclude={"app_secret"})
        body["app_secret"] = "private-secret-do-not-echo"
        assert client.put("/api/v1/system/feishu/config", json=body).status_code == 403
        client.headers["X-Uniform-Action"] = "feishu-notifications"
        assert (
            client.put(
                "/api/v1/system/feishu/config",
                json=body,
                headers={"Origin": "https://hostile.example"},
            ).status_code
            == 403
        )
        response = client.put("/api/v1/system/feishu/config", json={**body, "rules": []})
        assert response.status_code == 422 and "private-secret" not in response.text
        response = client.put("/api/v1/system/feishu/config", json=body)
        assert response.status_code == 200 and "private-secret" not in response.text
        assert (
            client.get(
                "/api/v1/system/feishu", headers={"X-AI-Capability": "untrusted"}
            ).status_code
            == 403
        )
        response = client.post("/api/v1/system/feishu/bind")
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        state = parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0]
        # The callback uses one-time browser state, not an API key in URLs.
        client.headers.pop("X-API-Key")
        assert (
            client.get(CALLBACK_PATH, params={"state": state, "code": "one-time-code"}).status_code
            == 200
        )
        client.headers["X-API-Key"] = "api-test-key"
        assert client.get("/api/v1/system/feishu").json()["recipient"]
        assert "private-token" not in client.get("/api/v1/system/feishu").text
        assert client.post("/api/v1/system/feishu/test").status_code == 200
        assert (
            client.put("/api/v1/system/feishu/config", json={**body, "enabled": True}).status_code
            == 200
        )


def test_schema_24_upgrade_preserves_integrations_and_accuracy_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "uniform.db")
    db.initialize()
    with db.connect() as c:
        for table in ["feishu_recipient", "system_alert_events", "system_alert_state"]:
            c.execute(f"DROP TABLE {table}")
        c.execute("DELETE FROM schema_migrations")
        c.execute("INSERT INTO schema_migrations VALUES(24,'2026-10-27')")
        c.execute(
            "INSERT INTO accuracy_dataset_integrations VALUES('mmlu','fingerprint','run','time')"
        )
        c.commit()
    db.initialize()
    with db.connect() as c:
        assert [r[0] for r in c.execute("SELECT version FROM schema_migrations")] == [24, 25]
        assert c.execute("SELECT COUNT(*) FROM accuracy_dataset_integrations").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM accuracy_runs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/bad-path",
        "http://192.168.1.1" + CALLBACK_PATH,
        "https://user:password@example.com" + CALLBACK_PATH,
        "https://example.com" + CALLBACK_PATH + "?code=evil",
    ],
)
def test_callback_urls_are_restricted(uri: str) -> None:
    with pytest.raises(ValueError):
        FeishuConfigUpdate(redirect_uri=uri)
