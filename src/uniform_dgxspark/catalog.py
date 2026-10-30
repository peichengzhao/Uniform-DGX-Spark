from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any
from urllib.parse import quote

from huggingface_hub import HfApi
from modelscope_hub import HubApi as ModelScopeApi

from .database import Database
from .schemas import CatalogModelRead, CatalogPublisherRead, CatalogSourceRead
from .store import to_iso, utc_now

HF_MODEL_EXPAND = [
    "author",
    "config",
    "createdAt",
    "downloads",
    "gated",
    "lastModified",
    "library_name",
    "likes",
    "pipeline_tag",
    "private",
    "safetensors",
    "sha",
    "tags",
    "trendingScore",
]

MODELSCOPE_REFRESH_TASKS = ("text-generation", "image-text-to-text")


class CatalogError(RuntimeError):
    pass


def _normalize_identifier(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _normalized_identity(item: CatalogModelRead) -> str:
    owner, separator, name = item.model_id.partition("/")
    if not separator:
        owner = item.author or ""
        name = item.model_id
    return f"{_normalize_identifier(owner)}/{_normalize_identifier(name)}"


def _normalized_model_name(item: CatalogModelRead) -> str:
    return _normalize_identifier(item.model_id.rpartition("/")[2])


def _publisher(item: CatalogModelRead) -> CatalogPublisherRead:
    owner = item.author or item.model_id.partition("/")[0] or "community"
    normalized_model = _normalized_model_name(item)
    model_publishers = (
        (("qwen",), "Qwen"),
        (("deepseek",), "deepseek-ai"),
        (("glm", "chatglm", "codegeex", "cogview"), "zai-org"),
        (("llama",), "meta-llama"),
        (("gemma",), "google"),
        (("phi",), "microsoft"),
        (("mistral", "mixtral", "codestral"), "mistralai"),
        (("nemotron",), "nvidia"),
    )
    for prefixes, publisher in model_publishers:
        if normalized_model.startswith(prefixes):
            owner = publisher
            break
    owner_aliases = {"zhipuai": "zai-org"}
    owner = owner_aliases.get(owner.casefold(), owner)
    display_names = {
        "01-ai": "01.AI",
        "baichuan-inc": "Baichuan",
        "deepseek-ai": "DeepSeek",
        "google": "Google",
        "iic": "ModelScope IIC",
        "meta-llama": "Meta",
        "microsoft": "Microsoft",
        "mistralai": "Mistral AI",
        "nvidia": "NVIDIA",
        "qwen": "Qwen",
        "zai-org": "Z.ai",
    }
    encoded_owner = quote(owner, safe="")
    encoded_model = quote(item.model_id, safe="")
    return CatalogPublisherRead(
        id=owner,
        name=display_names.get(owner.casefold(), owner),
        icon_url=(f"/api/v1/catalog/publishers/{encoded_owner}/icon?model_id={encoded_model}"),
    )


def sort_catalog_models(
    items: list[CatalogModelRead], sort: str = "popularity"
) -> list[CatalogModelRead]:
    def time_value(item: CatalogModelRead) -> float:
        published = item.created_at or item.last_modified
        return published.timestamp() if published else 0.0

    if sort == "recent":
        return sorted(items, key=lambda item: (-time_value(item), item.model_id.casefold()))
    if sort == "downloads":
        return sorted(
            items,
            key=lambda item: (-item.downloads, -item.likes, item.model_id.casefold()),
        )
    if sort == "likes":
        return sorted(
            items,
            key=lambda item: (-item.likes, -item.downloads, item.model_id.casefold()),
        )
    if sort == "name":
        return sorted(items, key=lambda item: item.model_id.casefold())
    return sorted(
        items,
        key=lambda item: (
            item.popularity_rank is None,
            item.popularity_rank if item.popularity_rank is not None else 1_000_000,
            -item.downloads,
            item.model_id.casefold(),
        ),
    )


def merge_catalog_models(
    items: list[CatalogModelRead], *, sort: str = "popularity"
) -> list[CatalogModelRead]:
    candidates: dict[str, list[CatalogModelRead]] = {}
    for item in items:
        candidates.setdefault(_normalized_model_name(item), []).append(item)
    groups: list[list[CatalogModelRead]] = []
    for candidates_with_name in candidates.values():
        source_counts = {
            source: sum(item.source == source for item in candidates_with_name)
            for source in {item.source for item in candidates_with_name}
        }
        if len(source_counts) > 1 and all(count == 1 for count in source_counts.values()):
            groups.append(candidates_with_name)
            continue
        exact_groups: dict[str, list[CatalogModelRead]] = {}
        for item in candidates_with_name:
            exact_groups.setdefault(_normalized_identity(item), []).append(item)
        groups.extend(exact_groups.values())
    merged: list[CatalogModelRead] = []
    compatibility_order = {"likely": 0, "experimental": 1, "incompatible": 2}
    for group in groups:
        ordered = sorted(
            group,
            key=lambda item: (
                item.source != "huggingface",
                item.popularity_rank if item.popularity_rank is not None else 1_000_000,
            ),
        )
        primary = ordered[0]
        best_compatibility = min(
            ordered,
            key=lambda item: compatibility_order.get(item.compatibility, 9),
        )
        tags = list(dict.fromkeys(tag for item in ordered for tag in item.tags))
        source_references = [
            CatalogSourceRead(
                source=item.source,  # type: ignore[arg-type]
                model_id=item.model_id,
                revision=item.revision,
                downloads=item.downloads,
                likes=item.likes,
            )
            for item in ordered
        ]
        metadata = dict(primary.metadata)
        for key in ("architecture", "model_type", "quantization", "parameter_count"):
            if metadata.get(key) is None:
                metadata[key] = next(
                    (item.metadata[key] for item in ordered if item.metadata.get(key) is not None),
                    None,
                )
        exact_size = next(
            (
                item.metadata.get("model_size_bytes")
                for item in ordered
                if item.metadata.get("model_size_bytes") is not None
                and not item.metadata.get("model_size_estimated", False)
            ),
            None,
        )
        if exact_size is not None:
            metadata["model_size_bytes"] = exact_size
            metadata["model_size_estimated"] = False
        elif metadata.get("model_size_bytes") is None:
            size_source = next(
                (
                    item.metadata
                    for item in ordered
                    if item.metadata.get("model_size_bytes") is not None
                ),
                {},
            )
            metadata["model_size_bytes"] = size_source.get("model_size_bytes")
            metadata["model_size_estimated"] = size_source.get("model_size_estimated", False)
        metadata.update(
            {
                "available_sources": [reference.source for reference in source_references],
                "primary_source": primary.source,
                "fallback_source": (
                    "modelscope"
                    if any(reference.source == "modelscope" for reference in source_references)
                    and primary.source == "huggingface"
                    else None
                ),
            }
        )
        merged.append(
            primary.model_copy(
                update={
                    "pipeline_tag": primary.pipeline_tag or best_compatibility.pipeline_tag,
                    "library_name": primary.library_name or best_compatibility.library_name,
                    "tags": tags,
                    "compatibility": best_compatibility.compatibility,
                    "compatibility_reason": best_compatibility.compatibility_reason,
                    "metadata": metadata,
                    "sources": source_references,
                    "publisher": _publisher(primary),
                }
            )
        )
    return sort_catalog_models(merged, sort)


class CatalogStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_many(self, items: list[CatalogModelRead]) -> int:
        if not items:
            return 0
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in items:
                connection.execute(
                    """
                    INSERT INTO model_catalog(
                        source, model_id, revision, author, pipeline_tag,
                        library_name, tags_json, downloads, likes, last_modified, created_at,
                        gated, private, compatibility, compatibility_reason,
                        metadata_json, refreshed_at, popularity_rank
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source, model_id) DO UPDATE SET
                        revision=excluded.revision,
                        author=excluded.author,
                        pipeline_tag=excluded.pipeline_tag,
                        library_name=excluded.library_name,
                        tags_json=excluded.tags_json,
                        downloads=excluded.downloads,
                        likes=excluded.likes,
                        last_modified=excluded.last_modified,
                        created_at=excluded.created_at,
                        gated=excluded.gated,
                        private=excluded.private,
                        compatibility=excluded.compatibility,
                        compatibility_reason=excluded.compatibility_reason,
                        metadata_json=json_patch(
                            model_catalog.metadata_json, excluded.metadata_json
                        ),
                        refreshed_at=excluded.refreshed_at,
                        popularity_rank=COALESCE(
                            excluded.popularity_rank, model_catalog.popularity_rank
                        )
                    """,
                    (
                        item.source,
                        item.model_id,
                        item.revision,
                        item.author,
                        item.pipeline_tag,
                        item.library_name,
                        json.dumps(item.tags, separators=(",", ":")),
                        item.downloads,
                        item.likes,
                        to_iso(item.last_modified) if item.last_modified else None,
                        to_iso(item.created_at) if item.created_at else None,
                        int(item.gated),
                        int(item.private),
                        item.compatibility,
                        item.compatibility_reason,
                        json.dumps(item.metadata, separators=(",", ":")),
                        to_iso(item.refreshed_at),
                        item.popularity_rank,
                    ),
                )
            connection.commit()
        return len(items)

    def search(
        self,
        *,
        query: str = "",
        source: str | None = None,
        compatibility: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CatalogModelRead], int]:
        clauses = ["model_id LIKE ?"]
        arguments: list[Any] = [f"%{query}%"]
        if source:
            clauses.append("source = ?")
            arguments.append(source)
        if compatibility == "deployable":
            clauses.append("compatibility != ?")
            arguments.append("incompatible")
        elif compatibility:
            clauses.append("compatibility = ?")
            arguments.append(compatibility)
        where = " AND ".join(clauses)
        with self.database.connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM model_catalog WHERE {where}", arguments
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY source
                        ORDER BY
                            CASE WHEN popularity_rank IS NULL THEN 1 ELSE 0 END,
                            popularity_rank ASC,
                            downloads DESC,
                            likes DESC,
                            model_id ASC
                    ) AS source_rank
                    FROM model_catalog WHERE {where}
                )
                SELECT * FROM ranked
                ORDER BY source_rank ASC, source ASC
                LIMIT ? OFFSET ?
                """,
                (*arguments, limit, offset),
            ).fetchall()
        return [self._from_row(row) for row in rows], total

    def search_merged(
        self,
        *,
        query: str = "",
        source: str | None = None,
        compatibility: str | None = None,
        sort: str = "popularity",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[CatalogModelRead], int]:
        items, _ = self.search(
            query=query,
            source=source,
            compatibility=compatibility,
            limit=10_000,
            offset=0,
        )
        merged = merge_catalog_models(items, sort=sort)
        return merged[offset : offset + limit], len(merged)

    def get(self, source: str, model_id: str) -> CatalogModelRead | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_catalog WHERE source = ? AND model_id = ?",
                (source, model_id),
            ).fetchone()
        return self._from_row(row) if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> CatalogModelRead:
        def parse_time(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

        return CatalogModelRead(
            source=row["source"],
            model_id=row["model_id"],
            revision=row["revision"],
            author=row["author"],
            pipeline_tag=row["pipeline_tag"],
            library_name=row["library_name"],
            tags=json.loads(row["tags_json"]),
            downloads=int(row["downloads"]),
            likes=int(row["likes"]),
            last_modified=parse_time(row["last_modified"]),
            created_at=parse_time(row["created_at"]),
            gated=bool(row["gated"]),
            private=bool(row["private"]),
            compatibility=row["compatibility"],
            compatibility_reason=row["compatibility_reason"],
            metadata=json.loads(row["metadata_json"]),
            refreshed_at=parse_time(row["refreshed_at"]),  # type: ignore[arg-type]
            popularity_rank=row["popularity_rank"],
        )


class CatalogService:
    def __init__(
        self,
        store: CatalogStore,
        *,
        hf_api: HfApi | None = None,
        hf_token: str | None = None,
        modelscope_api: ModelScopeApi | None = None,
        modelscope_token: str | None = None,
    ) -> None:
        self.store = store
        self.hf_api = hf_api or HfApi(token=hf_token)
        self.hf_token = hf_token
        self.modelscope_api = modelscope_api or ModelScopeApi(token=modelscope_token)
        self._api_lock = threading.Lock()
        self._metadata_lock = threading.BoundedSemaphore(3)

    def refresh_huggingface(self, *, limit: int = 100) -> int:
        try:
            with self._api_lock:
                models = list(
                    self.hf_api.list_models(
                        sort="trending_score",
                        direction=-1,
                        limit=limit,
                        expand=HF_MODEL_EXPAND,
                        token=self.hf_token,
                    )
                )
        except Exception as error:
            raise CatalogError(f"Hugging Face refresh failed: {error}") from error
        refreshed_at = utc_now()
        items = [
            self._from_huggingface(model, refreshed_at).model_copy(update={"popularity_rank": rank})
            for rank, model in enumerate(models, start=1)
        ]
        return self.store.upsert_many(items)

    def refresh_modelscope(self, *, limit: int = 100) -> int:
        models: dict[str, Any] = {}
        errors: list[str] = []
        for task in MODELSCOPE_REFRESH_TASKS:
            try:
                page_number = 1
                fetched = 0
                while fetched < limit:
                    page_size = min(50, limit - fetched)
                    with self._api_lock:
                        page = self.modelscope_api.list_repos(
                            "model",
                            sort="downloads",
                            page_number=page_number,
                            page_size=page_size,
                            task=task,
                        )
                    page_items = list(page.items)
                    fetched += len(page_items)
                    for model in page_items:
                        models[str(model.id)] = model
                    if len(page_items) < page_size:
                        break
                    page_number += 1
            except Exception as error:  # noqa: BLE001 - task filters are independent
                errors.append(f"{task}: {error}")
        if errors and not models:
            raise CatalogError(f"ModelScope refresh failed: {'; '.join(errors)}")
        refreshed_at = utc_now()
        ordered_models = sorted(
            models.values(),
            key=lambda model: (
                -int(getattr(model, "downloads", 0) or 0),
                -int(getattr(model, "likes", 0) or 0),
                str(getattr(model, "id", "")).casefold(),
            ),
        )[:limit]
        items = [
            self._from_modelscope(model, refreshed_at).model_copy(update={"popularity_rank": rank})
            for rank, model in enumerate(ordered_models, start=1)
        ]
        return self.store.upsert_many(items)

    def search_huggingface(self, query: str, *, limit: int = 25) -> list[CatalogModelRead]:
        try:
            with self._api_lock:
                models = list(
                    self.hf_api.list_models(
                        search=query,
                        sort="trending_score",
                        direction=-1,
                        limit=limit,
                        expand=HF_MODEL_EXPAND,
                        token=self.hf_token,
                    )
                )
        except Exception as error:
            raise CatalogError(f"Hugging Face search failed: {error}") from error
        refreshed_at = utc_now()
        items = [self._from_huggingface(model, refreshed_at) for model in models]
        self.store.upsert_many(items)
        return items

    def search_modelscope(self, query: str, *, limit: int = 25) -> list[CatalogModelRead]:
        try:
            with self._api_lock:
                page = self.modelscope_api.list_repos(
                    "model",
                    search=query,
                    sort="downloads",
                    page_number=1,
                    page_size=min(50, limit),
                )
        except Exception as error:
            raise CatalogError(f"ModelScope search failed: {error}") from error
        refreshed_at = utc_now()
        items = [self._from_modelscope(model, refreshed_at) for model in page.items]
        self.store.upsert_many(items)
        return items[:limit]

    def enrich_model_metadata(self, source: str, model_id: str) -> CatalogModelRead:
        item = self.store.get(source, model_id)
        if item is None:
            raise CatalogError("Model must be searched before loading its file metadata")
        if item.metadata.get("weight_metadata_verified"):
            return item
        try:
            if source == "huggingface":
                with self._metadata_lock:
                    info = self.hf_api.model_info(
                        model_id,
                        revision=item.revision,
                        files_metadata=True,
                        token=self.hf_token,
                    )
                files = [
                    (str(file.rfilename), int(file.size or 0)) for file in (info.siblings or [])
                ]
            elif source == "modelscope":
                with self._metadata_lock:
                    repo_files = self.modelscope_api.list_repo_files(
                        model_id, "model", revision=item.revision
                    )
                files = [
                    (str(file.path), int(file.size or 0))
                    for file in repo_files
                    if getattr(file, "type", "blob") == "blob"
                ]
            else:
                raise CatalogError(f"Unsupported catalog source: {source}")
        except CatalogError:
            raise
        except Exception as error:
            raise CatalogError(f"File metadata lookup failed: {error}") from error
        metadata = dict(item.metadata)
        metadata.update(self._summarize_repository_files(files))
        enriched = item.model_copy(update={"metadata": metadata})
        self.store.upsert_many([enriched])
        return enriched

    @classmethod
    def _from_huggingface(cls, model: Any, refreshed_at: datetime) -> CatalogModelRead:
        pipeline_tag = getattr(model, "pipeline_tag", None)
        library_name = getattr(model, "library_name", None)
        tags = list(getattr(model, "tags", None) or [])
        compatibility, reason = cls.classify_vllm(pipeline_tag, library_name, tags)
        last_modified = getattr(model, "last_modified", None)
        config = getattr(model, "config", None) or {}
        architectures = config.get("architectures") or []
        model_type = config.get("model_type") or cls._tag_value(tags, "model_type:")
        safetensors = getattr(model, "safetensors", None)
        parameters = getattr(safetensors, "parameters", None) or {}
        parameter_count = getattr(safetensors, "total", None)
        quantization_config = config.get("quantization_config")
        model_size = cls._estimated_weight_size(parameters, quantization_config)
        quantization = cls._quantization(str(model.id), tags, quantization_config, parameters)
        return CatalogModelRead(
            source="huggingface",
            model_id=model.id,
            revision=getattr(model, "sha", None),
            author=getattr(model, "author", None),
            pipeline_tag=pipeline_tag,
            library_name=library_name,
            tags=tags,
            downloads=int(getattr(model, "downloads", 0) or 0),
            likes=int(getattr(model, "likes", 0) or 0),
            last_modified=last_modified,
            created_at=getattr(model, "created_at", None),
            gated=bool(getattr(model, "gated", False)),
            private=bool(getattr(model, "private", False)),
            compatibility=compatibility,
            compatibility_reason=reason,
            metadata={
                "trending_score": getattr(model, "trending_score", None),
                "popularity_basis": "huggingface_trending_score",
                "architecture": architectures[0] if architectures else model_type,
                "model_type": model_type,
                "quantization": quantization,
                "parameter_count": parameter_count,
                "model_size_bytes": model_size,
                "model_size_estimated": model_size is not None,
            },
            refreshed_at=refreshed_at,
        )

    @classmethod
    def _from_modelscope(cls, model: Any, refreshed_at: datetime) -> CatalogModelRead:
        tags = list(getattr(model, "tags", None) or [])
        tasks = list(getattr(model, "tasks", None) or [])
        pipeline_tag = tasks[0] if tasks else cls._tag_value(tags, "task:")
        library_name = cls._tag_value(tags, "library:")
        compatibility, reason = cls.classify_vllm(pipeline_tag, library_name, tags)
        model_type = cls._tag_value(tags, "model_type:")
        file_size = getattr(model, "file_size", None)
        return CatalogModelRead(
            source="modelscope",
            model_id=str(model.id),
            revision=None,
            author=getattr(model, "owner", None),
            pipeline_tag=pipeline_tag,
            library_name=library_name,
            tags=tags,
            downloads=int(getattr(model, "downloads", 0) or 0),
            likes=int(getattr(model, "likes", 0) or 0),
            last_modified=getattr(model, "last_modified", None),
            created_at=getattr(model, "created_at", None),
            gated=bool(getattr(model, "gated", False)),
            private=bool(getattr(model, "private", False)),
            compatibility=compatibility,
            compatibility_reason=reason,
            metadata={
                "file_size": file_size,
                "license": getattr(model, "license", None),
                "popularity_basis": "modelscope_downloads",
                "architecture": model_type,
                "model_type": model_type,
                "quantization": cls._quantization(str(model.id), tags, None, {}),
                "parameter_count": None,
                "model_size_bytes": None,
                "model_size_estimated": False,
                "repository_size_bytes": file_size,
            },
            refreshed_at=refreshed_at,
        )

    @staticmethod
    def _tag_value(tags: list[str], prefix: str) -> str | None:
        for tag in tags:
            if tag.startswith(prefix):
                return tag.removeprefix(prefix)
        return None

    @staticmethod
    def _estimated_weight_size(
        parameters: dict[str, int], quantization_config: object = None
    ) -> int | None:
        if not parameters:
            return None
        if isinstance(quantization_config, dict):
            bits = quantization_config.get("bits")
            if isinstance(bits, int | float) and 0 < bits <= 16:
                return int(sum(int(count) for count in parameters.values()) * bits / 8)
        bytes_per_parameter = {
            "F64": 8,
            "F32": 4,
            "F16": 2,
            "BF16": 2,
            "I64": 8,
            "I32": 4,
            "I16": 2,
            "I8": 1,
            "U8": 1,
            "BOOL": 1,
            "F8_E4M3": 1,
            "F8_E5M2": 1,
        }
        return sum(
            int(count) * bytes_per_parameter.get(str(dtype).upper(), 2)
            for dtype, count in parameters.items()
        )

    @staticmethod
    def _quantization(
        model_id: str,
        tags: list[str],
        quantization_config: object,
        parameters: dict[str, int],
    ) -> str | None:
        searchable = " ".join([model_id, *tags]).casefold()
        explicit_labels = (
            ("nvfp4", "NVFP4"),
            ("gptq", "GPTQ"),
            ("awq", "AWQ"),
            ("gguf", "GGUF"),
            ("exl2", "EXL2"),
        )
        for marker, label in explicit_labels:
            if marker in searchable:
                return label
        if isinstance(quantization_config, dict):
            method = quantization_config.get("quant_method") or quantization_config.get(
                "quantization_method"
            )
            bits = quantization_config.get("bits")
            if method:
                return f"{str(method).upper()}{f' {bits}-bit' if bits else ''}"
        labels = (
            ("fp8", "FP8"),
            ("int8", "INT8"),
            ("int4", "INT4"),
            ("4bit", "4-bit"),
            ("4-bit", "4-bit"),
        )
        for marker, label in labels:
            if marker in searchable:
                return label
        precision = [str(dtype).upper() for dtype, count in parameters.items() if count]
        return "/".join(precision) if precision else None

    @staticmethod
    def _summarize_repository_files(files: list[tuple[str, int]]) -> dict[str, Any]:
        normalized = [(name, size) for name, size in files if size >= 0]
        repository_size = sum(size for _, size in normalized)
        by_extension: dict[str, list[tuple[str, int]]] = {}
        for name, size in normalized:
            lowered = name.casefold()
            for extension in (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt"):
                if lowered.endswith(extension):
                    by_extension.setdefault(extension, []).append((name, size))
                    break
        selected: list[tuple[str, int]] = []
        weight_format: str | None = None
        for extension in (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".ckpt"):
            if by_extension.get(extension):
                selected = by_extension[extension]
                weight_format = extension.removeprefix(".").upper()
                break
        variants: list[dict[str, Any]] = []
        if weight_format == "GGUF" and len(selected) > 1:
            variants = [
                {"name": name, "size_bytes": size}
                for name, size in sorted(selected, key=lambda pair: pair[1])
            ]
            weight_size: int | None = None
        else:
            weight_size = sum(size for _, size in selected) if selected else None
        return {
            "weight_size_bytes": weight_size,
            "repository_size_bytes": repository_size,
            "weight_file_count": len(selected),
            "weight_format": weight_format,
            "weight_variants": variants,
            "weight_metadata_verified": True,
            "model_size_bytes": weight_size,
            "model_size_estimated": False,
        }

    @staticmethod
    def classify_vllm(
        pipeline_tag: str | None, library_name: str | None, tags: list[str]
    ) -> tuple[str, str]:
        incompatible_tasks = {
            "text-to-image",
            "image-classification",
            "object-detection",
            "text-to-speech",
            "automatic-speech-recognition",
            "text-to-image-synthesis",
        }
        if pipeline_tag in incompatible_tasks:
            return "incompatible", f"task {pipeline_tag} is not served by vLLM"
        if pipeline_tag in {"text-generation", "image-text-to-text"}:
            if library_name in {None, "transformer", "transformers", "vllm"}:
                return "likely", "generation model; final architecture check occurs before deploy"
        if "vllm" in tags:
            return "likely", "model is tagged for vLLM"
        return "experimental", "insufficient metadata; deployment requires preflight"
