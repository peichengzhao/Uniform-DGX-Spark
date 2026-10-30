from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.ai_store import (
    AIDeploymentReusePending,
    AIDeploymentTemplateNotFound,
    AISessionStore,
)
from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import (
    AIDeploymentResultCreate,
    AIHealthCheckResult,
    AIInferenceTestResult,
    AISessionCreate,
)
from uniform_dgxspark.services.deployment_templates import (
    AIDeploymentTemplateManager,
    DeploymentTemplateBusy,
    DeploymentTemplateExecutionError,
    DeploymentTemplateInvalid,
)
from uniform_dgxspark.services.docker_runtime import (
    CommandResult,
    DockerRuntime,
    RuntimeErrorBase,
)
from uniform_dgxspark.state import DeploymentStatus


def deployment_result(
    *,
    engine: str = "sglang",
    launch_command: str = "docker run example/inference:1",
    container_id: str = "container-id",
) -> AIDeploymentResultCreate:
    return AIDeploymentResultCreate(
        engine=engine,
        container_name="uniform-template-test",
        container_id=container_id,
        image="example/inference:1",
        port=19041,
        service_url="http://127.0.0.1:19041",
        launch_command=launch_command,
        health_check=AIHealthCheckResult(
            url="http://127.0.0.1:19041/health",
            status_code=200,
            passed=True,
            details="healthy",
        ),
        inference_test=AIInferenceTestResult(
            url="http://127.0.0.1:19041/v1/chat/completions",
            status_code=200,
            passed=True,
            request_summary="one redacted prompt",
            response_summary="one choice",
        ),
    )


