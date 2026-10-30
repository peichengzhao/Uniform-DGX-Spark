#!/usr/bin/env python3
"""Continuously sample DGX Spark unified memory and GPU utilization.

The sampler is intentionally self-contained because the benchmark reference directory is copied
into every AI benchmark workspace.  It measures the shared CPU/GPU memory pool from
``/proc/meminfo`` and checkpoints its JSON output after every sample.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    host: str | None = None
    username: str | None = None
    port: int = 22

    @property
    def is_local(self) -> bool:
        return self.host is None


def parse_meminfo(text: str) -> dict[str, int | float]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        fields = raw.strip().split()
        if not fields or not fields[0].isdigit():
            continue
        multiplier = 1024 if len(fields) > 1 and fields[1].lower() == "kb" else 1
        values[key] = int(fields[0]) * multiplier
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    if total <= 0 or available < 0 or available > total:
        raise ValueError("/proc/meminfo does not contain valid MemTotal and MemAvailable values")
    used = total - available
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(used / total * 100, 2),
    }


def parse_gpu_utilization(text: str) -> list[float]:
    values: list[float] = []
    for line in text.splitlines():
        raw = line.strip().removesuffix("%").strip()
        if not raw or raw.lower() in {"n/a", "[not supported]"}:
            continue
        value = float(raw)
        if value < 0 or value > 100:
            raise ValueError(f"invalid GPU utilization value: {value}")
        values.append(value)
    if not values:
        raise ValueError("nvidia-smi returned no GPU utilization values")
    return values


def _ssh_prefix(target: Target, timeout: float) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={max(1, int(timeout))}",
        "-o",
        "ConnectionAttempts=1",
        "-p",
        str(target.port),
        f"{target.username}@{target.host}",
    ]


def _read_gpu_utilization(target: Target, timeout: float) -> list[float]:
    probe = [
        "nvidia-smi",
        "--query-gpu=utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    command = probe if target.is_local else [*_ssh_prefix(target, timeout), *probe]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 2,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()[-1:] or ["GPU utilization probe failed"]
        raise RuntimeError(detail[0][:500])
    return parse_gpu_utilization(completed.stdout)


def _read_target(target: Target, timeout: float) -> dict[str, Any]:
    if target.is_local:
        reading: dict[str, Any] = parse_meminfo(
            Path("/proc/meminfo").read_text(encoding="utf-8")
        )
    else:
        command = [*_ssh_prefix(target, timeout), "cat", "/proc/meminfo"]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()[-1:] or ["SSH memory probe failed"]
            raise RuntimeError(detail[0][:500])
        reading = parse_meminfo(completed.stdout)
    try:
        reading["gpu_utilization_percent"] = _read_gpu_utilization(target, timeout)
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as error:
        reading["gpu_utilization_error"] = str(error)
    return reading


def targets_from_context(path: Path | None) -> list[Target]:
    targets = [Target(id="local", label=socket.gethostname())]
    if path is None or not path.is_file():
        return targets
    payload = json.loads(path.read_text(encoding="utf-8"))
    cluster = (payload.get("benchmark_target") or {}).get("cluster") or {}
    for item in cluster.get("items") or []:
        connection = item.get("connection") or {}
        if item.get("id") == "local" or connection.get("kind") != "ssh":
            continue
        if not item.get("online", False):
            continue
        host = str(connection.get("host") or "").strip()
        username = str(connection.get("username") or "").strip()
        if not host or not username:
            continue
        targets.append(
            Target(
                id=str(item.get("id") or host),
                label=str(item.get("label") or item.get("hostname") or host),
                host=host,
                username=username,
                port=int(connection.get("port") or 22),
            )
        )
    return targets


def _target_metadata(target: Target) -> dict[str, Any]:
    connection: dict[str, Any] = {"kind": "local"}
    if not target.is_local:
        connection = {
            "kind": "ssh",
            "host": target.host,
            "username": target.username,
            "port": target.port,
        }
    return {"id": target.id, "label": target.label, "connection": connection}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


class UnifiedMemorySampler:
    def __init__(self, targets: list[Target], *, interval: float, timeout: float) -> None:
        if not targets:
            raise ValueError("at least one sampling target is required")
        self.targets = targets
        self.interval = interval
        self.timeout = timeout
        self.started_at = _timestamp()
        self.samples: list[dict[str, Any]] = []
        self.errors: list[dict[str, str]] = []

    def sample(self) -> None:
        readings: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(self.targets)) as executor:
            futures = {
                executor.submit(_read_target, target, self.timeout): target
                for target in self.targets
            }
            for future in as_completed(futures):
                target = futures[future]
                try:
                    readings[target.id] = future.result()
                    gpu_error = readings[target.id].pop("gpu_utilization_error", None)
                    if gpu_error:
                        self.errors.append(
                            {
                                "timestamp": _timestamp(),
                                "node_id": target.id,
                                "metric": "gpu_utilization",
                                "error": gpu_error,
                            }
                        )
                except Exception as error:  # noqa: BLE001 - local/SSH probe boundary
                    self.errors.append(
                        {"timestamp": _timestamp(), "node_id": target.id, "error": str(error)}
                    )
        cluster = None
        if len(readings) == len(self.targets):
            total = sum(int(value["total_bytes"]) for value in readings.values())
            used = sum(int(value["used_bytes"]) for value in readings.values())
            available = sum(int(value["available_bytes"]) for value in readings.values())
            cluster = {
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": available,
                "used_percent": round(used / total * 100, 2) if total else 0,
            }
        self.samples.append({"timestamp": _timestamp(), "nodes": readings, "cluster": cluster})

    def document(self) -> dict[str, Any]:
        node_summaries: list[dict[str, Any]] = []
        for target in self.targets:
            readings = [
                sample["nodes"][target.id]
                for sample in self.samples
                if target.id in sample["nodes"]
            ]
            summary = _target_metadata(target)
            summary["sample_count"] = len(readings)
            if readings:
                peak = max(readings, key=lambda value: int(value["used_bytes"]))
                current = readings[-1]
                summary.update(
                    total_bytes=int(current["total_bytes"]),
                    current_used_bytes=int(current["used_bytes"]),
                    current_available_bytes=int(current["available_bytes"]),
                    current_used_percent=float(current["used_percent"]),
                    peak_used_bytes=int(peak["used_bytes"]),
                    peak_used_percent=float(peak["used_percent"]),
                    minimum_available_bytes=min(
                        int(value["available_bytes"]) for value in readings
                    ),
                )
                gpu_device_count = max(
                    (len(value.get("gpu_utilization_percent") or []) for value in readings),
                    default=0,
                )
                summary["gpu_utilization_peak_by_device_percent"] = [
                    max(
                        float(value["gpu_utilization_percent"][device_index])
                        for value in readings
                        if len(value.get("gpu_utilization_percent") or []) > device_index
                    )
                    for device_index in range(gpu_device_count)
                ]
            node_summaries.append(summary)

        cluster_readings = [sample["cluster"] for sample in self.samples if sample["cluster"]]
        cluster_summary: dict[str, Any] = {
            "node_count": len(self.targets),
            "complete_sample_count": len(cluster_readings),
        }
        if cluster_readings:
            peak = max(cluster_readings, key=lambda value: int(value["used_bytes"]))
            current = cluster_readings[-1]
            cluster_summary.update(
                total_bytes=int(current["total_bytes"]),
                current_used_bytes=int(current["used_bytes"]),
                current_available_bytes=int(current["available_bytes"]),
                current_used_percent=float(current["used_percent"]),
                peak_used_bytes=int(peak["used_bytes"]),
                peak_used_percent=float(peak["used_percent"]),
                minimum_available_bytes=min(
                    int(value["available_bytes"]) for value in cluster_readings
                ),
            )
        cluster_gpu_peaks = [
            peak
            for node in node_summaries
            for peak in node.get("gpu_utilization_peak_by_device_percent", [])
        ]
        cluster_summary["gpu_utilization_peak_by_device_percent"] = cluster_gpu_peaks
        return {
            "schema_version": 2,
            "metrics": ["dgx_spark_unified_memory", "gpu_utilization_by_device"],
            "measurement_scope": "shared_cpu_gpu_memory_pool_and_each_gpu_device",
            "source": [
                "/proc/meminfo: MemTotal - MemAvailable",
                "nvidia-smi: utilization.gpu",
            ],
            "started_at": self.started_at,
            "ended_at": _timestamp(),
            "interval_seconds": self.interval,
            "nodes": node_summaries,
            "cluster": cluster_summary,
            "samples": self.samples,
            "errors": self.errors,
        }


def _write_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def run_sampler(
    *,
    targets: list[Target],
    output: Path,
    interval: float,
    timeout: float,
    duration: float | None,
) -> dict[str, Any]:
    sampler = UnifiedMemorySampler(targets, interval=interval, timeout=timeout)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    previous_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
    started = time.monotonic()
    try:
        while not stop.is_set():
            sampler.sample()
            _write_atomic(output, sampler.document())
            if duration is not None:
                remaining = duration - (time.monotonic() - started)
                if remaining <= 0:
                    break
                stop.wait(min(interval, remaining))
            else:
                stop.wait(interval)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    document = sampler.document()
    _write_atomic(output, document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="checkpoint JSON path")
    parser.add_argument(
        "--context",
        type=Path,
        default=Path("benchmark-context.json"),
        help="benchmark context used to discover online SSH nodes",
    )
    parser.add_argument("--local-only", action="store_true", help="ignore context SSH nodes")
    parser.add_argument("--interval", type=float, default=1.0, help="sampling interval in seconds")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-node probe timeout")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after this many seconds; otherwise run until SIGINT/SIGTERM",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        raise SystemExit("--interval and --timeout must be greater than zero")
    if args.duration is not None and args.duration < 0:
        raise SystemExit("--duration cannot be negative")
    targets = targets_from_context(None if args.local_only else args.context)
    document = run_sampler(
        targets=targets,
        output=args.output,
        interval=args.interval,
        timeout=args.timeout,
        duration=args.duration,
    )
    print(json.dumps({"output": str(args.output), "cluster": document["cluster"]}))
    return 0 if document["cluster"].get("complete_sample_count", 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
