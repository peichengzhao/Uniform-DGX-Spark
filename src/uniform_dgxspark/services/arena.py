from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ai_store import AISessionStore
from ..arena_store import ArenaRunStore
from ..schemas import (
    ArenaLeaderboardDetail,
    ArenaLeaderboardEntry,
    ArenaLeaderboardList,
    ArenaLeaderboardPreset,
    ArenaLeaderboardReference,
    ArenaMetricRead,
    ArenaProgressRead,
    ArenaResourceNodeSummary,
    ArenaResourceSummary,
    ArenaResultSummary,
    ArenaRunCreate,
    ArenaRunRead,
    ArenaRuntimeProfile,
    ArenaRuntimeRecipe,
    ArenaSpeculativeConfig,
    ArenaSpeculativeMetrics,
)
from ..store import DeploymentStore
from .docker_runtime import DockerRuntime
from .model_targets import ModelTargetError, ModelTargetResolver


class ArenaTargetError(RuntimeError):
    pass


class ArenaBusy(RuntimeError):
    pass


class ArenaLeaderboardError(RuntimeError):
    pass


_BEARER_CREDENTIAL = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_NAMED_CREDENTIAL = re.compile(
    r"(?i)(\b(?:[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|PASSWORD|PASSWD|SECRET)"
    r"[A-Z0-9_]*|authorization)\b\s*[:=]\s*)([\"']?)([^\s,;\"'}]+)([\"']?)"
)


def prepare_log_for_analysis(text: str, *, max_chars: int = 100_000) -> tuple[str, bool]:
    """Redact common credentials and keep the most recent log content."""
    redacted = _BEARER_CREDENTIAL.sub("Bearer <redacted>", text)
    redacted = _NAMED_CREDENTIAL.sub(r"\1\2<redacted>\4", redacted)
    if len(redacted) <= max_chars:
        return redacted, False
    marker = "[较早日志已截断，仅保留末尾]\n"
    return marker + redacted[-(max_chars - len(marker)) :], True


@dataclass(slots=True)
class _ActiveRun:
    cancel_event: threading.Event
    process: subprocess.Popen[bytes] | None = None


