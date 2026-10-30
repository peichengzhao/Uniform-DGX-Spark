from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SAMPLER_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "uniform_dgxspark"
    / "benchmark_reference"
    / "collect_unified_memory.py"
)
SPEC = importlib.util.spec_from_file_location("collect_unified_memory", SAMPLER_PATH)
assert SPEC and SPEC.loader
memory_sampler = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = memory_sampler
SPEC.loader.exec_module(memory_sampler)


def test_parse_meminfo_uses_available_memory_for_shared_pool() -> None:
    result = memory_sampler.parse_meminfo(
        "MemTotal:       100000 kB\n"
        "MemFree:         5000 kB\n"
        "MemAvailable:   25000 kB\n"
        "Cached:         10000 kB\n"
    )

    assert result == {
        "total_bytes": 102_400_000,
        "used_bytes": 76_800_000,
        "available_bytes": 25_600_000,
        "used_percent": 75.0,
    }


def test_parse_gpu_utilization_supports_multiple_devices() -> None:
    assert memory_sampler.parse_gpu_utilization("42\n87.5 %\n") == [42.0, 87.5]


def test_targets_from_context_discovers_only_online_ssh_nodes(tmp_path: Path) -> None:
    context = tmp_path / "benchmark-context.json"
    context.write_text(
        json.dumps(
            {
                "benchmark_target": {
                    "cluster": {
                        "items": [
                            {
                                "id": "local",
                                "online": True,
                                "connection": {"kind": "local"},
                            },
                            {
                                "id": "worker",
                                "label": "Worker",
                                "online": True,
                                "connection": {
                                    "kind": "ssh",
                                    "host": "10.0.22.2",
                                    "username": "dgx-spark",
                                    "port": 2222,
                                },
                            },
                            {
                                "id": "offline",
                                "online": False,
                                "connection": {
                                    "kind": "ssh",
                                    "host": "10.0.22.3",
                                    "username": "dgx-spark",
                                },
                            },
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    targets = memory_sampler.targets_from_context(context)

    assert [target.id for target in targets] == ["local", "worker"]
    assert targets[1].host == "10.0.22.2"
    assert targets[1].port == 2222


def test_sampler_reports_synchronized_cluster_and_per_node_peaks(monkeypatch) -> None:
    targets = [
        memory_sampler.Target(id="local", label="Head"),
        memory_sampler.Target(
            id="worker", label="Worker", host="10.0.22.2", username="dgx-spark"
        ),
    ]
    rounds = {
        "local": iter([(100, 60, [40]), (100, 70, [85])]),
        "worker": iter([(200, 120, [75]), (200, 110, [60])]),
    }

    def fake_read(target, _timeout):
        total, used, gpu = next(rounds[target.id])
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": total - used,
            "used_percent": used / total * 100,
            "gpu_utilization_percent": gpu,
        }

    monkeypatch.setattr(memory_sampler, "_read_target", fake_read)
    sampler = memory_sampler.UnifiedMemorySampler(targets, interval=1, timeout=5)
    sampler.sample()
    sampler.sample()

    document = sampler.document()

    assert document["cluster"]["peak_used_bytes"] == 180
    assert document["cluster"]["complete_sample_count"] == 2
    assert document["nodes"][0]["peak_used_bytes"] == 70
    assert document["nodes"][1]["peak_used_bytes"] == 120
    assert document["nodes"][0]["gpu_utilization_peak_by_device_percent"] == [85.0]
    assert document["nodes"][1]["gpu_utilization_peak_by_device_percent"] == [75.0]
    assert document["cluster"]["gpu_utilization_peak_by_device_percent"] == [85.0, 75.0]
    assert document["errors"] == []
