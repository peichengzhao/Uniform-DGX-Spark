from __future__ import annotations

from pathlib import Path

import pytest

from uniform_dgxspark.ai_store import AISessionStore
from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import (
    AIDeploymentResultCreate,
    AIHealthCheckResult,
    AIInferenceTestResult,
    AISessionCreate,
    DeploymentCreate,
    DeploymentParameters,
)
from uniform_dgxspark.services.model_targets import ModelTargetError, ModelTargetResolver
from uniform_dgxspark.state import DeploymentStatus
from uniform_dgxspark.store import DeploymentStore


def make_resolver(tmp_path: Path) -> tuple[DeploymentStore, AISessionStore, ModelTargetResolver]:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    deployments = DeploymentStore(database)
    sessions = AISessionStore(database)
    return deployments, sessions, ModelTargetResolver(deployments, sessions)


def mark_running(deployments: DeploymentStore, deployment_id: str) -> None:
    for status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        deployments.transition(deployment_id, status, message="test")


def test_resolves_running_deployment_to_loopback_snapshot(tmp_path: Path) -> None:
    deployments, _sessions, resolver = make_resolver(tmp_path)
    deployment = deployments.create(
        DeploymentCreate(
            model_id="org/model",
            display_name="Local model",
            parameters=DeploymentParameters(port=8123, served_model_name="served-model"),
        )
    )
    mark_running(deployments, deployment.id)

    target = resolver.resolve("deployment", deployment.id)

    assert target.label == "Local model"
    assert target.model_name == "served-model"
    assert target.service_url == "http://127.0.0.1:8123"
    assert target.snapshot == {
        "target_type": "deployment",
        "target_id": deployment.id,
        "status": "running",
        "model_id": "org/model",
        "revision": None,
        "served_model_name": "served-model",
        "port": 8123,
    }


def test_rejects_non_running_deployment(tmp_path: Path) -> None:
    deployments, _sessions, resolver = make_resolver(tmp_path)
    deployment = deployments.create(DeploymentCreate(model_id="org/stopped"))

    with pytest.raises(ModelTargetError, match="not running"):
        resolver.resolve("deployment", deployment.id)


def test_resolves_verified_ai_session_without_trusting_saved_service_url(
    tmp_path: Path,
) -> None:
    _deployments, sessions, resolver = make_resolver(tmp_path)
    session = sessions.create(
        AISessionCreate(
            provider="codex",
            task_type="deployment",
            display_name="AI model",
            target_model_id="org/ai-model",
            target_source="huggingface",
        ),
        str(tmp_path / "workspace"),
    )
    sessions.upsert_result(
        session.id,
        AIDeploymentResultCreate(
            engine="vllm",
            container_name="uniform-ai-test",
            image="engine:test",
            port=8234,
            service_url="http://untrusted.example:8234",
            launch_command="docker run engine:test",
            health_check=AIHealthCheckResult(url="http://127.0.0.1:8234/health", passed=True),
            inference_test=AIInferenceTestResult(
                url="http://127.0.0.1:8234/v1/models", passed=True
            ),
        ),
    )

    target = resolver.resolve("ai_session", session.id)

    assert target.model_name == "org/ai-model"
    assert target.service_url == "http://127.0.0.1:8234"
    assert target.snapshot["result_status"] == "verified"
    assert "service_url" not in target.snapshot