class ArenaManager:
    SNAPSHOT_URL = "https://spark-arena.com/static/snapshot/models"
    DETAILS_URL = "https://spark-arena.com/api/benchmarks/{benchmark_id}"

    def __init__(
        self,
        store: ArenaRunStore,
        deployments: DeploymentStore,
        ai_sessions: AISessionStore,
        *,
        uvx_binary: str = "uvx",
        uv_cache_dir: Path | None = None,
        uv_tool_dir: Path | None = None,
        metrics_service: Any | None = None,
        cluster_monitoring: Any | None = None,
    ) -> None:
        self.store = store
        self.deployments = deployments
        self.ai_sessions = ai_sessions
        self.target_resolver = ModelTargetResolver(deployments, ai_sessions)
        self.uvx_binary = uvx_binary
        self.uv_cache_dir = uv_cache_dir or Path.home() / ".cache" / "uv"
        self.uv_tool_dir = uv_tool_dir or Path.home() / ".local" / "share" / "uv" / "tools"
        self.metrics_service = metrics_service
        self.cluster_monitoring = cluster_monitoring
        self._active: dict[str, _ActiveRun] = {}
        self._lock = threading.Lock()
        self._leaderboard_cache: tuple[float, ArenaLeaderboardList] | None = None

    def create(self, request: ArenaRunCreate) -> ArenaRunRead:
        target_label, inferred_model, service_url = self._resolve_target(request)
        api_key = self._resolve_api_key(request.api_key_env)
        served_model = request.served_model_name or self._discover_model(
            service_url, inferred_model, api_key
        )
        report_model = request.report_model_name or inferred_model or served_model
        tokenizer = self._normalize_tokenizer(request.tokenizer)
        normalized = request.model_copy(
            update={
                "report_model_name": report_model,
                "served_model_name": served_model,
                "tokenizer": tokenizer,
            }
        )
        with self._lock:
            if self._active:
                raise ArenaBusy("another Spark Arena benchmark is already running")
            preview = self._build_command(
                normalized,
                service_url,
                Path("<result.json>"),
                Path("<progress.jsonl>"),
                preview=True,
            )
            run = self.store.create(
                normalized,
                target_label=target_label,
                service_url=service_url,
                served_model_name=served_model,
                command_preview=preview,
            )
            active = _ActiveRun(cancel_event=threading.Event())
            self._active[run.id] = active
        threading.Thread(
            target=self._run,
            args=(run.id, active),
            name=f"arena-{run.id[:8]}",
            daemon=True,
        ).start()
        return run

    def cancel(self, run_id: str) -> ArenaRunRead:
        run = self.store.get(run_id)
        with self._lock:
            active = self._active.get(run_id)
            if active:
                active.cancel_event.set()
                process = active.process
            else:
                process = None
        if process and process.poll() is None:
            self._terminate_process(process)
        if run.status in {"queued", "running"}:
            return self.store.cancel(run_id)
        return run

    def delete(self, run_id: str) -> None:
        run = self.store.get(run_id)
        with self._lock:
            active = run_id in self._active
        if active or run.status in {"queued", "running"}:
            raise ArenaBusy("cancel the running Arena benchmark before deleting it")
        self.store.delete(run_id)

    def stop_all(self) -> None:
        with self._lock:
            run_ids = list(self._active)
        for run_id in run_ids:
            try:
                self.cancel(run_id)
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass

    def leaderboard(self, *, query: str = "", limit: int = 100) -> ArenaLeaderboardList:
        now = time.monotonic()
        with self._lock:
            cached = self._leaderboard_cache
        if cached is None or now - cached[0] > 300:
            payload = self._fetch_json(self.SNAPSHOT_URL)
            rows = payload.get("benchmarks") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ArenaLeaderboardError("Spark Arena leaderboard returned an invalid response")
            items = []
            for row in rows:
                if not isinstance(row, dict) or not row.get("id") or not row.get("modelName"):
                    continue
                benchmark_id = str(row["id"])
                items.append(
                    ArenaLeaderboardEntry(
                        id=benchmark_id,
                        model_name=str(row["modelName"]),
                        runtime=str(row.get("runtime") or "Unknown"),
                        cluster_size=max(1, int(row.get("clusterSize") or 1)),
                        source_url=self.DETAILS_URL.format(benchmark_id=benchmark_id),
                    )
                )
            generated_at = None
            if payload.get("generatedAt"):
                try:
                    generated_at = datetime.fromisoformat(
                        str(payload["generatedAt"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    pass
            cached_value = ArenaLeaderboardList(
                generated_at=generated_at,
                items=items,
                total=len(items),
            )
            with self._lock:
                self._leaderboard_cache = (now, cached_value)
            cached = (now, cached_value)
        needle = query.casefold().strip()
        filtered = [
            item
            for item in cached[1].items
            if not needle
            or needle in item.model_name.casefold()
            or needle in item.runtime.casefold()
        ]
        return ArenaLeaderboardList(
            generated_at=cached[1].generated_at,
            items=filtered[:limit],
            total=len(filtered),
        )

    def leaderboard_detail(self, benchmark_id: str) -> ArenaLeaderboardDetail:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", benchmark_id):
            raise ArenaLeaderboardError("invalid Spark Arena benchmark ID")
        payload = self._fetch_json(self.DETAILS_URL.format(benchmark_id=benchmark_id))
        if not isinstance(payload, dict) or not isinstance(payload.get("tests"), list):
            raise ArenaLeaderboardError("Spark Arena benchmark detail is invalid")
        grouped: dict[tuple[int, int], dict[str, list[tuple[int, str, dict[str, Any]]]]] = {}
        pattern = re.compile(r"^(pp|tg)(\d+)(?: @ d(\d+))? \(c(\d+)\)$")
        for item in payload["tests"]:
            if not isinstance(item, dict):
                continue
            test_name = str(item.get("testName") or "")
            match = pattern.fullmatch(test_name)
            if not match:
                continue
            kind, token_count, depth, concurrency = match.groups()
            bucket = grouped.setdefault((int(depth or 0), int(concurrency)), {"pp": [], "tg": []})
            bucket[kind].append((int(token_count), test_name, item))
        presets: list[ArenaLeaderboardPreset] = []
        for (depth, concurrency), phases in grouped.items():
            for pp_count, pp_name, pp in phases["pp"]:
                for tg_count, tg_name, tg in phases["tg"]:
                    presets.append(
                        ArenaLeaderboardPreset(
                            pp=pp_count,
                            tg=tg_count,
                            depth=depth,
                            concurrency=concurrency,
                            prefill_test_name=pp_name,
                            decode_test_name=tg_name,
                            summary=self._summary_from_tests(pp, tg),
                        )
                    )
        presets.sort(key=lambda item: (item.depth, item.concurrency, item.pp, item.tg))
        recipe = self._runtime_recipe(
            payload.get("recipe"),
            speculative_decoding=payload.get("speculativeDecoding"),
            speculative_method=payload.get("speculativeMethod"),
        )
        return ArenaLeaderboardDetail(
            id=str(payload.get("id") or benchmark_id),
            model_name=str(payload.get("modelName") or benchmark_id),
            model_path=payload.get("modelFullPath"),
            runtime=str(payload.get("runtime") or "Unknown"),
            cluster_size=max(1, int(payload.get("clusterSize") or 1)),
            source_url=self.DETAILS_URL.format(benchmark_id=benchmark_id),
            presets=presets,
            recipe=recipe,
        )

    def logs(self, run_id: str, *, tail: int = 500) -> str:
        self.store.get(run_id)
        path = self.store.run_directory(run_id) / "llama-benchy.log"
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def result_path(self, run_id: str) -> Path:
        self.store.get(run_id)
        path = self.store.run_directory(run_id) / "result.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def report(self, run_id: str) -> dict[str, Any]:
        run = self.store.get(run_id)
        return {
            "schema_version": 1,
            "generated_at": datetime.now().astimezone().isoformat(),
            "benchmark": run.model_dump(mode="json"),
            "methodology": {
                "tool": "llama-benchy 0.4.0",
                "resource_sampling": (
                    "Local metrics sampled every second; remote values use the latest cached "
                    "SSH sample, normally refreshed every five seconds."
                ),
                "speculative_acceptance": (
                    "accepted speculative token counter delta divided by draft token counter "
                    "delta from the inference server /metrics endpoint."
                ),
            },
        }

    def _resolve_target(self, request: ArenaRunCreate) -> tuple[str, str, str]:
        try:
            target = self.target_resolver.resolve(request.target_type, request.target_id)
        except ModelTargetError as error:
            raise ArenaTargetError(str(error)) from error
        return target.label, target.model_name, target.service_url

    @staticmethod
    def _resolve_api_key(variable_name: str | None) -> str:
        if not variable_name:
            return "EMPTY"
        value = os.environ.get(variable_name)
        if not value:
            raise ArenaTargetError(f"API key environment variable {variable_name} is not set")
        return value

    @staticmethod
    def _normalize_tokenizer(tokenizer: str) -> str:
        if tokenizer.startswith(("/", "~")):
            path = Path(tokenizer).expanduser().resolve()
            if not path.is_dir():
                raise ArenaTargetError("local tokenizer directory does not exist")
            return str(path)
        return tokenizer

    @staticmethod
    def _discover_model(service_url: str, fallback: str, api_key: str) -> str:
        request = urllib.request.Request(
            f"{service_url}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=10) as response:
                payload = json.loads(response.read(1_000_000))
            models = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(models, list) and models and isinstance(models[0], dict):
                if models[0].get("id"):
                    return str(models[0]["id"])
        except (OSError, ValueError, urllib.error.URLError):
            pass
        if fallback:
            return fallback
        raise ArenaTargetError("served model name could not be discovered from /v1/models")

    def _resource_sample(self) -> dict[str, Any]:
        observed_at = time.time()
        nodes: list[dict[str, Any]] = []
        if self.metrics_service is not None:
            try:
                local = self.metrics_service.snapshot()
                nodes.append(
                    {
                        "node_id": "local",
                        "label": "Head / 本机",
                        "cpu_percent": local.get("cpu", {}).get("percent"),
                        "gpu_utilization_percent": local.get("gpu", {}).get("utilization_percent"),
                        "unified_memory_used_bytes": local.get("unified_memory", {}).get(
                            "used_bytes"
                        ),
                        "unified_memory_percent": local.get("unified_memory", {}).get("percent"),
                        "rx_bps": None,
                        "tx_bps": None,
                    }
                )
            except Exception:  # noqa: BLE001 - monitoring is best effort
                pass
        if self.cluster_monitoring is not None:
            try:
                cluster = self.cluster_monitoring.snapshot()
                for item in cluster.get("items", []):
                    if not item.get("online") or item.get("id") == "local":
                        continue
                    links = item.get("rdma_interfaces") or []
                    nodes.append(
                        {
                            "node_id": str(item.get("id") or "remote"),
                            "label": str(item.get("label") or item.get("hostname") or "Worker"),
                            "cpu_percent": item.get("cpu", {}).get("utilization_percent"),
                            "gpu_utilization_percent": item.get("gpu", {}).get(
                                "utilization_percent"
                            ),
                            "unified_memory_used_bytes": item.get("memory", {}).get("used_bytes"),
                            "unified_memory_percent": item.get("memory", {}).get("percent"),
                            "rx_bps": sum(float(link.get("rx_bps") or 0) for link in links),
                            "tx_bps": sum(float(link.get("tx_bps") or 0) for link in links),
                        }
                    )
            except Exception:  # noqa: BLE001 - monitoring is best effort
                pass
        return {"observed_at": observed_at, "nodes": nodes}

    @staticmethod
    def _summarize_resources(samples: list[dict[str, Any]]) -> ArenaResourceSummary:
        grouped: dict[str, dict[str, Any]] = {}
        for sample in samples:
            for node in sample.get("nodes") or []:
                node_id = str(node.get("node_id") or "unknown")
                bucket = grouped.setdefault(
                    node_id,
                    {
                        "node_id": node_id,
                        "label": str(node.get("label") or node_id),
                        "samples": 0,
                        "cpu": [],
                        "gpu": [],
                        "memory": [],
                        "memory_percent": [],
                        "rx": [],
                        "tx": [],
                    },
                )
                bucket["samples"] += 1
                for source, target in (
                    ("cpu_percent", "cpu"),
                    ("gpu_utilization_percent", "gpu"),
                    ("unified_memory_used_bytes", "memory"),
                    ("unified_memory_percent", "memory_percent"),
                    ("rx_bps", "rx"),
                    ("tx_bps", "tx"),
                ):
                    if node.get(source) is not None:
                        bucket[target].append(float(node[source]))
        nodes = [
            ArenaResourceNodeSummary(
                node_id=value["node_id"],
                label=value["label"],
                samples=value["samples"],
                peak_cpu_percent=max(value["cpu"], default=None),
                peak_gpu_utilization_percent=max(value["gpu"], default=None),
                peak_unified_memory_used_bytes=(
                    int(max(value["memory"])) if value["memory"] else None
                ),
                peak_unified_memory_percent=max(value["memory_percent"], default=None),
                peak_rx_bps=max(value["rx"], default=None),
                peak_tx_bps=max(value["tx"], default=None),
            )
            for value in grouped.values()
        ]
        return ArenaResourceSummary(sample_count=len(samples), nodes=nodes)

    @staticmethod
    def _service_runtime_metrics(service_url: str) -> dict[str, float]:
        request = urllib.request.Request(
            f"{service_url.rstrip('/')}/metrics",
            headers={"User-Agent": "Uniform-DGX-Spark/0.1"},
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                text = response.read(5_000_000).decode("utf-8", errors="replace")
        except (OSError, urllib.error.URLError, TimeoutError):
            return {}
        wanted = {
            "vllm:spec_decode_num_drafts_total",
            "vllm:spec_decode_num_draft_tokens_total",
            "vllm:spec_decode_num_accepted_tokens_total",
        }
        values: dict[str, float] = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            metric, separator, raw = line.rpartition(" ")
            if not separator:
                continue
            name = metric.split("{", 1)[0]
            position_match = re.search(r'position="([^"]+)"', metric)
            if name not in wanted and name != "vllm:spec_decode_num_accepted_tokens_per_pos_total":
                continue
            key = f"{name}:position={position_match.group(1)}" if position_match else name
            try:
                values[key] = values.get(key, 0.0) + float(raw)
            except ValueError:
                continue
        return values

    @staticmethod
    def _speculative_delta(
        before: dict[str, float], after: dict[str, float]
    ) -> ArenaSpeculativeMetrics:
        accepted_name = "vllm:spec_decode_num_accepted_tokens_total"
        draft_name = "vllm:spec_decode_num_draft_tokens_total"
        drafts_name = "vllm:spec_decode_num_drafts_total"
        if accepted_name not in after or draft_name not in after:
            return ArenaSpeculativeMetrics(
                supported=False,
                source="服务 /metrics 未暴露 vLLM 投机解码计数器",
            )

        def delta(name: str) -> int:
            end = after.get(name, 0.0)
            start = before.get(name, 0.0)
            return int(max(0.0, end - start if end >= start else end))

        accepted = delta(accepted_name)
        draft_tokens = delta(draft_name)
        drafts = delta(drafts_name) if drafts_name in after else None
        per_position: dict[str, float] = {}
        if drafts:
            prefix = "vllm:spec_decode_num_accepted_tokens_per_pos_total:position="
            for name in after:
                if name.startswith(prefix):
                    per_position[name.removeprefix(prefix)] = round(delta(name) / drafts * 100, 2)
        return ArenaSpeculativeMetrics(
            supported=True,
            accepted_tokens=accepted,
            draft_tokens=draft_tokens,
            drafts=drafts,
            acceptance_rate_percent=(
                round(accepted / draft_tokens * 100, 2) if draft_tokens else None
            ),
            per_position_acceptance_percent=per_position,
            source="评测前后 vLLM /metrics 计数器差值",
        )

    def _run(self, run_id: str, active: _ActiveRun) -> None:
        try:
            run = self.store.mark_running(run_id)
            if active.cancel_event.is_set():
                self.store.cancel(run_id)
                return
            run_dir = self.store.run_directory(run_id)
            result_path = run_dir / "result.json"
            progress_path = run_dir / "progress.jsonl"
            log_path = run_dir / "llama-benchy.log"
            resource_path = run_dir / "resource-samples.jsonl"
            runtime_before = self._service_runtime_metrics(run.service_url)
            resource_samples: list[dict[str, Any]] = []
            last_resource_sample = 0.0
            api_key = self._resolve_api_key(run.parameters.api_key_env)
            command = self._build_command(
                run.parameters, run.service_url, result_path, progress_path
            )
            environment = os.environ.copy()
            for name in (
                "http_proxy",
                "https_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "all_proxy",
            ):
                environment.pop(name, None)
            environment["UV_TOOL_DIR"] = str(self.uv_tool_dir)
            environment["UV_CACHE_DIR"] = str(self.uv_cache_dir)
            command_with_key = self._insert_api_key(command, api_key)
            with log_path.open("w", encoding="utf-8", buffering=1) as log:
                log.write("$ " + " ".join(run.command_preview) + "\n\n")
                process = subprocess.Popen(  # noqa: S603 - fixed executable, argv only
                    command_with_key,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                )
                with self._lock:
                    active.process = process
                progress = run.progress
                offset = 0
                while process.poll() is None:
                    if active.cancel_event.wait(0.2):
                        self._terminate_process(process)
                        break
                    progress, offset = self._read_progress(progress_path, progress, offset)
                    self.store.update_progress(run_id, progress)
                    now = time.monotonic()
                    if now - last_resource_sample >= 1.0:
                        sample = self._resource_sample()
                        resource_samples.append(sample)
                        with resource_path.open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                        last_resource_sample = now
                progress, _ = self._read_progress(progress_path, progress, offset)
                if self.store.get(run_id).status == "running":
                    self.store.update_progress(run_id, progress)
            if active.cancel_event.is_set():
                if self.store.get(run_id).status in {"queued", "running"}:
                    self.store.cancel(run_id)
                return
            if process.returncode != 0:
                raise RuntimeError(
                    f"llama-benchy exited with code {process.returncode}; inspect run logs"
                )
            final_sample = self._resource_sample()
            resource_samples.append(final_sample)
            with resource_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(final_sample, ensure_ascii=False) + "\n")
            resource_summary = self._summarize_resources(resource_samples)
            (run_dir / "resource-summary.json").write_text(
                resource_summary.model_dump_json(indent=2), encoding="utf-8"
            )
            speculative_metrics = self._speculative_delta(
                runtime_before,
                self._service_runtime_metrics(run.service_url),
            )
            (run_dir / "speculative-metrics.json").write_text(
                speculative_metrics.model_dump_json(indent=2), encoding="utf-8"
            )
            summary = self._parse_result(result_path, run.parameters)
            reference = None
            if run.parameters.leaderboard_benchmark_id:
                try:
                    reference = self._leaderboard_reference(
                        run.parameters.leaderboard_benchmark_id,
                        run.parameters,
                        summary,
                    )
                except Exception as error:  # noqa: BLE001 - local score must be preserved
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write(f"\n[warning] Spark Arena baseline unavailable: {error}\n")
            self.store.complete(run_id, summary, reference)
        except Exception as error:  # noqa: BLE001 - background job boundary
            try:
                if self.store.get(run_id).status not in {"cancelled", "completed"}:
                    self.store.fail(run_id, str(error))
            except Exception:  # noqa: BLE001 - preserve worker error
                pass
        finally:
            with self._lock:
                self._active.pop(run_id, None)

    def _build_command(
        self,
        request: ArenaRunCreate,
        service_url: str,
        result_path: Path,
        progress_path: Path,
        *,
        preview: bool = False,
    ) -> list[str]:
        command = [
            self.uvx_binary,
            "llama-benchy@0.4.0",
            "--base-url",
            f"{service_url}/v1",
            "--api-key",
            f"${{{request.api_key_env}}}" if preview and request.api_key_env else "<redacted>",
            "--model",
            request.report_model_name or request.served_model_name or "model",
            "--served-model-name",
            request.served_model_name or request.report_model_name or "model",
            "--tokenizer",
            request.tokenizer,
            "--pp",
            str(request.pp),
            "--tg",
            str(request.tg),
            "--depth",
            str(request.depth),
            "--runs",
            str(request.runs),
            "--concurrency",
            str(request.concurrency),
            "--save-result",
            str(result_path),
            "--format",
            "json",
            "--emit-progress",
            str(progress_path),
            "--exit-on-first-fail",
        ]
        if request.enable_prefix_caching:
            command.append("--enable-prefix-caching")
        if request.exact_tg:
            command.append("--exact-tg")
        if request.sglang_compatibility:
            command.extend(["--extra-body", "return_token_ids=false"])
        return command

    @staticmethod
    def _insert_api_key(command: list[str], api_key: str) -> list[str]:
        result = list(command)
        result[result.index("--api-key") + 1] = api_key
        return result

    @staticmethod
    def _read_progress(
        path: Path,
        current: ArenaProgressRead,
        offset: int,
    ) -> tuple[ArenaProgressRead, int]:
        if not path.is_file():
            return current, offset
        started = current.started_requests
        completed = current.completed_requests
        failed = current.failed_requests
        last_event = current.last_event
        last_label = current.last_label
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                event_type = str(event.get("type") or "")
                last_event = event_type or last_event
                if event_type == "request_start":
                    started += 1
                    last_label = (
                        f"第 {int(event.get('run_index') or 0) + 1} 轮 · 已发起 {started} 个请求"
                    )
                elif event_type == "request_end":
                    completed += 1
                    if event.get("error"):
                        failed += 1
                elif event_type == "latency_measured":
                    last_label = "延迟校准完成"
                elif event_type == "bench_complete":
                    last_label = "评测完成，正在汇总结果"
            new_offset = stream.tell()
        return (
            ArenaProgressRead(
                total_requests=current.total_requests,
                started_requests=started,
                completed_requests=completed,
                failed_requests=failed,
                last_event=last_event,
                last_label=last_label,
            ),
            new_offset,
        )

    @staticmethod
    def _parse_result(path: Path, request: ArenaRunCreate) -> ArenaResultSummary:
        if not path.is_file():
            raise RuntimeError("llama-benchy did not produce result.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        benchmarks = payload.get("benchmarks") if isinstance(payload, dict) else None
        if not isinstance(benchmarks, list):
            raise RuntimeError("llama-benchy result has no benchmark records")
        matching = [
            item
            for item in benchmarks
            if isinstance(item, dict)
            and int(item.get("concurrency", -1)) == request.concurrency
            and int(item.get("context_size", -1)) == request.depth
            and int(item.get("prompt_size", -1)) == request.pp
            and int(item.get("response_size", -1)) == request.tg
            and not bool(item.get("is_context_prefill_phase"))
        ]
        if not matching:
            raise RuntimeError("llama-benchy result does not match the requested parameters")
        item = matching[0]
        decode = ArenaManager._metric(item.get("tg_throughput"))
        tpot = None
        if request.concurrency == 1 and isinstance(item.get("tg_throughput"), dict):
            values = item["tg_throughput"].get("values")
            if isinstance(values, list):
                tpot_values = [1000 / float(value) for value in values if float(value) > 0]
                tpot = ArenaManager._metric_from_values(tpot_values)
        return ArenaResultSummary(
            prefill_throughput=ArenaManager._metric(item.get("pp_throughput")),
            decode_throughput=decode,
            peak_decode_throughput=ArenaManager._metric(item.get("peak_throughput")),
            ttfr_ms=ArenaManager._metric(item.get("ttfr")),
            estimated_prefill_ms=ArenaManager._metric(item.get("est_ppt")),
            e2e_ttft_ms=ArenaManager._metric(item.get("e2e_ttft")),
            tpot_ms=tpot,
        )

    def _leaderboard_reference(
        self,
        benchmark_id: str,
        request: ArenaRunCreate,
        local: ArenaResultSummary,
    ) -> ArenaLeaderboardReference:
        detail = self.leaderboard_detail(benchmark_id)
        if not detail.presets:
            raise ArenaLeaderboardError("selected entry has no comparable pp/tg tests")
        exact = next(
            (
                preset
                for preset in detail.presets
                if preset.pp == request.pp
                and preset.tg == request.tg
                and preset.depth == request.depth
                and preset.concurrency == request.concurrency
            ),
            None,
        )
        selected = exact or min(
            detail.presets,
            key=lambda preset: (
                abs(preset.depth - request.depth)
                + abs(preset.pp - request.pp)
                + abs(preset.tg - request.tg) * 8
                + abs(preset.concurrency - request.concurrency) * 4096
            ),
        )
        notes = [
            f"运行环境：榜单为 {detail.runtime} / {detail.cluster_size} 台 DGX Spark；"
            "本地镜像、量化方式和启动参数也会影响结果。"
        ]
        for label, local_value, baseline_value in (
            ("Prompt 长度 pp", request.pp, selected.pp),
            ("生成长度 tg", request.tg, selected.tg),
            ("上下文深度 depth", request.depth, selected.depth),
            ("并发数", request.concurrency, selected.concurrency),
        ):
            if local_value != baseline_value:
                notes.append(f"{label}：本地 {local_value}，榜单 {baseline_value}")
        if exact:
            notes.insert(0, "请求参数完全匹配，可直接比较；仍需留意运行环境差异。")
        else:
            notes.insert(0, "请求参数不完全匹配，仅展示最接近基线，不计算性能差距。")
        baseline = selected.summary
        local_runtime = self._local_runtime_profile(request)
        baseline_speculative = detail.recipe.speculative if detail.recipe else None
        if baseline_speculative and baseline_speculative.enabled is not None:
            state = "启用" if baseline_speculative.enabled else "未启用"
            notes.append(f"榜单投机解码：{state}。")
        if local_runtime and local_runtime.speculative.enabled is not None:
            state = "启用" if local_runtime.speculative.enabled else "未启用"
            notes.append(f"本地投机解码：{state}。")
        elif local_runtime:
            notes.append("本地投机解码：无法从启动命令与当前容器配置可靠判断。")
        if (
            baseline_speculative
            and local_runtime
            and baseline_speculative.enabled is not None
            and local_runtime.speculative.enabled is not None
            and baseline_speculative.enabled != local_runtime.speculative.enabled
        ):
            notes.append("本地与榜单的投机解码开关不同，性能差距包含该运行配置影响。")
        return ArenaLeaderboardReference(
            benchmark_id=benchmark_id,
            model_name=detail.model_name,
            model_path=detail.model_path,
            runtime=detail.runtime,
            cluster_size=detail.cluster_size,
            source_url=self.DETAILS_URL.format(benchmark_id=benchmark_id),
            summary=baseline,
            baseline_pp=selected.pp,
            baseline_tg=selected.tg,
            baseline_depth=selected.depth,
            baseline_concurrency=selected.concurrency,
            parameter_match=exact is not None,
            comparison_notes=notes,
            recipe=detail.recipe,
            local_runtime=local_runtime,
            prefill_gap_percent=(
                self._throughput_gap(local.prefill_throughput, baseline.prefill_throughput)
                if exact
                else None
            ),
            decode_gap_percent=(
                self._throughput_gap(local.decode_throughput, baseline.decode_throughput)
                if exact
                else None
            ),
            ttfr_gap_percent=(
                self._latency_gap(local.ttfr_ms, baseline.ttfr_ms) if exact else None
            ),
        )

    @classmethod
    def _runtime_recipe(
        cls,
        payload: Any,
        *,
        speculative_decoding: Any = None,
        speculative_method: Any = None,
    ) -> ArenaRuntimeRecipe | None:
        if not isinstance(payload, dict) and speculative_decoding is None:
            return None
        if not isinstance(payload, dict):
            payload = {}
        full_recipe = payload.get("fullRecipe")
        recipe = full_recipe if isinstance(full_recipe, dict) else payload
        defaults = recipe.get("defaults")
        defaults = defaults if isinstance(defaults, dict) else {}
        environment = recipe.get("env")
        environment = environment if isinstance(environment, dict) else {}
        command = recipe.get("command") or payload.get("command")
        public_config = defaults.get("speculative_config")
        local_config = defaults.get("local_speculative_config")
        speculative = cls._speculative_config(
            command=str(command or ""),
            environment={str(key): str(value) for key, value in environment.items()},
            explicit=public_config if public_config is not None else local_config,
            complete=bool(command) or bool(defaults),
            evidence="Spark Arena 公开 recipe",
        )
        if isinstance(speculative_decoding, bool):
            speculative.enabled = speculative_decoding
            if speculative.raw is None:
                speculative.evidence = "Spark Arena 公开 benchmark 元数据"
        if speculative.method is None:
            speculative.method = cls._optional_text(speculative_method)
        pre_exec = recipe.get("pre_exec")
        return ArenaRuntimeRecipe(
            container=cls._optional_text(recipe.get("container") or payload.get("container")),
            command=cls._optional_text(command),
            model_revision=cls._optional_text(
                recipe.get("model_revision") or defaults.get("revision")
            ),
            defaults={str(key): value for key, value in defaults.items()},
            environment={str(key): str(value) for key, value in environment.items()},
            pre_exec=([str(item) for item in pre_exec] if isinstance(pre_exec, list) else []),
            speculative=speculative,
        )

    def _local_runtime_profile(self, request: ArenaRunCreate) -> ArenaRuntimeProfile | None:
        return self.runtime_profile(request.target_type, request.target_id)

    def runtime_profile(self, target_type: str, target_id: str) -> ArenaRuntimeProfile:
        if target_type == "deployment":
            deployment = self.deployments.get(target_id)
            command = "vllm serve /models/model " + " ".join(deployment.parameters.extra_args)
            speculative = self._speculative_config(
                command=command,
                environment={},
                explicit=None,
                complete=True,
                evidence="平台托管部署参数",
            )
            self._apply_live_speculative_evidence(
                speculative, f"http://127.0.0.1:{deployment.parameters.port}"
            )
            return ArenaRuntimeProfile(
                engine="vllm",
                image=deployment.parameters.image,
                container_name=DockerRuntime.container_name(deployment.id),
                launch_command=command.strip(),
                configuration_source="平台托管部署参数",
                cluster_size=1,
                tensor_parallel=self._integer_setting(
                    command, {}, ("--tensor-parallel-size", "--tp-size"), (), default=1
                ),
                max_model_len=deployment.parameters.max_model_len,
                gpu_memory_utilization=deployment.parameters.gpu_memory_utilization,
                quantization=self._option_value(command, "--quantization", "--load-format"),
                prefix_caching=(
                    True if "--enable-prefix-caching" in deployment.parameters.extra_args else None
                ),
                speculative=speculative,
            )

        if target_type != "ai_session":
            raise ArenaTargetError("unsupported Arena target type")
        result = self.ai_sessions.get_result(target_id)
        inspected_command, environment, inspected = self._inspect_container_config(
            result.container_name
        )
        detection_command = "\n".join(
            item for item in (result.launch_command, inspected_command) if item
        )
        direct_engine_command = bool(
            re.search(r"(?:^|\s)(?:vllm|sglang)(?:\s|$)", detection_command)
            or re.search(r"(?:^|\s)(?:vllm\s+serve|python\s+-m\s+vllm)", detection_command)
        )
        relevant_environment = any(
            key.upper()
            in {
                "SPEC_METHOD",
                "SPECULATIVE_CONFIG",
                "VLLM_SPECULATIVE_CONFIG",
            }
            for key in environment
        )
        evidence = "当前容器配置" if inspected else "统一部署结果中的启动命令"
        speculative = self._speculative_config(
            command=detection_command,
            environment=environment,
            explicit=None,
            complete=direct_engine_command or relevant_environment,
            evidence=evidence,
        )
        self._apply_live_speculative_evidence(speculative, f"http://127.0.0.1:{result.port}")
        return ArenaRuntimeProfile(
            engine=result.engine,
            image=result.image,
            container_name=result.container_name,
            launch_command=result.launch_command,
            configuration_source=evidence,
            cluster_size=self._integer_setting(
                detection_command,
                environment,
                ("--nnodes", "--num-nodes"),
                ("NNODES", "WORLD_SIZE"),
            ),
            tensor_parallel=self._integer_setting(
                detection_command,
                environment,
                ("--tensor-parallel-size", "--tp-size"),
                ("TP", "TENSOR_PARALLEL_SIZE"),
            ),
            max_model_len=self._integer_setting(
                detection_command,
                environment,
                ("--max-model-len", "--context-length"),
                ("MAX_MODEL_LEN", "CONTEXT_LENGTH"),
            ),
            gpu_memory_utilization=self._float_setting(
                detection_command,
                environment,
                ("--gpu-memory-utilization", "--mem-fraction-static"),
                ("GPU_MEMORY_UTILIZATION", "GPU_MEM_UTIL", "MEM_FRACTION_STATIC"),
            ),
            quantization=(
                self._option_value(detection_command, "--quantization", "--load-format")
                or environment.get("QUANTIZATION")
                or environment.get("LOAD_FORMAT")
            ),
            prefix_caching=(
                True
                if "--enable-prefix-caching" in detection_command
                else False
                if "--disable-prefix-caching" in detection_command
                else None
            ),
            speculative=speculative,
        )

    @staticmethod
    def _option_value(command: str, *names: str) -> str | None:
        for name in names:
            match = re.search(
                rf"(?:^|\s){re.escape(name)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
                command,
            )
            if match:
                return next((value for value in match.groups() if value is not None), None)
        return None

    @classmethod
    def _integer_setting(
        cls,
        command: str,
        environment: dict[str, str],
        options: tuple[str, ...],
        variables: tuple[str, ...],
        *,
        default: int | None = None,
    ) -> int | None:
        raw = cls._option_value(command, *options) or next(
            (environment.get(name) for name in variables if environment.get(name)), None
        )
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    @classmethod
    def _float_setting(
        cls,
        command: str,
        environment: dict[str, str],
        options: tuple[str, ...],
        variables: tuple[str, ...],
    ) -> float | None:
        raw = cls._option_value(command, *options) or next(
            (environment.get(name) for name in variables if environment.get(name)), None
        )
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            return None

    def _apply_live_speculative_evidence(
        self, speculative: ArenaSpeculativeConfig, service_url: str
    ) -> None:
        metrics = self._service_runtime_metrics(service_url)
        draft_tokens = metrics.get("vllm:spec_decode_num_draft_tokens_total", 0)
        accepted_tokens = metrics.get("vllm:spec_decode_num_accepted_tokens_total", 0)
        if draft_tokens > 0 or accepted_tokens > 0:
            speculative.enabled = True
            speculative.evidence = "当前服务 /metrics 已观察到投机 draft/accepted token 计数"

    @staticmethod
    def _inspect_container_config(container_name: str) -> tuple[str, dict[str, str], bool]:
        normalized = container_name.removeprefix("/")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", normalized):
            return "", {}, False
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and validated name
                ["docker", "inspect", "--format", "{{json .Config}}", normalized],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return "", {}, False
            config = json.loads(result.stdout)
            if not isinstance(config, dict):
                return "", {}, False
            argv = []
            for key in ("Entrypoint", "Cmd"):
                value = config.get(key)
                if isinstance(value, list):
                    argv.extend(str(item) for item in value)
                elif isinstance(value, str):
                    argv.append(value)
            environment: dict[str, str] = {}
            for item in config.get("Env") or []:
                if isinstance(item, str) and "=" in item:
                    key, value = item.split("=", 1)
                    environment[key] = value
            return shlex.join(argv), environment, True
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            return "", {}, False

    @classmethod
    def _speculative_config(
        cls,
        *,
        command: str,
        environment: dict[str, str],
        explicit: Any,
        complete: bool,
        evidence: str,
    ) -> ArenaSpeculativeConfig:
        raw = cls._parse_mapping(explicit)
        env_upper = {key.upper(): value for key, value in environment.items()}
        if raw is None:
            raw = cls._parse_mapping(
                env_upper.get("VLLM_SPECULATIVE_CONFIG") or env_upper.get("SPECULATIVE_CONFIG")
            )
        spec_method = env_upper.get("SPEC_METHOD")
        if spec_method is not None and spec_method.strip().casefold() in {
            "",
            "0",
            "false",
            "none",
            "off",
            "disabled",
        }:
            return ArenaSpeculativeConfig(
                enabled=False,
                evidence=f"{evidence}：SPEC_METHOD={spec_method or '<empty>'}",
            )

        command_config = cls._command_option(command, "--speculative-config")
        if raw is None and command_config and "{" not in command_config:
            command_config = None
        if raw is None:
            raw = cls._parse_mapping(command_config)
        draft_model = cls._command_option(command, "--speculative-model")
        token_value = cls._command_option(command, "--num-speculative-tokens")
        method = spec_method.strip() if spec_method and spec_method.strip() else None
        if isinstance(raw, dict):
            method = cls._optional_text(raw.get("method")) or method
            draft_model = cls._optional_text(
                raw.get("model") or raw.get("draft_model") or draft_model
            )
            token_value = raw.get("num_speculative_tokens", token_value)
        disabled_by_config = isinstance(raw, dict) and (
            raw.get("enabled") is False
            or str(raw.get("method") or "").casefold() in {"none", "off", "disabled"}
        )
        has_spec_flag = any(
            flag in command
            for flag in (
                "--speculative-config",
                "--speculative-model",
                "--num-speculative-tokens",
            )
        )
        enabled = False if disabled_by_config else bool(raw or draft_model or method)
        if not enabled and has_spec_flag and command_config is None and not draft_model:
            enabled = None
        elif not enabled and not complete:
            enabled = None
        parsed_tokens = cls._optional_int(token_value)
        return ArenaSpeculativeConfig(
            enabled=enabled,
            method=method,
            draft_model=draft_model,
            num_speculative_tokens=parsed_tokens,
            draft_tensor_parallel_size=(
                cls._optional_int(raw.get("draft_tensor_parallel_size"))
                if isinstance(raw, dict)
                else None
            ),
            draft_sample_method=(
                cls._optional_text(raw.get("draft_sample_method"))
                if isinstance(raw, dict)
                else None
            ),
            rejection_sample_method=(
                cls._optional_text(raw.get("rejection_sample_method"))
                if isinstance(raw, dict)
                else None
            ),
            evidence=evidence,
            raw=raw if raw is not None else explicit,
        )

    @staticmethod
    def _parse_mapping(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _command_option(command: str, option: str) -> str | None:
        try:
            arguments = shlex.split(command)
        except ValueError:
            arguments = command.replace("\\\n", " ").split()
        for index, argument in enumerate(arguments):
            if argument == option and index + 1 < len(arguments):
                return arguments[index + 1]
            if argument.startswith(option + "="):
                return argument.split("=", 1)[1]
        return None

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _summary_from_tests(pp: dict[str, Any], tg: dict[str, Any]) -> ArenaResultSummary:
        def metric(item: dict[str, Any], value: str, deviation: str) -> ArenaMetricRead | None:
            if item.get(value) is None:
                return None
            return ArenaMetricRead(
                mean=float(item[value]),
                std=float(item.get(deviation) or 0),
            )

        return ArenaResultSummary(
            prefill_throughput=metric(pp, "tokensPerSec", "tokensPerSecStdDev"),
            decode_throughput=metric(tg, "tokensPerSec", "tokensPerSecStdDev"),
            ttfr_ms=metric(pp, "ttfr", "ttfrStdDev"),
            estimated_prefill_ms=metric(pp, "estPpt", "estPptStdDev"),
            e2e_ttft_ms=metric(pp, "e2eTtft", "e2eTtftStdDev"),
        )

    @staticmethod
    def _metric(value: Any) -> ArenaMetricRead | None:
        if not isinstance(value, dict) or value.get("mean") is None:
            return None
        values = value.get("values")
        if isinstance(values, list) and values:
            result = ArenaManager._metric_from_values(
                [float(item) for item in values if isinstance(item, int | float)]
            )
            if result:
                result.mean = float(value["mean"])
                result.std = float(value.get("std") or 0)
                return result
        return ArenaMetricRead(mean=float(value["mean"]), std=float(value.get("std") or 0))

    @staticmethod
    def _metric_from_values(values: list[float]) -> ArenaMetricRead | None:
        if not values:
            return None
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return ArenaMetricRead(
            mean=mean,
            std=variance**0.5,
            p50=ArenaManager._percentile(values, 0.50),
            p95=ArenaManager._percentile(values, 0.95),
            p99=ArenaManager._percentile(values, 0.99),
        )

    @staticmethod
    def _percentile(values: list[float], quantile: float) -> float:
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * quantile
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = position - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    @staticmethod
    def _throughput_gap(
        local: ArenaMetricRead | None, baseline: ArenaMetricRead | None
    ) -> float | None:
        if not local or not baseline or baseline.mean == 0:
            return None
        return (local.mean - baseline.mean) / baseline.mean * 100

    @staticmethod
    def _latency_gap(
        local: ArenaMetricRead | None, baseline: ArenaMetricRead | None
    ) -> float | None:
        if not local or not baseline or baseline.mean == 0:
            return None
        return (baseline.mean - local.mean) / baseline.mean * 100

    @staticmethod
    def _fetch_json(url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Uniform-DGX-Spark/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
                return json.loads(response.read(10_000_000))
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise ArenaLeaderboardError(f"cannot read Spark Arena: {error}") from error

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