def test_verified_template_is_credential_free_and_survives_failed_result(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    workspace = tmp_path / "ai-sessions" / "template-session"
    workspace.mkdir(parents=True)
    request = AISessionCreate(
        provider="codex",
        target_model_id="org/model",
        target_source="huggingface",
        reference_urls=[
            "https://example.com/tutorial",
            "https://example.com/private?access_token=url-secret-value",
        ],
    )
    session = store.create(request, str(workspace), session_id="template-session")
    context = {
        "deployment": {
            "target_model": {
                "model_id": "org/model",
                "source": "huggingface",
                "revision": None,
            }
        },
        "deployment_topology": {"resolved": "single_node"},
    }
    verified = store.upsert_result(
        session.id,
        deployment_result(
            launch_command=(
                "docker run -e API_KEY=supersecret -e HF_TOKEN=hf_123456789 "
                "example/inference:1 'https://example.com/model?signature=signed-value'"
            )
        ),
    )
    manager = AIDeploymentTemplateManager(store, tmp_path / "templates")

    template = manager.create(session, verified, deployment_context=context)

    script = Path(template.script_path).read_text(encoding="utf-8")
    manifest_text = (Path(template.script_path).parent / "deployment-template.json").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(manifest_text)
    serialized_template = template.model_dump_json()
    serialized_artifacts = script + manifest_text + serialized_template
    redacted_command = manifest["launch_command_template"]
    assert redacted_command != verified.launch_command
    assert "required at launch" in redacted_command
    assert verified.launch_command not in script
    assert "required at launch" not in script
    assert template.launch_command_template == redacted_command
    assert template.stop_script_path is not None
    assert template.stop_script_sha256 is not None
    stop_script = Path(template.stop_script_path).read_text(encoding="utf-8")
    assert hashlib.sha256(script.encode()).hexdigest() == template.script_sha256
    assert hashlib.sha256(stop_script.encode()).hexdigest() == template.stop_script_sha256
    assert template.reuse_mode == "ai_session"
    assert template.format_version == AIDeploymentTemplateManager.FORMAT_VERSION
    assert template.requires_secret_values is True
    assert "supersecret" not in serialized_artifacts
    assert "hf_123456789" not in serialized_artifacts
    assert "signed-value" not in serialized_artifacts
    assert "url-secret-value" not in serialized_artifacts
    assert template.reference_urls == ["https://example.com/tutorial"]

    failed = deployment_result()
    failed.inference_test.passed = False
    assert store.upsert_result(session.id, failed).status == "failed"
    latest = store.latest_template(
        model_id="org/model",
        target_source="huggingface",
        deployment_topology="single_node",
    )
    assert latest.id == template.id
    assert latest.script_sha256 == template.script_sha256
    with pytest.raises(AIDeploymentTemplateNotFound):
        store.latest_template(
            model_id="org/model",
            target_source="huggingface",
            target_revision="different-revision",
            deployment_topology="single_node",
        )


def test_template_writes_paired_container_lifecycle_scripts_and_hashes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    workspace = tmp_path / "ai-sessions" / "paired-artifacts"
    workspace.mkdir(parents=True)
    session = store.create(
        AISessionCreate(
            provider="codex",
            target_model_id="org/paired-model",
            target_source="huggingface",
        ),
        str(workspace),
        session_id="paired-artifacts",
    )
    result = store.upsert_result(
        session.id,
        deployment_result(
            engine="sglang\nprintf ENGINE_METADATA_INJECTION\n",
            launch_command=(
                "docker run --name uniform-template-test --label "
                "original-launch-command=must-stay-in-manifest example/inference:1"
            ),
            container_id="a" * 64,
        ),
    )
    manager = AIDeploymentTemplateManager(store, tmp_path / "templates")

    template = manager.create(
        session,
        result,
        deployment_context={
            "target_model": {
                "model_id": "org/paired-model",
                "source": "huggingface",
            },
            "deployment_topology": {"resolved": "single_node"},
        },
    )

    assert template.artifact_verification_status == "pending"
    assert template.stop_script_path is not None
    assert template.stop_script_sha256 is not None
    start_path = Path(template.script_path)
    stop_path = Path(template.stop_script_path)
    start_script = start_path.read_text(encoding="utf-8")
    stop_script = stop_path.read_text(encoding="utf-8")
    manifest = json.loads(
        (start_path.parent / "deployment-template.json").read_text(encoding="utf-8")
    )
    assert start_path.name == "start-reusable.sh"
    assert stop_path.name == "stop-reusable.sh"
    assert start_path.stat().st_mode & 0o777 == 0o700
    assert stop_path.stat().st_mode & 0o777 == 0o700
    assert 'exec "${docker_bin}" start "${container_name}"' in start_script
    assert 'exec "${docker_bin}" stop --time 30 "${container_name}"' in stop_script
    assert "original-launch-command" not in start_script + stop_script
    assert "original-launch-command" in manifest["launch_command_template"]
    assert "ENGINE_METADATA_INJECTION" not in start_script + stop_script
    assert "ENGINE_METADATA_INJECTION" in manifest["engine"]
    assert hashlib.sha256(start_path.read_bytes()).hexdigest() == template.script_sha256
    assert hashlib.sha256(stop_path.read_bytes()).hexdigest() == template.stop_script_sha256
    assert manifest["script_sha256"] == template.script_sha256
    assert manifest["stop_script_sha256"] == template.stop_script_sha256


def test_generated_lifecycle_scripts_execute_with_minimal_environment_and_exact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
fake_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf 'secret=%s\n' "${UNIFORM_TEST_SECRET-unset}" >> "$fake_root/environment.log"
printf '%s\n' "$*" >> "$fake_root/docker.log"
case "$1" in
  inspect)
    test "$2" = "--format"
    test "$4" = "uniform-template-test"
    case "$3" in
      '{{.Id}}') cat "$fake_root/container-id" ;;
      '{{.Config.Image}}') cat "$fake_root/container-image" ;;
      '{{.HostConfig.AutoRemove}}') cat "$fake_root/container-auto-remove" ;;
      '{{.State.Running}}') cat "$fake_root/container-running" ;;
      *) exit 91 ;;
    esac
    ;;
  start)
    test "$2" = "uniform-template-test"
    printf 'true\n' > "$fake_root/container-running"
    ;;
  stop)
    test "$2" = "--time"
    test "$3" = "30"
    test "$4" = "uniform-template-test"
    printf 'false\n' > "$fake_root/container-running"
    ;;
  *) exit 92 ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o700)
    container_id_path = tmp_path / "container-id"
    container_image_path = tmp_path / "container-image"
    container_auto_remove_path = tmp_path / "container-auto-remove"
    container_running_path = tmp_path / "container-running"
    expected_container_id = "b" * 64
    container_id_path.write_text(f"{expected_container_id}\n", encoding="utf-8")
    container_image_path.write_text("example/inference:1\n", encoding="utf-8")
    container_auto_remove_path.write_text("true\n", encoding="utf-8")
    container_running_path.write_text("false\n", encoding="utf-8")

    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    workspace = tmp_path / "ai-sessions" / "executable-artifacts"
    workspace.mkdir(parents=True)
    session = store.create(
        AISessionCreate(
            provider="codex",
            target_model_id="org/executable-model",
            target_source="huggingface",
        ),
        str(workspace),
        session_id="executable-artifacts",
    )
    result = store.upsert_result(
        session.id,
        deployment_result(container_id=expected_container_id),
    )
    manager = AIDeploymentTemplateManager(
        store,
        tmp_path / "templates",
        docker_binary=str(fake_docker),
    )
    pending = manager.create(
        session,
        result,
        deployment_context={
            "target_model": {
                "model_id": "org/executable-model",
                "source": "huggingface",
            },
            "deployment_topology": {"resolved": "single_node"},
        },
    )
    monkeypatch.setenv("UNIFORM_TEST_SECRET", "must-not-leak")

    with pytest.raises(DeploymentTemplateExecutionError, match="exited with code 67"):
        manager.execute(pending, "stop", allow_unverified=True)
    assert "stop --time 30 uniform-template-test" not in (tmp_path / "docker.log").read_text(
        encoding="utf-8"
    )

    container_auto_remove_path.write_text("false\n", encoding="utf-8")
    template = manager.update_verification(pending, "verified")
    manager.execute(template, "start")
    assert container_running_path.read_text(encoding="utf-8").strip() == "true"
    manager.execute(template, "stop")
    assert container_running_path.read_text(encoding="utf-8").strip() == "false"
    docker_calls = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert "inspect --format {{.Id}} uniform-template-test" in docker_calls
    assert "inspect --format {{.Config.Image}} uniform-template-test" in docker_calls
    assert "inspect --format {{.HostConfig.AutoRemove}} uniform-template-test" in docker_calls
    assert "inspect --format {{.State.Running}} uniform-template-test" in docker_calls
    assert "start uniform-template-test" in docker_calls
    assert "stop --time 30 uniform-template-test" in docker_calls
    environment_lines = (tmp_path / "environment.log").read_text(encoding="utf-8").splitlines()
    assert environment_lines
    assert set(environment_lines) == {"secret=unset"}

    container_id_path.write_text(f"{'c' * 64}\n", encoding="utf-8")
    with pytest.raises(DeploymentTemplateExecutionError, match="exited with code 65"):
        manager.execute(template, "start")
    container_id_path.write_text(f"{expected_container_id}\n", encoding="utf-8")
    container_image_path.write_text("example/replaced:2\n", encoding="utf-8")
    with pytest.raises(DeploymentTemplateExecutionError, match="exited with code 66"):
        manager.execute(template, "start")

    start_path = Path(template.script_path)
    start_path.write_text(
        start_path.read_text(encoding="utf-8") + "echo tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(DeploymentTemplateInvalid, match="integrity check failed"):
        manager.execute(template, "start")


def test_named_container_identity_includes_auto_remove_policy() -> None:
    container_id = "f" * 64

    class IdentityRunner:
        def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
            assert arguments == [
                "docker",
                "inspect",
                "--format",
                "{{json .}}",
                "uniform-template-test",
            ]
            assert timeout == 10
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Id": container_id,
                        "Config": {"Image": "example/inference:1"},
                        "State": {"Running": True},
                        "HostConfig": {"AutoRemove": True},
                    }
                ),
                "",
            )

    identity = DockerRuntime(runner=IdentityRunner()).named_container_identity(
        "uniform-template-test"
    )

    assert identity == {
        "container_id": container_id,
        "image": "example/inference:1",
        "running": True,
        "auto_remove": True,
    }


