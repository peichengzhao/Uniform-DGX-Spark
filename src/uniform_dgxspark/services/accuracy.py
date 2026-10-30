from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..accuracy_identity import cache_fingerprint, evaluation_fingerprint
from ..accuracy_store import AccuracyRunStore, AccuracyRunTransitionError
from ..ai_store import AISessionStore
from ..schemas import (
    AccuracyArtifactList,
    AccuracyArtifactRead,
    AccuracyComparisonRead,
    AccuracyLogRead,
    AccuracyMetricRead,
    AccuracyResolvedDataset,
    AccuracyRunCreate,
    AccuracyRunError,
    AccuracyRunProgress,
    AccuracyRunRead,
    AccuracyRunSummary,
)
from ..store import DeploymentStore
from .evalscope_accuracy import (
    EvalScopeAccuracyAdapter,
    EvalScopeIdentityError,
    EvalScopeReportError,
    NormalizedEvalScopeReport,
)
from .model_targets import ModelTargetError, ModelTargetResolver


class AccuracyBusy(RuntimeError):
    pass


class AccuracyTargetError(RuntimeError):
    pass


class AccuracyCompatibilityError(ValueError):
    pass


class EvalScopeProcessError(RuntimeError):
    pass


@dataclass(slots=True)
class _ActiveAccuracyRun:
    cancel_event: threading.Event
    process: subprocess.Popen[bytes] | None = None
    adapter: EvalScopeAccuracyAdapter | None = None


