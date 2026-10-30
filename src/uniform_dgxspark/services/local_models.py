from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..database import Database
from ..schemas import LocalModelInventory, LocalModelRead, LocalModelRootRead
from ..store import to_iso, utc_now

WEIGHT_SUFFIXES = {".safetensors", ".bin", ".gguf", ".pt", ".pth", ".onnx", ".engine", ".plan"}
TOKENIZER_FILES = {
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
}
LFS_POINTER_MAX_BYTES = 1024
LFS_OID_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class _LFSPointer:
    oid: str
    size: int


@dataclass(frozen=True)
class _WeightInspection:
    usable_bytes: int
    expected_bytes: int
    downloaded_bytes: int
    usable: bool
    lfs_incomplete: bool


class LocalModelRootError(ValueError):
    pass


class LocalModelService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add_root(self, value: str | Path) -> LocalModelRootRead:
        path = self._validate_root(value)
        now = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO local_model_roots(path, created_at) VALUES (?, ?)",
                (str(path), now),
            )
            connection.commit()
        return next(root for root in self.list_roots() if root.path == str(path))

    def seed_roots(self, paths: tuple[Path, ...]) -> None:
        for path in paths:
            self.add_root(path)

    def remove_root(self, value: str | Path) -> bool:
        path = str(Path(value).expanduser().resolve(strict=False))
        with self.database.connect() as connection:
            cursor = connection.execute("DELETE FROM local_model_roots WHERE path = ?", (path,))
            connection.commit()
        return cursor.rowcount > 0

    def list_roots(self) -> list[LocalModelRootRead]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT path, created_at FROM local_model_roots ORDER BY created_at, path"
            ).fetchall()
        return [
            LocalModelRootRead(
                path=row["path"],
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            )
            for row in rows
        ]

    def scan(self) -> LocalModelInventory:
        roots = self.list_roots()
        items: list[LocalModelRead] = []
        errors: dict[str, str] = {}
        seen: set[Path] = set()
        for root in roots:
            root_path = Path(root.path)
            try:
                candidates = self._candidate_directories(root_path)
                for candidate in candidates:
                    resolved = candidate.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    items.append(self._inspect_model(candidate, root_path))
            except (OSError, ValueError) as error:
                errors[root.path] = str(error)
        items.sort(
            key=lambda item: item.modified_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return LocalModelInventory(
            roots=roots,
            items=items,
            total=len(items),
            total_bytes=sum(item.total_bytes for item in items),
            errors=errors,
        )

    def get_registered_model(self, value: str | Path) -> LocalModelRead:
        requested = Path(value).expanduser().resolve(strict=True)
        inventory = self.scan()
        for item in inventory.items:
            if Path(item.path).resolve() == requested:
                return item
        raise LocalModelRootError("local model is not inside a registered model root")

    @staticmethod
    def _validate_root(value: str | Path) -> Path:
        raw = str(value).strip()
        if not raw:
            raise LocalModelRootError("model root path is required")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise LocalModelRootError("model root path must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise LocalModelRootError(f"model root does not exist: {path}") from error
        if not resolved.is_dir():
            raise LocalModelRootError("model root must be a directory")
        if resolved == Path(resolved.anchor):
            raise LocalModelRootError("filesystem root cannot be used as a model root")
        return resolved

    @classmethod
    def _candidate_directories(cls, root: Path) -> list[Path]:
        if not root.is_dir():
            raise LocalModelRootError("configured model root is unavailable")
        if cls._has_model_markers(root):
            return [root]
        return [
            child
            for child in root.iterdir()
            if child.is_dir() and not child.name.startswith(".") and cls._has_model_markers(child)
        ]

    @staticmethod
    def _has_model_markers(path: Path) -> bool:
        if (path / "config.json").is_file():
            return True
        try:
            return any(
                entry.is_file() and entry.suffix.lower() in WEIGHT_SUFFIXES
                for entry in path.iterdir()
            )
        except OSError:
            return False

    @classmethod
    def _inspect_model(cls, path: Path, root: Path) -> LocalModelRead:
        config = cls._read_config(path / "config.json")
        weights: list[Path] = []
        total_files = 0
        physical_total_bytes = 0
        latest_mtime = 0.0
        for directory, directory_names, filenames in os.walk(path, followlinks=False):
            directory_names[:] = [name for name in directory_names if not name.startswith(".")]
            for filename in filenames:
                file_path = Path(directory) / filename
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                total_files += 1
                physical_total_bytes += stat.st_size
                latest_mtime = max(latest_mtime, stat.st_mtime)
                if file_path.suffix.lower() in WEIGHT_SUFFIXES:
                    weights.append(file_path)

        inspected_weights = [cls._inspect_weight(path, file) for file in weights]
        raw_weight_bytes = sum(cls._safe_file_size(file) for file in weights)
        weight_bytes = sum(item.usable_bytes for item in inspected_weights)
        expected_weight_bytes = sum(item.expected_bytes for item in inspected_weights)
        downloaded_weight_bytes = sum(item.downloaded_bytes for item in inspected_weights)
        usable_weight_files = sum(item.usable for item in inspected_weights)
        total_bytes = physical_total_bytes - raw_weight_bytes + expected_weight_bytes

        formats = sorted({file.suffix.lower().lstrip(".").upper() for file in weights})
        quantization = cls._quantization_label(config)
        architectures = config.get("architectures") or []
        architecture = str(architectures[0]) if architectures else None
        missing = cls._missing_index_weights(path)
        incomplete_weights = missing + sum(not item.usable for item in inspected_weights)
        has_lfs_incomplete = any(item.lfs_incomplete for item in inspected_weights)
        has_config = (path / "config.json").is_file()
        ready = (
            bool(weights)
            and (has_config or any(file.suffix.lower() == ".gguf" for file in weights))
            and incomplete_weights == 0
        )
        status = "ready" if ready else ("incomplete" if weights or has_config else "unrecognized")
        status_reason = None
        if status == "incomplete":
            if has_lfs_incomplete:
                status_reason = "Git LFS 下载未完成"
            elif incomplete_weights:
                status_reason = "权重文件不完整"
            elif not weights:
                status_reason = "未找到权重文件"
            else:
                status_reason = "缺少部署所需配置"
        return LocalModelRead(
            name=path.name,
            path=str(path.resolve()),
            root_path=str(root.resolve()),
            architecture=architecture,
            model_type=str(config.get("model_type")) if config.get("model_type") else None,
            quantization=quantization,
            weight_format=" + ".join(formats) if formats else None,
            weight_files=len(weights),
            usable_weight_files=usable_weight_files,
            weight_bytes=weight_bytes,
            expected_weight_bytes=expected_weight_bytes,
            downloaded_weight_bytes=downloaded_weight_bytes,
            incomplete_weight_files=incomplete_weights,
            total_files=total_files,
            total_bytes=total_bytes,
            modified_at=(datetime.fromtimestamp(latest_mtime, tz=UTC) if latest_mtime else None),
            has_config=has_config,
            has_tokenizer=any((path / name).is_file() for name in TOKENIZER_FILES),
            missing_weight_files=missing,
            status=status,
            status_reason=status_reason,
        )

    @classmethod
    def _inspect_weight(cls, model_path: Path, file_path: Path) -> _WeightInspection:
        actual_bytes = cls._safe_file_size(file_path)
        pointer = cls._read_lfs_pointer(file_path)
        if pointer is None and actual_bytes == 0:
            pointer = cls._read_git_lfs_pointer(model_path, file_path)

        if pointer is None:
            return _WeightInspection(
                usable_bytes=actual_bytes,
                expected_bytes=actual_bytes,
                downloaded_bytes=actual_bytes,
                usable=actual_bytes > 0,
                lfs_incomplete=False,
            )

        lfs_downloaded, lfs_incomplete = cls._lfs_download_state(model_path, pointer)
        return _WeightInspection(
            usable_bytes=0,
            expected_bytes=pointer.size,
            downloaded_bytes=min(pointer.size, lfs_downloaded),
            usable=False,
            lfs_incomplete=lfs_incomplete or lfs_downloaded < pointer.size,
        )

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @classmethod
    def _read_lfs_pointer(cls, path: Path) -> _LFSPointer | None:
        try:
            if path.stat().st_size > LFS_POINTER_MAX_BYTES:
                return None
            return cls._parse_lfs_pointer(path.read_bytes())
        except OSError:
            return None

    @staticmethod
    def _parse_lfs_pointer(payload: bytes) -> _LFSPointer | None:
        try:
            lines = payload.decode("ascii").replace("\r\n", "\n").splitlines()
        except UnicodeDecodeError:
            return None
        if not lines or lines[0] != "version https://git-lfs.github.com/spec/v1":
            return None
        values = dict(line.split(" ", 1) for line in lines[1:] if " " in line)
        match = LFS_OID_PATTERN.fullmatch(values.get("oid", ""))
        try:
            size = int(values.get("size", ""))
        except ValueError:
            return None
        if match is None or size <= 0:
            return None
        return _LFSPointer(oid=match.group(1), size=size)

    @classmethod
    def _read_git_lfs_pointer(cls, model_path: Path, file_path: Path) -> _LFSPointer | None:
        if not (model_path / ".git").exists():
            return None
        try:
            relative_path = file_path.relative_to(model_path).as_posix()
        except ValueError:
            return None
        object_name = f"HEAD:{relative_path}"
        try:
            size_result = subprocess.run(
                ["git", "-C", str(model_path), "cat-file", "-s", object_name],
                capture_output=True,
                check=False,
                text=True,
                timeout=2,
            )
            if size_result.returncode != 0:
                return None
            blob_size = int(size_result.stdout.strip())
            if blob_size > LFS_POINTER_MAX_BYTES:
                return None
            pointer_result = subprocess.run(
                ["git", "-C", str(model_path), "cat-file", "-p", object_name],
                capture_output=True,
                check=False,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return None
        if pointer_result.returncode != 0:
            return None
        return cls._parse_lfs_pointer(pointer_result.stdout)

    @classmethod
    def _lfs_download_state(cls, model_path: Path, pointer: _LFSPointer) -> tuple[int, bool]:
        git_dir = model_path / ".git"
        if not git_dir.is_dir():
            return 0, False

        complete_object = git_dir / "lfs" / "objects" / pointer.oid[:2] / pointer.oid[2:4]
        complete_sizes = [
            cls._safe_file_size(candidate)
            for candidate in complete_object.glob(f"{pointer.oid}*")
            if candidate.is_file()
        ]
        incomplete_dir = git_dir / "lfs" / "incomplete"
        incomplete_sizes = [
            cls._safe_file_size(candidate)
            for candidate in incomplete_dir.glob(f"{pointer.oid}*")
            if candidate.is_file()
        ]
        downloaded = max([0, *complete_sizes, *incomplete_sizes])
        return downloaded, bool(incomplete_sizes and downloaded < pointer.size)

    @staticmethod
    def _read_config(path: Path) -> dict[str, Any]:
        try:
            if path.stat().st_size > 4_000_000:
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @classmethod
    def _missing_index_weights(cls, path: Path) -> int:
        indexes = list(path.glob("*.safetensors.index.json"))
        if not indexes:
            return 0
        try:
            payload = cls._read_config(indexes[0])
            expected = set((payload.get("weight_map") or {}).values())
            return sum(not (path / filename).is_file() for filename in expected)
        except (OSError, AttributeError):
            return 0

    @staticmethod
    def _quantization_label(config: dict[str, Any]) -> str | None:
        quantization = config.get("quantization_config")
        if not isinstance(quantization, dict):
            return None
        method = quantization.get("quant_algo") or quantization.get("quant_method")
        bits = quantization.get("bits")
        if method and bits and str(bits) not in str(method):
            return f"{method} {bits}-bit"
        return str(method) if method else (f"{bits}-bit" if bits else None)
