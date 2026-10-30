from __future__ import annotations

import json
from pathlib import Path

import pytest

from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import DeploymentCreate
from uniform_dgxspark.services.docker_runtime import (
    CommandResult,
    DockerCommandError,
    DockerRuntime,
)
from uniform_dgxspark.store import DeploymentStore


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        self.calls.append(arguments)
        if arguments[1] == "inspect":
            return CommandResult(1, "", "not found")
        return CommandResult(0, "container-id\n", "")


class ImageListRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        del timeout
        assert arguments == [
            "docker",
            "image",
            "ls",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
        rows = [
            {
                "ID": "sha256:abc123",
                "Repository": "nvcr.io/nvidia/vllm",
                "Tag": "26.07-py3",
                "Digest": "<none>",
                "Size": "33GB",
                "CreatedAt": "2026-08-01 10:00:00 +0000 UTC",
                "CreatedSince": "4 weeks ago",
                "Containers": "2",
            },
            {
                "ID": "sha256:def456",
                "Repository": "ubuntu",
                "Tag": "24.04",
                "Digest": "sha256:digest",
                "Size": "78MB",
                "CreatedAt": "2026-07-01 10:00:00 +0000 UTC",
                "CreatedSince": "2 months ago",
                "Containers": "0",
            },
        ]
        return CommandResult(0, "\n".join(json.dumps(row) for row in rows), "")


class InspectRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        del timeout
        if arguments[1:4] == ["inspect", "--format", "{{json .}}"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "Id": "sha256:container",
                        "Mounts": [
                            {
                                "Source": "/models/on host/model name",
                                "Destination": "/models/model",
                            }
                        ],
                    }
                ),
                "",
            )
        return CommandResult(0, "", "")


class LogRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        del timeout
        self.calls.append(arguments)
        return CommandResult(
            0,
            '2026-09-01T12:00:00Z INFO "POST /v1/chat/completions HTTP/1.1" 200 OK\n',
            "",
        )


class PortBindingRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        assert timeout == 5
        assert arguments[1:4] == [
            "inspect",
            "--format",
            "{{json .NetworkSettings.Ports}}",
        ]
        return CommandResult(
            0,
            json.dumps(
                {
                    "8000/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": "18190"},
                        {"HostIp": "::1", "HostPort": "18190"},
                    ]
                }
            ),
            "",
        )


class DiscoveryRunner:
    def run(self, arguments: list[str], *, timeout: int = 120) -> CommandResult:
        del timeout
        if arguments[1] == "ps":
            return CommandResult(0, "api-container\nworker-container\n", "")
        container_id = arguments[-1]
        rank = "0" if container_id == "api-container" else "1"
        return CommandResult(
            0,
            json.dumps(
                {
                    "Id": f"sha256:{container_id}",
                    "Name": f"/uniform-model-{rank}",
                    "Config": {
                        "Image": "engine:test",
                        "Labels": {
                            "uniform.session_id": "session-1234",
                            "uniform.deployment": "Recovered model",
                        },
                        "Entrypoint": ["vllm", "serve"],
                        "Cmd": [
                            "/model",
                            "--served-model-name=qwen-test",
                            "--node-rank",
                            rank,
                            "--port",
                            "8888",
                        ],
                    },
                    "NetworkSettings": {"Ports": {}},
                }
            ),
            "",
        )


def test_start_builds_argument_list_without_shell(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = DeploymentStore(database)
    deployment = store.create(
        DeploymentCreate.model_validate(
            {
                "model_id": "org/model",
                "parameters": {
                    "port": 18080,
                    "local_model_path": str(tmp_path),
                    "served_model_name": "test-model",
                    "max_model_len": 1024,
                    "gpu_memory_utilization": 0.5,
                },
            }
        )
    )
    runner = RecordingRunner()
    runtime = DockerRuntime(runner=runner)

    runtime.start(deployment, tmp_path)

    docker_run = runner.calls[-1]
    assert docker_run[:3] == ["docker", "run", "-d"]
    assert "127.0.0.1:18080:8000" in docker_run
    assert "--gpus" in docker_run
    assert "all" in docker_run
    assert "--served-model-name" in docker_run
    assert "test-model" in docker_run
    assert "--max-model-len" in docker_run


def test_list_local_images_and_detect_vllm_candidates() -> None:
    images = DockerRuntime(runner=ImageListRunner()).list_images()

    assert len(images) == 2
    assert images[0].reference == "nvcr.io/nvidia/vllm:26.07-py3"
    assert images[0].vllm_candidate is True
    assert images[0].containers == 2
    assert images[1].reference == "ubuntu:24.04"
    assert images[1].vllm_candidate is False


def test_container_metadata_returns_reproducible_launch_command(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    deployment = DeploymentStore(database).create(
        DeploymentCreate.model_validate(
            {"model_id": "org/model", "parameters": {"port": 18080}}
        )
    )

    metadata = DockerRuntime(runner=InspectRunner()).container_metadata(deployment)

    assert metadata["container_id"] == "sha256:container"
    assert metadata["container_name"].startswith("uniform-vllm-")
    assert "src=/models/on host/model name" in str(metadata["launch_command"])
    assert "127.0.0.1:18080:8000" in str(metadata["launch_command"])


def test_logs_for_reported_container_validates_name_and_adds_timestamps() -> None:
    runner = LogRunner()
    runtime = DockerRuntime(runner=runner)

    logs = runtime.logs_for_container("/tutorial-vllm", tail=1000)

    assert "POST /v1/chat/completions" in logs
    assert runner.calls == [
        ["docker", "logs", "--timestamps", "--tail", "1000", "tutorial-vllm"]
    ]
    with pytest.raises(DockerCommandError, match="invalid container name"):
        runtime.logs_for_container("../../unrelated container")


def test_container_host_bindings_return_only_matching_host_port() -> None:
    runtime = DockerRuntime(runner=PortBindingRunner())

    assert runtime.container_host_bindings("model-api", 18190) == ["127.0.0.1", "::1"]
    assert runtime.container_host_bindings("model-api", 18191) == []
    assert runtime.container_host_bindings("../../invalid", 18190) == []


def test_discovers_only_api_rank_from_uniform_labeled_containers() -> None:
    services = DockerRuntime(runner=DiscoveryRunner()).discover_ai_services()

    assert len(services) == 1
    assert services[0].session_id == "session-1234"
    assert services[0].container_name == "uniform-model-0"
    assert services[0].port == 8888
    assert services[0].model_name == "qwen-test"
    assert services[0].engine == "vllm"
