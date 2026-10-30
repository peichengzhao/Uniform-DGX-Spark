from __future__ import annotations

import fcntl
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from modelscope_hub import HubApi

from .downloads import DownloadResult, ModelDownloadError


class ModelScopeDownloadManager:
    def __init__(
        self,
        models_dir: Path,
        *,
        token: str | None = None,
        max_workers: int = 4,
        api: HubApi | None = None,
    ) -> None:
        self.models_dir = models_dir
        self.max_workers = max_workers
        self.api = api or HubApi(token=token)

    def download(
        self,
        model_id: str,
        revision: str | None,
        *,
        progress_callback: Callable[[float, int, int | None], None] | None = None,
    ) -> DownloadResult:
        resolved_revision = revision or "master"
        target = self._target_path(model_id, resolved_revision)
        target.mkdir(parents=True, exist_ok=True)
        marker = target / ".uniform-download-complete.json"
        lock_path = target / ".uniform-download.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            if self._is_complete(marker, model_id, resolved_revision, target):
                size = self._directory_size(target)
                if progress_callback:
                    progress_callback(1, size, None)
                return DownloadResult(target, resolved_revision, True, size)
            try:
                self.api.download_repo(
                    model_id,
                    "model",
                    revision=resolved_revision,
                    local_dir=target,
                    max_workers=self.max_workers,
                )
                self._validate_snapshot(target)
                size = self._directory_size(target)
                temporary_marker = marker.with_suffix(".tmp")
                temporary_marker.write_text(
                    json.dumps(
                        {
                            "model_id": model_id,
                            "revision": resolved_revision,
                            "bytes_on_disk": size,
                            "completed_at": time.time(),
                        },
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary_marker, marker)
            except Exception as error:
                raise ModelDownloadError(
                    f"failed to download ModelScope {model_id}@{resolved_revision}: {error}"
                ) from error
            if progress_callback:
                progress_callback(1, size, None)
            return DownloadResult(target, resolved_revision, False, size)

    @staticmethod
    def _safe_segment(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "--", value).strip(".-") or "model"

    def _target_path(self, model_id: str, revision: str) -> Path:
        return (
            self.models_dir
            / "modelscope"
            / self._safe_segment(model_id)
            / self._safe_segment(revision)
        )

    @staticmethod
    def _directory_size(target: Path) -> int:
        return sum(
            path.stat().st_size
            for path in target.rglob("*")
            if path.is_file() and path.name != ".uniform-download.lock"
        )

    @staticmethod
    def _validate_snapshot(target: Path) -> None:
        if not (target / "config.json").is_file():
            raise ModelDownloadError("downloaded snapshot has no config.json")
        if not any(target.glob("*.safetensors")) and not any(target.glob("*.bin")):
            raise ModelDownloadError("downloaded snapshot has no supported weight files")

    @staticmethod
    def _is_complete(marker: Path, model_id: str, revision: str, target: Path) -> bool:
        if not marker.is_file() or not (target / "config.json").is_file():
            return False
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return value.get("model_id") == model_id and value.get("revision") == revision
