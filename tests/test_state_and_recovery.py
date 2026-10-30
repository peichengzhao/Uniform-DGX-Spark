from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentCreate
from uniform_dgxspark.state import DeploymentStatus, InvalidTransition
from uniform_dgxspark.store import DeploymentStore


def make_store(tmp_path: Path) -> DeploymentStore:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    return DeploymentStore(database)


def test_state_machine_rejects_skipped_stage(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    deployment = store.create(DeploymentCreate(model_id="org/model"))

    with pytest.raises(InvalidTransition, match="cannot transition"):
        store.transition(
            deployment.id,
            DeploymentStatus.RUNNING,
            message="invalid jump",
        )


def test_incomplete_task_is_recovered_after_restart(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    deployment = store.create(DeploymentCreate(model_id="org/model"))
    store.transition(
        deployment.id,
        DeploymentStatus.PREFLIGHT,
        message="preflight started",
        progress=0.1,
    )

    restarted_app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(restarted_app) as client:
        health = client.get("/api/v1/health").json()
        recovered = client.get(f"/api/v1/deployments/{deployment.id}").json()
        events = client.get(f"/api/v1/deployments/{deployment.id}/events").json()

    assert health["recovered_tasks"] == 1
    assert recovered["status"] == "queued"
    assert recovered["progress"] == 0
    assert recovered["attempt"] == 1
    assert events["items"][-1]["event_type"] == "recovered"
    assert events["items"][-1]["from_status"] == "preflight"


def test_database_persists_terminal_task_across_restart(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, worker_enabled=False)
    first_app = create_app(settings)
    with TestClient(first_app) as client:
        created = client.post(
            "/api/v1/deployments", json={"model_id": "org/model"}
        ).json()
        client.post(f"/api/v1/deployments/{created['id']}/cancel")

    second_app = create_app(settings)
    with TestClient(second_app) as client:
        persisted = client.get(f"/api/v1/deployments/{created['id']}").json()
        health = client.get("/api/v1/health").json()

    assert persisted["status"] == "cancelled"
    assert health["recovered_tasks"] == 0
