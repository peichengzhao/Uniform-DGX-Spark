from types import SimpleNamespace
from unittest.mock import Mock

from uniform_dgxspark.services.monitoring import MetricsService


def test_prometheus_parser_sums_labelled_series() -> None:
    text = """
# HELP ignored ignored
vllm:request_success_total{finished_reason="stop"} 2
vllm:request_success_total{finished_reason="length"} 3
vllm:generation_tokens_total 42
other_metric 99
"""

    parsed = MetricsService.parse_prometheus(
        text, {"vllm:request_success_total", "vllm:generation_tokens_total"}
    )

    assert parsed == {
        "vllm:request_success_total": 5,
        "vllm:generation_tokens_total": 42,
    }


def test_free_memory_metrics_uses_total_minus_available() -> None:
    memory = SimpleNamespace(total=1000, available=250, used=100, percent=10.0)

    assert MetricsService._free_memory_metrics(memory) == {
        "total_bytes": 1000,
        "used_bytes": 750,
        "available_bytes": 250,
        "percent": 75.0,
    }


def test_compute_process_memory_matches_nvidia_smi_model_rows() -> None:
    output = """3765, /usr/libexec/gnome-remote-desktop-daemon, 176
3604006, python3, 218
3604379, sglang::scheduler_TP0, 51134
"""

    metrics = MetricsService._parse_compute_processes(output)

    assert metrics["model_memory_used_mib"] == 51134
    assert metrics["model_memory_used_bytes"] == 51134 * 1024 * 1024
    assert metrics["model_process_count"] == 1
    assert metrics["model_processes"][0]["pid"] == 3604379


def test_sample_reports_model_share_of_unified_memory(
    tmp_path, monkeypatch
) -> None:
    service = MetricsService(tmp_path)
    gib = 1024**3
    memory = SimpleNamespace(total=128 * gib, available=64 * gib)
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.virtual_memory", lambda: memory
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.swap_memory",
        lambda: SimpleNamespace(total=0, used=0, percent=0),
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.disk_usage",
        lambda _path: SimpleNamespace(total=1, used=0, free=1, percent=0),
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.net_io_counters",
        lambda: SimpleNamespace(bytes_sent=0, bytes_recv=0),
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.cpu_percent",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.psutil.cpu_count",
        lambda **_kwargs: 1,
    )
    service._gpu_metrics = Mock(
        side_effect=lambda *, unified_memory_total_bytes: {
            "memory_total_bytes": unified_memory_total_bytes
        }
    )

    sample = service._sample()

    assert sample["gpu"]["memory_total_bytes"] == 128 * gib


def test_gpu_metrics_reports_nvidia_model_memory_as_unified_memory_share(
    monkeypatch,
) -> None:
    gib = 1024**3
    outputs = iter(
        [
            SimpleNamespace(returncode=0, stdout="NVIDIA GB10, 7, 48, 14\n", stderr=""),
            SimpleNamespace(
                returncode=0,
                stdout="3604379, sglang::scheduler_TP0, 51200\n",
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(
        "uniform_dgxspark.services.monitoring.subprocess.run",
        lambda *_args, **_kwargs: next(outputs),
    )

    metrics = MetricsService._gpu_metrics(unified_memory_total_bytes=128 * gib)

    assert metrics["memory_kind"] == "unified"
    assert metrics["memory_scope"] == "model_compute_process_allocation"
    assert metrics["memory_used_bytes"] == 50 * gib
    assert metrics["memory_total_bytes"] == 128 * gib
    assert metrics["model_memory_total_bytes"] == 128 * gib
    assert metrics["model_memory_percent"] == 39.1
