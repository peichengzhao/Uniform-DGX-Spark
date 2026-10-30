from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.node_store import SSHNodeAlreadyExists, SSHNodeStore
from uniform_dgxspark.schemas import SSHNodeCreate, SSHNodeRead
from uniform_dgxspark.services import cluster_monitoring, node_probe
from uniform_dgxspark.services.cluster_monitoring import (
    ClusterMonitoringService,
    SSHProbeRunner,
)
from uniform_dgxspark.services.node_probe import _link_status


def sample(
    *,
    hostname: str,
    interface: str,
    address: str,
    observed_at: float,
    rx_bytes: int,
    tx_bytes: int,
) -> dict[str, Any]:
    return {
        "observed_at": observed_at,
        "hostname": hostname,
        "architecture": "aarch64",
        "uptime_seconds": 100,
        "cpu": {
            "logical_count": 20,
            "utilization_percent": 37.5,
            "load_average": [1, 2, 3],
        },
        "memory": {
            "total_bytes": 128_000,
            "used_bytes": 64_000,
            "available_bytes": 64_000,
            "percent": 50,
        },
        "disk": {"total_bytes": 1, "used_bytes": 0, "free_bytes": 1, "percent": 0},
        "gpu": {
            "available": True,
            "name": "NVIDIA GB10",
            "utilization_percent": 42,
            "memory_kind": "unified",
            "memory_total_bytes": 128_000,
            "memory_used_bytes": 64_000,
            "memory_available_bytes": 64_000,
            "memory_allocation_visible": False,
        },
        "addresses": [
            {"interface": interface, "operstate": "UP", "addresses": [address]}
        ],
        "rdma_interfaces": [
            {
                "rdma_device": f"roce-{interface}",
                "rdma_port": 1,
                "state": "ACTIVE",
                "physical_state": "LINK_UP",
                "interface": interface,
                "operstate": "UP",
                "addresses": [address],
                "mtu": 9000,
                "speed_mbps": 200_000,
                "driver": "mlx5_core",
                "rx_bytes": rx_bytes,
                "tx_bytes": tx_bytes,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_dropped": 0,
                "tx_dropped": 0,
            }
        ],
        "containers": [
            {
                "id": "abc",
                "name": f"{hostname}-worker",
                "image": "inference:dynamic",
                "state": "running",
                "status": "Up",
                "ports": "",
                "network": "host",
                "cpu_percent": "12.5%",
                "memory_usage": "8GiB / 128GiB",
                "memory_percent": "6.25%",
                "net_io": "1GB / 2GB",
                "block_io": "3GB / 4GB",
                "pids": "42",
            }
        ],
    }


class FakeRunner:
    def __init__(self) -> None:
        self.iteration = 0

    def collect(self, node: SSHNodeRead) -> dict[str, Any]:
        self.iteration += 1
        return sample(
            hostname=f"remote-dynamic-{self.iteration}",
            interface="cx7-renamed-42",
            address=f"10.99.0.{26 + self.iteration}",
            observed_at=100 + self.iteration * 5,
            rx_bytes=1_000 + self.iteration * 1_000,
            tx_bytes=2_000 + self.iteration * 2_000,
        )


def test_link_status_distinguishes_physical_carrier_from_network_configuration() -> None:
    physical_only = _link_status("ACTIVE", "LINK_UP", "UP", [])
    link_local_only = _link_status("ACTIVE", "LINK_UP", "UP", ["fe80::1"])
    configured = _link_status("ACTIVE", "LINK_UP", "UP", ["10.0.22.1"])
    down = _link_status("DOWN", "DISABLED", "DOWN", [])

    assert physical_only == {
        "physical_link_up": True,
        "network_configured": False,
        "peer_reachable": None,
        "connection_status": "physical_only",
    }
    assert link_local_only["connection_status"] == "physical_only"
    assert configured["connection_status"] == "network_configured"
    assert configured["network_configured"] is True
    assert down["connection_status"] == "down"


