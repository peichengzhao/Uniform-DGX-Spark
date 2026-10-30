from __future__ import annotations

from uniform_dgxspark.services.ai_terminal import (
    _BENCHMARK_HISTORY_COLUMNS,
    _BENCHMARK_HISTORY_V2_COLUMNS,
    AITerminalManager,
    _migrate_benchmark_history_table,
)


def test_v2_history_migration_drops_network_columns_and_splits_gpu() -> None:
    old_row = [str(index) for index in range(len(_BENCHMARK_HISTORY_V2_COLUMNS))]
    old_values = dict(zip(_BENCHMARK_HISTORY_V2_COLUMNS, old_row, strict=True))

    migrated = _migrate_benchmark_history_table(
        [_BENCHMARK_HISTORY_V2_COLUMNS, old_row]
    )

    assert len(migrated[0]) == 52
    assert all(len(row) == len(migrated[0]) for row in migrated)
    assert "Network RX Peak (bps)" not in migrated[0]
    assert "Network TX Peak (bps)" not in migrated[0]
    assert "RDMA RX Peak (bps)" not in migrated[0]
    assert "RDMA TX Peak (bps)" not in migrated[0]
    migrated_values = dict(zip(migrated[0], migrated[1], strict=True))
    assert migrated_values["GPU 0 Utilization Peak (%)"] == old_values[
        "GPU Utilization Peak (%)"
    ]
    assert migrated_values["GPU 1 Utilization Peak (%)"] == ""
    assert migrated_values["Speculative Acceptance Rate (%)"] == old_values[
        "Speculative Acceptance Rate (%)"
    ]
    assert migrated_values["Cluster Size"] == old_values["Cluster Size"]


def test_history_row_writes_two_gpu_peaks_without_network_columns() -> None:
    record = {
        "machine": "two-node",
        "model": "model",
        "engine": "sglang",
        "input_tokens": 128,
        "output_tokens": 32,
        "max_concurrency": 2,
        "requests": 2,
        "successful_requests": 2,
        "duration_seconds": 1.0,
        "total_input_tokens": 256,
        "total_generated_tokens": 64,
        "gpu_utilization_peak_by_device_percent": [91.236, 82.104],
        "network_rx_peak_bps": 123,
        "rdma_rx_peak_bps": 456,
    }

    row = AITerminalManager._benchmark_history_row(record, {})
    values = dict(zip(_BENCHMARK_HISTORY_COLUMNS, row, strict=True))

    assert values["GPU 0 Utilization Peak (%)"] == 91.24
    assert values["GPU 1 Utilization Peak (%)"] == 82.1
    assert len(row) == 52
