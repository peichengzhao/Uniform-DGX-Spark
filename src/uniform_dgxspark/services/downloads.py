from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from huggingface_hub import HfApi, snapshot_download


class SnapshotDownloader(Protocol):
    def __call__(self, **kwargs: Any) -> str: ...


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    revision: str
    cached: bool
    bytes_on_disk: int


class ModelDownloadError(RuntimeError):
    pass


class HuggingFaceDownloadManager:
    def __init__(
        self,
        models_dir: Path,
        *,
        token: str | None = None,
        max_workers: int = 4,
        api: HfApi | None = None,
        downloader: SnapshotDownloader = snapshot_download,
    ) -> None:
        self.models_dir = models_dir
        self.token = token
        self.max_workers = max_workers
        self.api = api or HfApi(token=token)
        self.downloader = downloader

    def download(
        self,
        model_id: str,
        revision: str | None,
        *,
        progress_callback: Callable[[float, int, int | None], None] | None = None,
    ) -> DownloadResult:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        resolved_revision, expected_bytes = self._resolve_revision(model_id, revision)
        target = self._target_path(model_id, resolved_revision)
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".uniform-download-complete.json"
        lock_path = target / ".uniform-download.lock"

        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if self._is_complete(marker, model_id, resolved_revision, target):
                current_bytes = self._directory_size(target)
                if progress_callback:
                    progress_callback(1.0, current_bytes, expected_bytes)
                return DownloadResult(
                    path=target,
                    revision=resolved_revision,
                    cached=True,
                    bytes_on_disk=current_bytes,
                )

            monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=self._monitor_progress,
                args=(target, expected_bytes, progress_callback, monitor_stop),
                daemon=True,
            )
            monitor.start()
            try:
                self.downloader(
                    repo_id=model_id,
                    revision=resolved_revision,
                    local_dir=str(target),
                    token=self.token,
                    max_workers=self.max_workers,
                )
                self._validate_snapshot(target)
                bytes_on_disk = self._directory_size(target)
                metadata = {
                    "model_id": model_id,
                    "revision": resolved_revision,
                    "bytes_on_disk": bytes_on_disk,
                    "completed_at": time.time(),
                }
                temporary_marker = marker.with_suffix(".tmp")
                temporary_marker.write_text(
                    json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
                )
                os.replace(temporary_marker, marker)
            except Exception as error:
                raise ModelDownloadError(
                    f"failed to download {model_id}@{resolved_revision}: {error}"
                ) from error
            finally:
                monitor_stop.set()
                monitor.join(timeout=2)

            if progress_callback:
                progress_callback(1.0, bytes_on_disk, expected_bytes)
            return DownloadResult(
                path=target,
                revision=resolved_revision,
                cached=False,
                bytes_on_disk=bytes_on_disk,
            )

    def _resolve_revision(self, model_id: str, revision: str | None) -> tuple[str, int | None]:
        try:
            info = self.api.model_info(
                repo_id=model_id,
                revision=revision,
                files_metadata=True,
                token=self.token,
            )
        except Exception as error:
            raise ModelDownloadError(f"failed to read model metadata: {error}") from error
        resolved = info.sha
        if not resolved:
            raise ModelDownloadError("Hugging Face did not return an immutable revision")
        expected = 0
        has_sizes = False
        for sibling in info.siblings or []:
            size = getattr(sibling, "size", None)
            if isinstance(size, int):
                expected += size
                has_sizes = True
        return resolved, expected if has_sizes else None

    @staticmethod
    def _safe_segment(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "--", value).strip(".-") or "model"

    def _target_path(self, model_id: str, revision: str) -> Path:
        return self.models_dir / self._safe_segment(model_id) / self._safe_segment(revision)

    @staticmethod
    def _is_complete(marker: Path, model_id: str, revision: str, target: Path) -> bool:
        if not marker.is_file() or not (target / "config.json").is_file():
            return False
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return metadata.get("model_id") == model_id and metadata.get("revision") == revision

    @staticmethod
    def _validate_snapshot(target: Path) -> None:
        if not (target / "config.json").is_file():
            raise ModelDownloadError("downloaded snapshot has no config.json")
        has_weights = any(target.glob("*.safetensors")) or any(target.glob("*.bin"))
        if not has_weights:
            raise ModelDownloadError("downloaded snapshot has no supported weight files")

    @classmethod
    def _directory_size(cls, target: Path) -> int:
        total = 0
        for path in target.rglob("*"):
            try:
                if path.is_file() and path.name != ".uniform-download.lock":
                    total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    @classmethod
    def _monitor_progress(
        cls,
        target: Path,
        expected_bytes: int | None,
        callback: Callable[[float, int, int | None], None] | None,
        stop_event: threading.Event,
    ) -> None:
        if callback is None:
            return
        while not stop_event.wait(1):
            current = cls._directory_size(target)
            fraction = min(current / expected_bytes, 0.99) if expected_bytes else 0.0
            callback(fraction, current, expected_bytes)
