from __future__ import annotations

import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

import psutil


class MetricsService:
    MODEL_PROCESS_MARKERS = (
        "sglang::",
        "vllm",
        "tritonserver",
        "tensorrt",
        "llama",
        "ollama",
        "text-generation",
    )

    def __init__(
        self,
        data_dir: Path,
        *,
        interval_seconds: float = 2,
        history_size: int = 300,
    ) -> None:
        self.data_dir = data_dir
        self.interval_seconds = interval_seconds
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._sample_and_store()
        self._thread = threading.Thread(
            target=self._loop,
            name="uniform-metrics-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def snapshot(self, history: int = 0) -> dict[str, Any]:
        with self._lock:
            latest = dict(self.history[-1]) if self.history else self._sample()
            if history > 0:
                latest["history"] = list(self.history)[-history:]
        return latest

    def vllm_metrics(self, port: int) -> dict[str, float]:
        try:
            with self.local_http.open(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
                text = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return {}
        return self.parse_prometheus(
            text,
            {
                "vllm:prompt_tokens_total",
                "vllm:generation_tokens_total",
                "vllm:request_success_total",
                "vllm:time_to_first_token_seconds_count",
                "vllm:time_to_first_token_seconds_sum",
            },
        )

    def inference_telemetry(self, port: int) -> dict[str, Any]:
        try:
            with self.local_http.open(f"http://127.0.0.1:{port}/metrics", timeout=3) as response:
                text = response.read(5_000_000).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return {"available": False}
        names = {
            "vllm:request_success_total",
            "vllm:prompt_tokens_total",
            "vllm:generation_tokens_total",
            "vllm:spec_decode_num_drafts_total",
            "vllm:spec_decode_num_draft_tokens_total",
            "vllm:spec_decode_num_accepted_tokens_total",
        }
        values = self.parse_prometheus(text, names)
        accepted = values.get("vllm:spec_decode_num_accepted_tokens_total")
        drafted = values.get("vllm:spec_decode_num_draft_tokens_total")
        return {
            "available": bool(values),
            "request_success_total": values.get("vllm:request_success_total"),
            "prompt_tokens_total": values.get("vllm:prompt_tokens_total"),
            "generation_tokens_total": values.get("vllm:generation_tokens_total"),
            "speculative_drafts_total": values.get("vllm:spec_decode_num_drafts_total"),
            "speculative_draft_tokens_total": drafted,
            "speculative_accepted_tokens_total": accepted,
            "speculative_acceptance_rate_percent": (
                round(accepted / drafted * 100, 2) if accepted is not None and drafted else None
            ),
        }

    @staticmethod
    def parse_prometheus(text: str, names: set[str]) -> dict[str, float]:
        values = {name: 0.0 for name in names}
        found: set[str] = set()
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            metric, separator, raw_value = line.rpartition(" ")
            if not separator:
                continue
            name = metric.split("{", 1)[0]
            if name not in names:
                continue
            try:
                values[name] += float(raw_value)
                found.add(name)
            except ValueError:
                continue
        return {name: values[name] for name in found}

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._sample_and_store()

    def _sample_and_store(self) -> None:
        sample = self._sample()
        with self._lock:
            self.history.append(sample)

    @staticmethod
    def _free_memory_metrics(memory: Any) -> dict[str, int | float]:
        """Return the same memory accounting used by Linux ``free``.

        ``psutil`` has changed the meaning of ``used`` between releases.  Linux
        ``free`` defines used memory as total minus available, so derive both
        the byte count and percentage from those two stable kernel values.
        """
        total = max(0, int(memory.total))
        available = min(total, max(0, int(memory.available)))
        used = total - available
        return {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "percent": round(used / total * 100, 1) if total else 0.0,
        }

    def _sample(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        unified_memory = self._free_memory_metrics(memory)
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(self.data_dir if self.data_dir.exists() else Path("."))
        network = psutil.net_io_counters()
        return {
            "timestamp": time.time(),
            "cpu": {
                "percent": psutil.cpu_percent(interval=None),
                "logical_count": psutil.cpu_count(logical=True),
            },
            "unified_memory": unified_memory,
            "swap": {
                "total_bytes": swap.total,
                "used_bytes": swap.used,
                "percent": swap.percent,
            },
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "percent": disk.percent,
            },
            "network": {
                "bytes_sent": network.bytes_sent,
                "bytes_recv": network.bytes_recv,
            },
            "gpu": self._gpu_metrics(unified_memory_total_bytes=int(unified_memory["total_bytes"])),
        }

    @classmethod
    def _parse_compute_processes(cls, output: str) -> dict[str, Any]:
        processes: list[dict[str, Any]] = []
        for line in output.splitlines():
            parts = [part.strip() for part in line.split(",", 2)]
            if len(parts) != 3:
                continue
            try:
                pid = int(parts[0])
                used_mib = float(parts[2])
            except ValueError:
                continue
            processes.append(
                {
                    "pid": pid,
                    "process_name": parts[1],
                    "used_memory_mib": used_mib,
                    "used_memory_bytes": int(used_mib * 1024 * 1024),
                }
            )

        model_processes = [
            process
            for process in processes
            if any(
                marker in process["process_name"].lower() for marker in cls.MODEL_PROCESS_MARKERS
            )
        ]
        # Some runtimes leave the process name as plain ``python``.  In that
        # case the largest compute allocation is the closest value to the
        # model row shown by nvidia-smi, while small desktop CUDA contexts are
        # deliberately not added to it.
        if not model_processes and processes:
            model_processes = [max(processes, key=lambda item: item["used_memory_mib"])]

        used_mib = sum(process["used_memory_mib"] for process in model_processes)
        return {
            "compute_processes": processes,
            "compute_process_count": len(processes),
            "model_processes": model_processes,
            "model_process_count": len(model_processes),
            "model_memory_used_mib": used_mib,
            "model_memory_used_bytes": int(used_mib * 1024 * 1024),
            "memory_allocation_visible": bool(model_processes),
        }

    @classmethod
    def _gpu_metrics(cls, *, unified_memory_total_bytes: int = 0) -> dict[str, Any]:
        fields = [
            "name",
            "utilization.gpu",
            "temperature.gpu",
            "power.draw",
        ]
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    f"--query-gpu={','.join(fields)}",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False}
        if result.returncode != 0 or not result.stdout.strip():
            return {"available": False, "error": result.stderr.strip()}
        parts = [part.strip() for part in result.stdout.splitlines()[0].split(",")]

        try:
            process_result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-compute-apps=pid,process_name,used_gpu_memory",
                    "--format=csv,noheader,nounits",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            process_metrics = (
                cls._parse_compute_processes(process_result.stdout)
                if process_result.returncode == 0
                else cls._parse_compute_processes("")
            )
        except (OSError, subprocess.TimeoutExpired):
            process_metrics = cls._parse_compute_processes("")

        def number(index: int) -> float | None:
            try:
                return float(parts[index])
            except (IndexError, ValueError):
                return None

        model_memory_used_bytes = int(process_metrics["model_memory_used_bytes"])
        model_memory_percent = (
            round(model_memory_used_bytes / unified_memory_total_bytes * 100, 1)
            if process_metrics["memory_allocation_visible"] and unified_memory_total_bytes > 0
            else None
        )
        return {
            "available": True,
            "name": parts[0] if parts else "NVIDIA GPU",
            "utilization_percent": number(1),
            "temperature_c": number(2),
            "power_watts": number(3),
            "memory_kind": "unified",
            "memory_scope": "model_compute_process_allocation",
            "memory_total_bytes": unified_memory_total_bytes,
            "memory_used_bytes": model_memory_used_bytes,
            "model_memory_total_bytes": unified_memory_total_bytes,
            "model_memory_percent": model_memory_percent,
            "memory_note": (
                "Model unified-memory allocation reported by nvidia-smi compute processes"
            ),
            **process_metrics,
        }