def test_lifecycle_operation_lock_raises_busy_and_releases_after_concurrent_use(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    manager = AIDeploymentTemplateManager(AISessionStore(database), tmp_path / "templates")
    entered = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def hold_operation() -> None:
        try:
            with manager.operation("shared-template"):
                entered.set()
                assert release.wait(timeout=5)
        except BaseException as error:  # noqa: BLE001 - propagated to the test thread
            holder_errors.append(error)

    holder = threading.Thread(target=hold_operation, daemon=True)
    holder.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(DeploymentTemplateBusy) as busy:
            with manager.operation("shared-template"):
                pytest.fail("a concurrent operation must not enter the critical section")
        assert isinstance(busy.value, DeploymentTemplateExecutionError)
        assert "already running" in str(busy.value)
        # A different template has an independent lifecycle lock.
        with manager.operation("other-template"):
            pass
    finally:
        release.set()
        holder.join(timeout=2)

    assert holder.is_alive() is False
    assert holder_errors == []
    # The lock is released even after a competing operation was rejected.
    with manager.operation("shared-template"):
        pass


def test_multi_node_launch_command_is_never_a_direct_script_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    workspace = tmp_path / "ai-sessions" / "multi-node-command"
    workspace.mkdir(parents=True)
    session = store.create(
        AISessionCreate(
            provider="codex",
            target_model_id="org/multi-node-model",
            target_source="huggingface",
        ),
        str(workspace),
        session_id="multi-node-command",
    )
    result = store.upsert_result(
        session.id,
        deployment_result(
            launch_command="torchrun --nnodes 2 serve.py",
            container_id="e" * 64,
        ),
    )
    manager = AIDeploymentTemplateManager(store, tmp_path / "templates")

    template = manager.create(
        session,
        result,
        deployment_context={
            "target_model": {
                "model_id": "org/multi-node-model",
                "source": "huggingface",
            },
            # Even a stale single-node context must not override command evidence.
            "deployment_topology": {"resolved": "single_node"},
        },
    )

    assert template.reuse_mode == "ai_session"
    assert template.artifact_verification_status == "legacy_unverified"


@pytest.mark.parametrize(
    ("command", "secret"),
    [
        ("API_KEY='secret value with spaces' docker run image", "secret value"),
        ("TOKEN=$(cat /private/token) docker run image", "/private/token"),
        ("docker login -u robot -p registry-password registry.local", "registry-password"),
        ("curl -u robot:http-password https://example.invalid", "http-password"),
    ],
)
def test_template_sanitizer_removes_complex_shell_credentials(command: str, secret: str) -> None:
    sanitized, changed = AIDeploymentTemplateManager._sanitize_text(command)

    assert changed is True
    assert secret not in sanitized
    assert "required" in sanitized


def test_template_sanitizer_preserves_non_secret_token_configuration() -> None:
    command = (
        "docker run -e HF_HUB_OFFLINE=1 --speculative-num-draft-tokens 8 "
        "--max-total-tokens 32768 image"
    )

    sanitized, changed = AIDeploymentTemplateManager._sanitize_text(command)

    assert changed is False
    assert sanitized == command


def test_certified_direct_script_launches_and_smoke_tests_without_ai_then_tampering_returns_to_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
            default_startup_timeout_seconds=10,
        )
    )
    container_id = "d" * 64
    running = {"value": True}
    auto_remove = {"value": True}
    executed: list[tuple[str, bool]] = []
    smoke_tests: list[tuple[int, str | None, str, float]] = []
    ai_prompts: list[tuple[str, str]] = []

    def container_identity(_container_name: str) -> dict[str, str | bool]:
        return {
            "container_id": container_id,
            "image": "example/inference:1",
            "running": running["value"],
            "auto_remove": auto_remove["value"],
        }

    def execute_artifact(_template: object, action: str, *, allow_unverified: bool = False) -> str:
        executed.append((action, allow_unverified))
        running["value"] = action == "start"
        return f"{action} completed"

    def smoke_test(
        port: int,
        *,
        model_name: str | None = None,
        endpoint: str = "completions",
        timeout: float = 30,
    ) -> str:
        assert running["value"] is True
        assert endpoint == "chat_completions"
        smoke_tests.append((port, model_name, endpoint, timeout))
        return "org/direct-model"

    def record_instruction(source_session_id: str, prompt: str):  # type: ignore[no-untyped-def]
        ai_prompts.append((source_session_id, prompt))
        return app.state.ai_store.get(source_session_id)

    with TestClient(app) as client:
        monkeypatch.setattr(
            client.app.state.runtime,
            "named_container_identity",
            container_identity,
        )
        monkeypatch.setattr(
            client.app.state.runtime,
            "is_named_container_running",
            lambda _container_name: running["value"],
        )
        monkeypatch.setattr(
            client.app.state.runtime,
            "health",
            lambda _port, timeout: running["value"],
        )
        monkeypatch.setattr(client.app.state.runtime, "smoke_test_service", smoke_test)
        monkeypatch.setattr(client.app.state.ai_deployment_templates, "execute", execute_artifact)
        monkeypatch.setattr(client.app.state.ai_terminal, "send_instruction", record_instruction)
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        session_id = created["id"]
        capability = client.app.state.ai_terminal._capabilities[session_id]
        result_payload = deployment_result(
            launch_command="docker run --name uniform-template-test example/inference:1",
            container_id=container_id,
        ).model_dump(mode="json")
        auto_remove_rejected = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result",
            headers={"X-AI-Capability": capability},
            json=result_payload,
        )
        assert auto_remove_rejected.status_code == 409
        assert "auto-remove" in auto_remove_rejected.json()["detail"]
        rejected_template = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        assert rejected_template["reuse_mode"] == "ai_session"
        assert rejected_template["artifact_verification_status"] == "failed"
        assert executed == []
        assert smoke_tests == []
        assert ai_prompts == []

        auto_remove["value"] = False
        reported = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result",
            headers={"X-AI-Capability": capability},
            json=result_payload,
        )
        assert reported.status_code == 200, reported.text
        template = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        assert template["reuse_mode"] == "direct_script"
        assert template["artifact_verification_status"] == "verified"
        assert executed == [("stop", True), ("start", True)]
        assert len(smoke_tests) == 2
        assert ai_prompts == []

        with client.app.state.ai_deployment_templates.operation(session_id):
            busy_report = client.post(
                f"/api/v1/ai/sessions/{session_id}/actions/result",
                headers={"X-AI-Capability": capability},
                json=result_payload,
            )
        assert busy_report.status_code == 409
        assert busy_report.headers["Retry-After"] == "1"
        after_busy_report = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        assert after_busy_report["reuse_mode"] == "direct_script"
        assert after_busy_report["artifact_verification_status"] == "verified"
        assert executed == [("stop", True), ("start", True)]
        assert ai_prompts == []

        running["value"] = False
        smoke_tests.clear()
        launch_payload = {
            "client_request_id": "reuse-direct-001",
            "model_id": "org/direct-model",
            "target_source": "huggingface",
            "deployment_topology": "single_node",
        }
        with client.app.state.ai_deployment_templates.operation(template["id"]):
            busy_launch = client.post("/api/v1/ai/deployment-templates/launch", json=launch_payload)
        assert busy_launch.status_code == 409
        assert busy_launch.headers["Retry-After"] == "1"
        still_verified = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        assert still_verified["reuse_mode"] == "direct_script"
        assert still_verified["artifact_verification_status"] == "verified"
        assert ai_prompts == []

        # Busy is retryable with the same idempotency key after the lock is released.
        launched = client.post("/api/v1/ai/deployment-templates/launch", json=launch_payload)
        assert launched.status_code == 200
        assert launched.json()["reuse_method"] == "direct_script"
        assert launched.json()["fallback_to_ai"] is False
        assert executed[-1] == ("start", False)
        assert smoke_tests == [(19041, "org/direct-model", "chat_completions", 60)]
        assert ai_prompts == []

        running["value"] = False
        Path(template["script_path"]).write_text("tampered\n", encoding="utf-8")
        tampered = client.post(
            "/api/v1/ai/deployment-templates/launch",
            json={
                "client_request_id": "reuse-direct-002",
                "model_id": "org/direct-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        )
        assert tampered.status_code == 200
        assert tampered.json()["reuse_method"] == "source_ai_session"
        assert tampered.json()["fallback_to_ai"] is True
        assert tampered.json()["session"]["id"] == session_id
        assert len(ai_prompts) == 1
        assert ai_prompts[0][0] == session_id
        assert "integrity check failed" in ai_prompts[0][1]


def test_external_template_uses_ai_and_tampering_returns_to_original_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
        )
    )
    with TestClient(app) as client:
        external_container_id = "1" * 64
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "org/external-model",
                "target_source": "huggingface",
            },
        ).json()
        session_id = created["id"]
        capability = client.app.state.ai_terminal._capabilities[session_id]
        reported = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result",
            headers={"X-AI-Capability": capability},
            json=deployment_result(
                engine="sglang",
                launch_command=("bash launch-sglang.sh --model org/external-model --nnodes 2"),
                container_id=external_container_id,
            ).model_dump(mode="json"),
        )
        assert reported.status_code == 200
        template = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/external-model",
                "target_source": "huggingface",
            },
        ).json()
        assert template["reuse_mode"] == "ai_session"

        monkeypatch.setattr(
            client.app.state.runtime, "is_named_container_running", lambda _name: True
        )
        monkeypatch.setattr(client.app.state.runtime, "health", lambda _port, timeout: True)
        monkeypatch.setattr(
            client.app.state.runtime,
            "named_container_identity",
            lambda _name: {
                "container_id": external_container_id,
                "image": "example/inference:1",
                "running": True,
                "auto_remove": False,
            },
        )
        running_smoke_tests: list[tuple[int, str | None, str]] = []

        def smoke_running_service(
            port: int,
            *,
            model_name: str | None = None,
            endpoint: str = "completions",
            timeout: float = 30,
        ) -> str:
            del timeout
            running_smoke_tests.append((port, model_name, endpoint))
            return "org/external-model"

        monkeypatch.setattr(client.app.state.runtime, "smoke_test_service", smoke_running_service)
        base_request = {
            "model_id": "org/external-model",
            "target_source": "huggingface",
            "deployment_topology": "single_node",
        }
        running = client.post(
            "/api/v1/ai/deployment-templates/launch",
            json={**base_request, "client_request_id": "reuse-external-001"},
        )
        assert running.json()["reuse_method"] == "running_service"
        assert running_smoke_tests == [(19041, "org/external-model", "chat_completions")]

        prompts: list[str] = []

        def record_instruction(source_session_id: str, prompt: str):  # type: ignore[no-untyped-def]
            prompts.append(prompt)
            return client.app.state.ai_store.get(source_session_id)

        monkeypatch.setattr(
            client.app.state.runtime, "is_named_container_running", lambda _name: False
        )
        monkeypatch.setattr(client.app.state.ai_terminal, "send_instruction", record_instruction)
        restored = client.post(
            "/api/v1/ai/deployment-templates/launch",
            json={**base_request, "client_request_id": "reuse-external-002"},
        )
        assert restored.json()["reuse_method"] == "source_ai_session"
        assert "deployment-template.json" in prompts[0]
        assert "launch_command_template" in prompts[0]
        assert template["script_sha256"] not in prompts[0]

        Path(template["script_path"]).write_text("tampered\n", encoding="utf-8")
        monkeypatch.setattr(client.app.state.runtime, "list_images", lambda: [])
        fallback = client.post(
            "/api/v1/ai/deployment-templates/launch",
            json={**base_request, "client_request_id": "reuse-external-003"},
        )
        assert fallback.status_code == 200
        assert fallback.json()["reuse_method"] == "source_ai_session"
        assert fallback.json()["fallback_to_ai"] is True
        assert fallback.json()["session"]["id"] == session_id
        assert len(prompts) == 2
        assert "integrity check failed" in prompts[-1]