class AccuracyManager:
    """Run one pinned EvalScope accuracy job at a time and persist normalized results."""

    def __init__(
        self,
        store: AccuracyRunStore,
        deployments: DeploymentStore,
        ai_sessions: AISessionStore,
        runs_directory: Path,
        adapter: EvalScopeAccuracyAdapter,
    ) -> None:
        self.store = store
        self.target_resolver = ModelTargetResolver(deployments, ai_sessions)
        self.runs_directory = runs_directory
        self.adapter = adapter
        self._active: dict[str, _ActiveAccuracyRun] = {}
        self._lock = threading.Lock()

    def create(
        self,
        request: AccuracyRunCreate,
        *,
        validation_adapter: EvalScopeAccuracyAdapter | None = None,
    ) -> AccuracyRunRead:
        adapter = validation_adapter or self.adapter
        try:
            target = self.target_resolver.resolve(request.target_type, request.target_id)
            resolved_config = adapter.resolve_config(request, target.snapshot)
        except ModelTargetError as error:
            raise AccuracyTargetError(str(error)) from error
        with self._lock:
            if self._active:
                raise AccuracyBusy("another accuracy evaluation is already running")
            source = None
            if request.use_cache:
                source = self.store.get(request.cache_source_run_id or "")
                fingerprint = evaluation_fingerprint(
                    resolved_config, adapter.version, adapter.commit
                )
                expected_cache = cache_fingerprint(fingerprint, target.model_name, target.snapshot)
                if (
                    not source.cache_fingerprint
                    or source.cache_fingerprint != expected_cache
                    or source.status in {"queued", "preparing", "running", "parsing"}
                ):
                    raise AccuracyCompatibilityError(
                        "cache requires matching evaluation identity, model and deployment; "
                        "the source must no longer be active"
                    )
            run_directory = (self.runs_directory / str(uuid4())).resolve()
            run_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            try:
                if source:
                    self._copy_cache(source, run_directory)
            except Exception:
                shutil.rmtree(run_directory)
                raise
            run = self.store.create(
                request,
                target_label=target.label,
                model_name=target.model_name,
                service_url=target.service_url,
                resolved_config=resolved_config,
                evalscope_version=adapter.version,
                evalscope_commit=adapter.commit,
                run_directory=run_directory,
            )
            active = _ActiveAccuracyRun(cancel_event=threading.Event(), adapter=adapter)
            self._active[run.id] = active
        threading.Thread(
            target=self._run,
            args=(run.id, active),
            name=f"accuracy-{run.id[:8]}",
            daemon=True,
        ).start()
        return run

    def resume(self, run_id: str) -> AccuracyRunRead:
        source = self.store.get(run_id)
        if source.status not in {"cancelled", "interrupted", "failed", "partial"}:
            raise AccuracyCompatibilityError("only incomplete terminal tasks can be resumed")
        payload = source.request.model_dump()
        payload.update(use_cache=True, cache_source_run_id=source.id)
        return self.create(AccuracyRunCreate.model_validate(payload))

    def compare(self, run_ids: list[str]) -> AccuracyComparisonRead:
        runs = [self.store.get(run_id) for run_id in run_ids]
        identity = runs[0].evaluation_fingerprint
        if not identity or any(run.evaluation_fingerprint != identity for run in runs):
            raise AccuracyCompatibilityError("different evaluation fingerprints cannot be compared")
        if any(
            run.status != "completed" or not run.summary or run.summary.incomplete for run in runs
        ):
            raise AccuracyCompatibilityError("comparison requires complete, successful reports")

        def metric_contract(run: AccuracyRunRead) -> set[tuple[object, ...]]:
            return {
                (
                    metric.dataset_name,
                    metric.evaluation_version,
                    metric.metric_key,
                    tuple(metric.category_path),
                    metric.subset_name,
                    metric.metric_kind,
                    metric.higher_is_better,
                    metric.sample_count,
                )
                for metric in run.metrics
            }

        if any(metric_contract(run) != metric_contract(runs[0]) for run in runs[1:]):
            raise AccuracyCompatibilityError("report metrics or sample counts are incompatible")
        return AccuracyComparisonRead(evaluation_fingerprint=identity, items=runs)

    def _copy_cache(self, source: AccuracyRunRead, destination: Path) -> None:
        root = self._safe_run_directory(source)
        manifest = root / "uniform-accuracy-config.json"
        if manifest.is_symlink() or not manifest.is_file():
            raise AccuracyCompatibilityError("cache identity manifest is missing or unsafe")
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError) as error:
            raise AccuracyCompatibilityError("cache identity manifest is invalid") from error
        if metadata.get("cache_fingerprint") != source.cache_fingerprint:
            raise AccuracyCompatibilityError("cache manifest identity does not match its record")
        workspaces = list((root / "evalscope").glob("*"))
        if not any(
            (workspace / "configs" / "task_config.yaml").is_file()
            and any((workspace / "predictions").rglob("*.jsonl"))
            for workspace in workspaces
        ):
            raise AccuracyCompatibilityError("source has no reusable prediction/config cache")
        size = 0

        def copy_tree(origin: Path, target: Path) -> None:
            nonlocal size
            if origin.is_symlink():
                raise AccuracyCompatibilityError("cache path contains a symlink")
            if not origin.exists():
                return
            for path in [origin, *origin.rglob("*")]:
                if path.is_symlink() or root not in path.resolve().parents:
                    raise AccuracyCompatibilityError(
                        "cache path contains a symlink or escapes its task"
                    )
                if path.is_file():
                    size += path.stat().st_size
                    if size > 2_000_000_000:
                        raise AccuracyCompatibilityError("cache copy exceeds the 2 GB safety limit")
            shutil.copytree(origin, target)

        for workspace in workspaces:
            for name in ("predictions", "reviews", "configs"):
                copy_tree(workspace / name, destination / "evalscope" / workspace.name / name)
        for name in ("dataset-cache", "modelscope-cache", "evalscope-cache", "huggingface-cache"):
            copy_tree(root / name, destination / name)

    def cancel(self, run_id: str) -> AccuracyRunRead:
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
        if run.status in {"queued", "preparing", "running", "parsing"}:
            try:
                return self.store.cancel(run_id)
            except AccuracyRunTransitionError:
                return self.store.get(run_id)
        return run

    def stop_all(self) -> None:
        with self._lock:
            run_ids = list(self._active)
        for run_id in run_ids:
            try:
                self.cancel(run_id)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass

    def delete(self, run_id: str) -> None:
        run = self.store.get(run_id)
        with self._lock:
            active = run_id in self._active
            if active or run.status in {"queued", "preparing", "running", "parsing"}:
                raise AccuracyBusy("cancel the running accuracy evaluation before deleting it")
            run_directory = self._safe_run_directory(run)
            self.store.delete(run_id)
            if run_directory.is_dir():
                shutil.rmtree(run_directory)

    def logs(self, run_id: str, *, tail: int = 500) -> AccuracyLogRead:
        run = self.store.get(run_id)
        run_directory = self._safe_run_directory(run)
        sections: list[str] = []
        size_bytes = 0
        truncated = False
        for path in sorted((run_directory / "evalscope").glob("*/evalscope.log")):
            resolved_path = path.resolve()
            if (
                path.is_symlink()
                or not path.is_file()
                or run_directory not in resolved_path.parents
            ):
                continue
            size_bytes += path.stat().st_size
            text, was_truncated = self._read_log_tail(path, max_bytes=1_000_000)
            truncated = truncated or was_truncated
            label = path.parent.name
            sections.append(f"== {label} ==\n{self._redact_log(text)}".rstrip())
        lines = "\n\n".join(sections).splitlines()
        if len(lines) > tail:
            lines = lines[-tail:]
            truncated = True
        return AccuracyLogRead(
            logs="\n".join(lines),
            size_bytes=size_bytes,
            truncated=truncated,
        )

    def artifacts(self, run_id: str) -> AccuracyArtifactList:
        run = self.store.get(run_id)
        run_directory = self._safe_run_directory(run)
        items = [
            AccuracyArtifactRead(
                path=str(path.relative_to(run_directory)),
                size_bytes=path.stat().st_size,
            )
            for path in self._artifact_files(run_directory)
        ]
        return AccuracyArtifactList(items=items, total=len(items))

    def artifact_path(self, run_id: str, relative_path: str) -> Path:
        run = self.store.get(run_id)
        run_directory = self._safe_run_directory(run)
        normalized = Path(relative_path)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise FileNotFoundError(relative_path)
        allowed = {
            str(path.relative_to(run_directory)): path
            for path in self._artifact_files(run_directory)
        }
        path = allowed.get(normalized.as_posix())
        if path is None:
            raise FileNotFoundError(relative_path)
        return path

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def _safe_run_directory(self, run: AccuracyRunRead) -> Path:
        root = self.runs_directory.resolve()
        run_directory = Path(run.run_directory).resolve()
        if root not in run_directory.parents:
            raise RuntimeError("accuracy run directory is outside the configured root")
        return run_directory

    @staticmethod
    def _artifact_files(run_directory: Path) -> list[Path]:
        candidates = [run_directory / "uniform-accuracy-config.json"]
        candidates.extend((run_directory / "evalscope").glob("*/uniform-command.json"))
        candidates.extend((run_directory / "evalscope").glob("*/progress.json"))
        candidates.extend((run_directory / "evalscope").glob("*/reports/**/*.json"))
        return sorted(
            {
                path.resolve()
                for path in candidates
                if path.is_file()
                and not path.is_symlink()
                and run_directory.resolve() in path.resolve().parents
            }
        )

    @staticmethod
    def _read_log_tail(path: Path, *, max_bytes: int) -> tuple[str, bool]:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(-max_bytes, os.SEEK_END)
            data = stream.read(max_bytes)
        return data.decode("utf-8", errors="replace"), size > max_bytes

    @staticmethod
    def _redact_log(text: str) -> str:
        text = re.sub(
            r"(?im)(\bauthorization\b\s*[:=]\s*)(?:Bearer\s+)?[^\r\n,;]+",
            r"\1<redacted>",
            text,
        )
        text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", text)
        text = re.sub(
            r"(?i)(\b(?:api[_-]?key|token|password|passwd|secret)"
            r"\b\s*[:=]\s*)(\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)",
            r"\1<redacted>",
            text,
        )
        return re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1<redacted>@", text)

    def _run(self, run_id: str, active: _ActiveAccuracyRun) -> None:
        adapter = active.adapter or self.adapter
        try:
            run = self.store.mark_preparing(run_id)
            identity = adapter.verify_identity()
            if active.cancel_event.is_set():
                self.cancel(run_id)
                return
            run_directory = Path(run.run_directory)
            planned_total = sum(
                dataset.limit * len(dataset.subset_list) for dataset in run.resolved_config.datasets
            )
            snapshot = {
                "evalscope": {"version": identity.version, "commit": identity.commit},
                "model_name": run.model_name,
                "service_url": run.service_url,
                "resolved_config": run.resolved_config.model_dump(mode="json"),
                "evaluation_fingerprint": run.evaluation_fingerprint,
                "cache_fingerprint": run.cache_fingerprint,
                "cache_source_run_id": run.request.cache_source_run_id,
            }
            (run_directory / "uniform-accuracy-config.json").write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            reports: list[NormalizedEvalScopeReport] = []
            forced_partial = False
            processed_before = 0
            cached_samples = 0
            for index, dataset in enumerate(run.resolved_config.datasets):
                work_directory = run_directory / "evalscope" / f"{index:02d}-{dataset.name}"
                work_directory.mkdir(mode=0o700, parents=True, exist_ok=run.request.use_cache)
                reuse = (
                    run.request.use_cache
                    and (work_directory / "configs" / "task_config.yaml").is_file()
                )
                command = adapter.build_command(
                    model_name=run.model_name,
                    service_url=run.service_url,
                    dataset=dataset,
                    config=run.resolved_config,
                    run_directory=run_directory,
                    work_directory=work_directory,
                    use_cache=reuse,
                )
                (work_directory / "uniform-command.json").write_text(
                    json.dumps(command, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                log_path = work_directory / "evalscope.log"
                with log_path.open("w", encoding="utf-8", buffering=1) as log:
                    process = subprocess.Popen(  # noqa: S603 - pinned executable and argv only
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=run_directory,
                        env=adapter.build_environment(run_directory),
                        start_new_session=True,
                    )
                    with self._lock:
                        active.process = process
                    if index == 0:
                        self.store.mark_running(run_id, pid=process.pid)
                    else:
                        self.store.update_pid(run_id, pid=process.pid)
                    while process.poll() is None:
                        if active.cancel_event.wait(0.2):
                            self._terminate_process(process)
                            break
                        self._persist_progress(
                            run_id,
                            dataset,
                            work_directory,
                            processed_before,
                            planned_total,
                        )
                    self._persist_progress(
                        run_id,
                        dataset,
                        work_directory,
                        processed_before,
                        planned_total,
                    )
                with self._lock:
                    active.process = None
                text, _ = self._read_log_tail(log_path, max_bytes=1_000_000)
                cached_samples += sum(
                    int(count)
                    for count in re.findall(r"Reusing predictions .*? got (\d+) predictions", text)
                )
                self.store.set_cached_samples(run_id, cached_samples)
                if active.cancel_event.is_set():
                    self.cancel(run_id)
                    return
                if adapter.has_report(work_directory, dataset.name):
                    report = adapter.normalize_report(work_directory, dataset, run.model_name)
                    reports.append(report)
                    processed_before += report.execution_requested
                if process.returncode != 0:
                    if reports:
                        forced_partial = True
                        break
                    raise EvalScopeProcessError(
                        f"EvalScope exited with code {process.returncode}; inspect run logs"
                    )
                if not reports or len(reports) <= index:
                    raise EvalScopeReportError(
                        f"EvalScope produced no {dataset.name} schema-v2 report"
                    )

            if active.cancel_event.is_set():
                self.cancel(run_id)
                return
            self.store.mark_parsing(run_id)
            self._complete(
                run_id,
                reports,
                planned_total=planned_total,
                expected_datasets=len(run.resolved_config.datasets),
                forced_partial=forced_partial,
            )
        except Exception as error:  # noqa: BLE001 - background job boundary
            self._record_failure(run_id, error)
        finally:
            try:
                # A state-write/cancellation race after Popen must not orphan the child.
                process = active.process
                if process is not None and process.poll() is None:
                    self._terminate_process(process)
            finally:
                with self._lock:
                    self._active.pop(run_id, None)

    def _persist_progress(
        self,
        run_id: str,
        dataset: AccuracyResolvedDataset,
        work_directory: Path,
        processed_before: int,
        planned_total: int,
    ) -> None:
        progress = self.adapter.read_progress(work_directory)
        if progress is None:
            return
        total = max(planned_total, processed_before + progress.total_samples)
        processed = min(total, processed_before + progress.processed_samples)
        overall = processed / total * 100 if total else 0
        try:
            self.store.update_progress(
                run_id,
                AccuracyRunProgress(
                    phase="running",
                    processed_samples=processed,
                    total_samples=total,
                    percent=round(overall, 2),
                    current_dataset=dataset.name,
                    message=progress.message,
                ),
            )
        except AccuracyRunTransitionError:
            if self.store.get(run_id).status != "cancelled":
                raise

    def _complete(
        self,
        run_id: str,
        reports: list[NormalizedEvalScopeReport],
        *,
        planned_total: int,
        expected_datasets: int,
        forced_partial: bool,
    ) -> None:
        metrics: list[AccuracyMetricRead] = [
            metric for report in reports for metric in report.metrics
        ]
        if not metrics:
            raise EvalScopeReportError("EvalScope produced no usable metrics")
        requested = max(
            planned_total,
            sum(report.execution_requested for report in reports),
        )
        succeeded = sum(report.execution_succeeded for report in reports)
        errored = sum(report.execution_errored for report in reports)
        partial = (
            forced_partial
            or len(reports) != expected_datasets
            or any(report.incomplete for report in reports)
            or errored > 0
            or succeeded + errored < requested
        )
        summary = AccuracyRunSummary(
            dataset_count=len({metric.dataset_name for metric in metrics}),
            metric_count=len(metrics),
            primary_metric_count=sum(metric.is_primary for metric in metrics),
            execution_requested=requested,
            execution_succeeded=succeeded,
            execution_errored=errored,
            incomplete=partial,
        )
        self.store.complete(run_id, summary, metrics, partial=partial)

    def _record_failure(self, run_id: str, error: Exception) -> None:
        try:
            run = self.store.get(run_id)
            if run.status in {"completed", "partial", "failed", "cancelled", "interrupted"}:
                return
            if isinstance(error, EvalScopeIdentityError):
                code, retryable = "evalscope_identity_invalid", False
            elif isinstance(error, EvalScopeReportError):
                code, retryable = "evalscope_report_invalid", False
            elif isinstance(error, EvalScopeProcessError):
                code, retryable = "evalscope_failed", True
            elif isinstance(error, OSError):
                code, retryable = "evalscope_launch_failed", True
            else:
                code, retryable = "accuracy_internal_error", False
            self.store.fail(
                run_id,
                AccuracyRunError(
                    code=code,
                    message=str(error)[:4000] or error.__class__.__name__,
                    retryable=retryable,
                    details={"error_type": error.__class__.__name__},
                ),
            )
        except Exception:  # noqa: BLE001 - preserve the original background failure
            pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