def test_node_store_and_dynamic_cluster_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = SSHNodeStore(database)
    node = store.create(
        SSHNodeCreate(label="peer", host="spark-peer.local", username="monitor")
    )
    with pytest.raises(SSHNodeAlreadyExists):
        store.create(SSHNodeCreate(host="spark-peer.local", username="monitor"))
    monkeypatch.setattr(
        cluster_monitoring,
        "collect_local",
        lambda: sample(
            hostname="local-dynamic",
            interface="rdma-local-new-name",
            address="10.99.0.26",
            observed_at=100,
            rx_bytes=10,
            tx_bytes=20,
        ),
    )
    service = ClusterMonitoringService(store, runner=FakeRunner())

    first = service.refresh()
    second = service.refresh()

    assert first["total"] == 2
    remote = next(item for item in second["items"] if item["id"] == node.id)
    assert remote["hostname"] == "remote-dynamic-2"
    assert remote["addresses"][0]["addresses"] == ["10.99.0.28"]
    assert remote["rdma_interfaces"][0]["interface"] == "cx7-renamed-42"
    assert remote["rdma_interfaces"][0]["speed_mbps"] == 200_000
    assert remote["rdma_interfaces"][0]["rx_bps"] == 1600
    assert remote["containers"][0]["name"] == "remote-dynamic-2-worker"
    assert remote["containers"][0]["cpu_percent"] == "12.5%"
    assert remote["cpu"]["utilization_percent"] == 37.5
    assert remote["gpu"]["memory_kind"] == "unified"


def test_node_probe_reports_unified_gpu_memory_and_container_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = {
        "total_bytes": 128 * 1024**3,
        "used_bytes": 32 * 1024**3,
        "available_bytes": 96 * 1024**3,
        "percent": 25,
    }
    gpu_fields = (
        "--query-gpu=name,uuid,utilization.gpu,utilization.memory,temperature.gpu,"
        "power.draw,memory.total,memory.used,memory.free"
    )

    def fake_run(arguments: list[str], timeout: int = 5) -> str:
        del timeout
        if arguments[:2] == ["nvidia-smi", gpu_fields]:
            return "NVIDIA GB10, GPU-test, 42, 7, 51, 14.5, [N/A], [N/A], [N/A]\n"
        if arguments[:2] == [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_gpu_memory",
        ]:
            return "120, python3, 128\n121, sglang::scheduler_TP0, 51134\n"
        if arguments[:2] == ["docker", "ps"]:
            return (
                '{"ID":"abcdef1234567890","Names":"worker","Image":"engine:test",'
                '"State":"running","Status":"Up 1 hour","Ports":"","Networks":"host"}\n'
            )
        if arguments[:2] == ["docker", "stats"]:
            return (
                '{"ID":"abcdef123456","Name":"worker","CPUPerc":"25.00%",'
                '"MemUsage":"16GiB / 128GiB","MemPerc":"12.50%",'
                '"NetIO":"1GB / 2GB","BlockIO":"3GB / 4GB","PIDs":"55"}\n'
            )
        return ""

    monkeypatch.setattr(node_probe, "_run", fake_run)
    gpu = node_probe._gpu(memory)
    containers = node_probe._containers()

    assert gpu["memory_kind"] == "unified"
    assert gpu["memory_allocation_visible"] is True
    assert gpu["model_memory_used_mib"] == 51134
    assert gpu["memory_used_bytes"] == 51134 * 1024**2
    assert gpu["memory_scope"] == "model_compute_process_allocation"
    assert gpu["model_memory_total_bytes"] == 128 * 1024**3
    assert gpu["model_memory_percent"] == 39.0
    assert gpu["model_process_count"] == 1
    assert gpu["memory_controller_utilization_percent"] == 7
    assert containers[0]["cpu_percent"] == "25.00%"
    assert containers[0]["memory_usage"] == "16GiB / 128GiB"


def test_ssh_probe_uses_fixed_read_only_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("print('{}')\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["arguments"] = arguments
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(arguments, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    node = SSHNodeRead(
        id="node",
        label=None,
        host="spark.local",
        port=2222,
        username="monitor",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )

    SSHProbeRunner(probe).collect(node)

    assert captured["arguments"][-3:] == ["monitor@spark.local", "python3", "-"]
    assert "StrictHostKeyChecking=yes" in captured["arguments"]
    assert "PasswordAuthentication=no" in captured["arguments"]
    assert captured["input"] == "print('{}')\n"


def test_system_node_api_uses_live_probe(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(app) as client:
        client.app.state.cluster_monitoring.stop()
        client.app.state.cluster_monitoring.runner = FakeRunner()
        invalid = client.post(
            "/api/v1/system/nodes",
            json={"host": "-oProxyCommand=bad", "username": "monitor"},
        )
        assert invalid.status_code == 422
        created = client.post(
            "/api/v1/system/nodes",
            json={
                "label": "Second Spark",
                "host": "spark-peer.local",
                "username": "monitor",
                "port": 22,
            },
        )
        assert created.status_code == 201

        refreshed = client.post("/api/v1/system/nodes/refresh")
        assert refreshed.status_code == 200
        remote = next(
            item for item in refreshed.json()["items"] if item["id"] != "local"
        )
        assert remote["online"] is True
        assert remote["hostname"] == "remote-dynamic-1"

        removed = client.delete(f"/api/v1/system/nodes/{created.json()['id']}")
        assert removed.json() == {"removed": True}