def test_managed_template_launch_is_idempotent_and_port_conflict_returns_to_original_ai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "org/managed-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        session_id = created["id"]
        capability = client.app.state.ai_terminal._capabilities[session_id]
        deployed = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/deploy",
            headers={"X-AI-Capability": capability},
            json={"image": "example/vllm:1", "port": 19042},
        ).json()
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
                deployed["id"], target_status, message="test transition"
            )
        monkeypatch.setattr(
            client.app.state.runtime,
            "container_metadata",
            lambda deployment: {
                "container_name": "uniform-managed-source",
                "container_id": "managed-id",
                "launch_command": "docker run example/vllm:1",
            },
        )
        reported = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result/managed",
            headers={"X-AI-Capability": capability},
            json={},
        )
        assert reported.status_code == 200
        template = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/managed-model",
                "target_source": "huggingface",
                "deployment_topology": "single_node",
            },
        ).json()
        assert template["reuse_mode"] == "managed"

        monkeypatch.setattr(
            client.app.state.runtime, "is_named_container_running", lambda _name: False
        )
        monkeypatch.setattr(client.app.state.runtime, "image_exists", lambda _image: True)
        monkeypatch.setattr(client.app.state.runtime, "ensure_port_available", lambda _port: None)
        monkeypatch.setattr(client.app.state.runtime, "list_images", lambda: [])
        request = {
            "client_request_id": "reuse-managed-001",
            "model_id": "org/managed-model",
            "target_source": "huggingface",
            "deployment_topology": "single_node",
        }
        launched = client.post("/api/v1/ai/deployment-templates/launch", json=request)
        duplicate = client.post("/api/v1/ai/deployment-templates/launch", json=request)
        assert launched.status_code == 200
        assert launched.json()["reuse_method"] == "managed_deployment"
        assert duplicate.json()["deployment"]["id"] == launched.json()["deployment"]["id"]

        conflict = dict(request, model_id="org/different-model")
        assert (
            client.post("/api/v1/ai/deployment-templates/launch", json=conflict).status_code == 409
        )

        def occupied(_port: int) -> None:
            raise RuntimeErrorBase("port is already in use")

        prompts: list[tuple[str, str]] = []

        def record_instruction(source_session_id: str, prompt: str):  # type: ignore[no-untyped-def]
            prompts.append((source_session_id, prompt))
            return client.app.state.ai_store.get(source_session_id)

        monkeypatch.setattr(client.app.state.ai_terminal, "send_instruction", record_instruction)
        monkeypatch.setattr(client.app.state.runtime, "ensure_port_available", occupied)
        fallback = client.post(
            "/api/v1/ai/deployment-templates/launch",
            json={**request, "client_request_id": "reuse-managed-002"},
        )
        assert fallback.status_code == 200
        assert fallback.json()["reuse_method"] == "source_ai_session"
        assert fallback.json()["fallback_to_ai"] is True
        assert fallback.json()["session"]["id"] == session_id
        assert prompts[0][0] == session_id
        assert "port is already in use" in prompts[0][1]


