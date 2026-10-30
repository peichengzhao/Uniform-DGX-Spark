from __future__ import annotations

import json
import math
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..ai_store import AISessionStore
from ..benchmark_store import BenchmarkStore
from ..schemas import BenchmarkCreate, BenchmarkMetrics, BenchmarkRead
from ..store import DeploymentStore
from .model_targets import ModelTargetError, ModelTargetResolver


class BenchmarkTargetError(RuntimeError):
    pass


class BenchmarkBusy(RuntimeError):
    pass


@dataclass(slots=True)
class _RequestResult:
    latency_seconds: float
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


class BenchmarkManager:
    def __init__(
        self,
        store: BenchmarkStore,
        deployments: DeploymentStore,
        ai_sessions: AISessionStore,
    ) -> None:
        self.store = store
        self.deployments = deployments
        self.ai_sessions = ai_sessions
        self.target_resolver = ModelTargetResolver(deployments, ai_sessions)
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def create(self, request: BenchmarkCreate) -> BenchmarkRead:
        target_label, model_name, service_url = self._resolve_target(request)
        with self._lock:
            if self._active:
                raise BenchmarkBusy("another benchmark is already running")
            benchmark = self.store.create(
                request,
                target_label=target_label,
                model_name=model_name,
                service_url=service_url,
            )
            cancel_event = threading.Event()
            self._active[benchmark.id] = cancel_event
        threading.Thread(
            target=self._run,
            args=(benchmark.id, cancel_event),
            name=f"benchmark-{benchmark.id[:8]}",
            daemon=True,
        ).start()
        return benchmark

    def cancel(self, benchmark_id: str) -> BenchmarkRead:
        benchmark = self.store.get(benchmark_id)
        with self._lock:
            event = self._active.get(benchmark_id)
        if event:
            event.set()
        if benchmark.status in {"queued", "running"}:
            return self.store.cancel(benchmark_id)
        return benchmark

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._active.items())
        for benchmark_id, event in items:
            event.set()
            try:
                self.store.cancel(benchmark_id)
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass

    def _resolve_target(self, request: BenchmarkCreate) -> tuple[str, str, str]:
        try:
            target = self.target_resolver.resolve(request.target_type, request.target_id)
        except ModelTargetError as error:
            raise BenchmarkTargetError(str(error)) from error
        return target.label, target.model_name, target.service_url

    def _run(self, benchmark_id: str, cancel_event: threading.Event) -> None:
        try:
            benchmark = self.store.mark_running(benchmark_id)
            parameters = benchmark.parameters
            model_name = self._discover_model(
                benchmark.service_url,
                benchmark.model_name,
                parameters.timeout_seconds,
            )
            prompt = "benchmark " * parameters.input_tokens
            for _ in range(parameters.warmup_requests):
                if cancel_event.is_set():
                    self.store.cancel(benchmark_id)
                    return
                self._request(
                    benchmark.service_url,
                    model_name,
                    prompt,
                    parameters,
                )

            started = time.monotonic()
            tasks: queue.Queue[int] = queue.Queue()
            results: queue.Queue[_RequestResult] = queue.Queue()
            for index in range(parameters.total_requests):
                tasks.put(index)

            def worker() -> None:
                while not cancel_event.is_set():
                    try:
                        tasks.get_nowait()
                    except queue.Empty:
                        return
                    results.put(
                        self._request(
                            benchmark.service_url,
                            model_name,
                            prompt,
                            parameters,
                        )
                    )

            workers = [
                threading.Thread(
                    target=worker,
                    name=f"benchmark-request-{benchmark_id[:8]}-{index}",
                    daemon=True,
                )
                for index in range(min(parameters.concurrency, parameters.total_requests))
            ]
            for worker_thread in workers:
                worker_thread.start()

            collected: list[_RequestResult] = []
            successful = 0
            failed = 0
            while len(collected) < parameters.total_requests and not cancel_event.is_set():
                try:
                    result = results.get(timeout=0.2)
                except queue.Empty:
                    continue
                collected.append(result)
                if result.error:
                    failed += 1
                else:
                    successful += 1
                self.store.update_progress(
                    benchmark_id,
                    completed_requests=len(collected),
                    successful_requests=successful,
                    failed_requests=failed,
                    total_requests=parameters.total_requests,
                )

            if cancel_event.is_set():
                self.store.cancel(benchmark_id)
                return
            duration = max(0.000001, time.monotonic() - started)
            if not successful:
                errors = [result.error for result in collected if result.error]
                self.store.fail(benchmark_id, errors[0] if errors else "all requests failed")
                return
            self.store.complete(
                benchmark_id,
                self._metrics(collected, duration, parameters.total_requests),
            )
        except Exception as error:  # noqa: BLE001 - background job boundary
            try:
                if self.store.get(benchmark_id).status not in {"cancelled", "completed"}:
                    self.store.fail(benchmark_id, str(error))
            except Exception:  # noqa: BLE001 - preserve original worker failure
                pass
        finally:
            with self._lock:
                self._active.pop(benchmark_id, None)

    @staticmethod
    def _discover_model(service_url: str, fallback: str, timeout: int) -> str:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"{service_url}/v1/models", timeout=min(timeout, 15)) as response:
                payload = json.loads(response.read(1_000_000))
            models = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(models, list) and models and models[0].get("id"):
                return str(models[0]["id"])
        except (OSError, ValueError, urllib.error.URLError):
            pass
        return fallback

    @staticmethod
    def _request(
        service_url: str,
        model_name: str,
        prompt: str,
        parameters: BenchmarkCreate,
    ) -> _RequestResult:
        if parameters.endpoint == "chat_completions":
            path = "/v1/chat/completions"
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": parameters.output_tokens,
                "temperature": 0,
                "stream": False,
            }
        else:
            path = "/v1/completions"
            payload = {
                "model": model_name,
                "prompt": prompt,
                "max_tokens": parameters.output_tokens,
                "temperature": 0,
                "stream": False,
            }
        request = urllib.request.Request(
            f"{service_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=parameters.timeout_seconds) as response:
                body = json.loads(response.read(20_000_000))
            if not isinstance(body, dict) or not body.get("choices"):
                raise ValueError("response does not contain choices")
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            return _RequestResult(
                latency_seconds=time.monotonic() - started,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            )
        except urllib.error.HTTPError as error:
            message = f"HTTP {error.code} {error.reason}"
        except (OSError, ValueError, urllib.error.URLError) as error:
            message = str(error)
        return _RequestResult(
            latency_seconds=time.monotonic() - started,
            prompt_tokens=0,
            completion_tokens=0,
            error=message[:500],
        )

    @staticmethod
    def _metrics(
        results: list[_RequestResult], duration: float, total_requests: int
    ) -> BenchmarkMetrics:
        successful = [result for result in results if not result.error]
        latencies = sorted(result.latency_seconds * 1000 for result in successful)
        prompt_tokens = sum(result.prompt_tokens for result in successful)
        completion_tokens = sum(result.completion_tokens for result in successful)
        errors = list(dict.fromkeys(result.error for result in results if result.error))[:10]

        def percentile(value: float) -> float:
            if not latencies:
                return 0.0
            position = (len(latencies) - 1) * value
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return latencies[lower]
            return latencies[lower] + (latencies[upper] - latencies[lower]) * (position - lower)

        count = len(successful)
        return BenchmarkMetrics(
            duration_seconds=round(duration, 3),
            success_rate=round(count / total_requests * 100, 2),
            request_throughput=round(count / duration, 3),
            output_token_throughput=round(completion_tokens / duration, 3),
            total_token_throughput=round((prompt_tokens + completion_tokens) / duration, 3),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            average_prompt_tokens=round(prompt_tokens / count, 2),
            average_completion_tokens=round(completion_tokens / count, 2),
            latency_average_ms=round(sum(latencies) / count, 2),
            latency_p50_ms=round(percentile(0.50), 2),
            latency_p95_ms=round(percentile(0.95), 2),
            latency_p99_ms=round(percentile(0.99), 2),
            errors=[str(error) for error in errors],
        )