def test_historical_verified_result_is_backfilled_on_startup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    workspace = data_dir / "ai-sessions" / "historical-session"
    workspace.mkdir(parents=True)
    request = AISessionCreate(
        provider="codex",
        target_model_id="org/historical-model",
        target_source="modelscope",
    )
    session = store.create(request, str(workspace), session_id="historical-session")
    (workspace / "deployment-context.json").write_text(
        json.dumps(
            {
                "deployment": {
                    "target_model": {
                        "model_id": "org/historical-model",
                        "source": "modelscope",
                        "revision": None,
                    }
                },
                "deployment_topology": {"resolved": "single_node"},
            }
        ),
        encoding="utf-8",
    )
    store.upsert_result(session.id, deployment_result())
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_codex.chmod(0o700)

    app = create_app(
        Settings(data_dir=data_dir, worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/ai/deployment-templates/latest",
            params={
                "model_id": "org/historical-model",
                "target_source": "modelscope",
                "deployment_topology": "single_node",
            },
        )
        assert response.status_code == 200
        assert response.json()["id"] == session.id


def test_stale_reuse_request_can_be_reclaimed(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    assert store.begin_reuse_request("reuse-request-001", "a" * 64) is None
    with pytest.raises(AIDeploymentReusePending):
        store.begin_reuse_request("reuse-request-001", "a" * 64)
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    with database.connect() as connection:
        connection.execute(
            "UPDATE ai_deployment_reuse_requests SET updated_at = ?",
            (stale,),
        )
        connection.commit()
    assert store.begin_reuse_request("reuse-request-001", "a" * 64) is None
