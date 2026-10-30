from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from uniform_dgxspark.catalog import CatalogService, CatalogStore
from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import CatalogModelRead


def catalog_item(source: str, model_id: str, *, rank: int = 1) -> CatalogModelRead:
    return CatalogModelRead(
        source=source,
        model_id=model_id,
        revision=f"{source}-revision",
        author=model_id.partition("/")[0],
        pipeline_tag="text-generation",
        library_name="transformers",
        tags=[source, "safetensors"],
        downloads=200 if source == "modelscope" else 100,
        likes=20 if source == "modelscope" else 10,
        last_modified=datetime(2026, 8, 31, tzinfo=UTC),
        gated=False,
        private=False,
        compatibility="likely",
        compatibility_reason="Text generation model",
        metadata={},
        refreshed_at=datetime(2026, 8, 31, tzinfo=UTC),
        popularity_rank=rank,
    )


class FakeHfApi:
    def list_models(self, **kwargs: object) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                id="org/generator",
                sha="abc",
                author="org",
                pipeline_tag="text-generation",
                library_name="transformers",
                tags=["safetensors"],
                config={"architectures": ["GeneratorForCausalLM"], "model_type": "generator"},
                safetensors=SimpleNamespace(parameters={"BF16": 1000}, total=1000),
                downloads=100,
                likes=5,
                last_modified=datetime(2026, 8, 31, tzinfo=UTC),
                gated=False,
                private=False,
            ),
            SimpleNamespace(
                id="org/image-model",
                sha="def",
                author="org",
                pipeline_tag="text-to-image",
                library_name="diffusers",
                tags=[],
                config={"architectures": ["ImageModel"], "model_type": "image"},
                safetensors=None,
                downloads=50,
                likes=2,
                last_modified=datetime(2026, 8, 30, tzinfo=UTC),
                gated=False,
                private=False,
            ),
        ]

    def model_info(self, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            siblings=[
                SimpleNamespace(rfilename="model-00001.safetensors", size=1200),
                SimpleNamespace(rfilename="model-00002.safetensors", size=1800),
                SimpleNamespace(rfilename="config.json", size=100),
            ]
        )


class FakeModelScopeApi:
    def list_repos(self, *args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    id="Qwen/Qwen-Test",
                    owner="Qwen",
                    tasks=["text-generation"],
                    tags=["library:transformer", "library:safetensors"],
                    downloads=200,
                    likes=10,
                    last_modified=datetime(2026, 8, 31, tzinfo=UTC),
                    gated=False,
                    private=False,
                    file_size=1234,
                    license="Apache-2.0",
                )
            ]
        )


class PaginatedModelScopeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def list_repos(self, *args: object, **kwargs: object) -> SimpleNamespace:
        page_number = int(kwargs["page_number"])
        page_size = int(kwargs["page_size"])
        self.calls.append((page_number, page_size))
        assert page_size <= 50
        start = (page_number - 1) * 50
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    id=f"org/model-{index}",
                    owner="org",
                    tasks=["text-generation"],
                    tags=[],
                    downloads=0,
                    likes=0,
                    last_modified=None,
                    gated=False,
                    private=False,
                    file_size=None,
                    license=None,
                )
                for index in range(start, start + page_size)
            ]
        )


def test_huggingface_refresh_search_and_compatibility(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    service = CatalogService(store, hf_api=FakeHfApi())  # type: ignore[arg-type]

    assert service.refresh_huggingface(limit=2) == 2
    items, total = store.search(query="org/")

    assert total == 2
    assert items[0].model_id == "org/generator"
    assert items[0].compatibility == "likely"
    assert items[0].metadata["architecture"] == "GeneratorForCausalLM"
    assert items[0].metadata["quantization"] == "BF16"
    assert items[0].metadata["parameter_count"] == 1000
    assert items[0].metadata["model_size_bytes"] == 2000
    assert items[0].metadata["model_size_estimated"] is True
    assert items[1].compatibility == "incompatible"

    filtered, filtered_total = store.search(compatibility="likely")
    assert filtered_total == 1
    assert filtered[0].model_id == "org/generator"


def test_modelscope_refresh_is_normalized(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    service = CatalogService(
        store,
        hf_api=FakeHfApi(),  # type: ignore[arg-type]
        modelscope_api=FakeModelScopeApi(),  # type: ignore[arg-type]
    )

    assert service.refresh_modelscope(limit=1) == 1
    items, total = store.search(source="modelscope")

    assert total == 1
    assert items[0].model_id == "Qwen/Qwen-Test"
    assert items[0].compatibility == "likely"
    assert items[0].metadata["file_size"] == 1234
    assert items[0].metadata["model_size_bytes"] is None
    assert items[0].metadata["repository_size_bytes"] == 1234
    assert items[0].metadata["architecture"] is None


def test_quantization_is_normalized_from_config_tags_and_model_name() -> None:
    assert CatalogService._quantization(
        "org/model", [], {"quant_method": "awq", "bits": 4}, {}
    ) == "AWQ 4-bit"
    assert CatalogService._quantization("org/model-GGUF", [], None, {}) == "GGUF"
    assert CatalogService._quantization("org/model", ["fp8"], None, {}) == "FP8"
    assert CatalogService._quantization(
        "org/model-NVFP4", [], {"quant_method": "fp8"}, {}
    ) == "NVFP4"
    assert CatalogService._estimated_weight_size(
        {"F16": 8_000_000_000}, {"quant_method": "awq", "bits": 4}
    ) == 4_000_000_000


def test_file_metadata_replaces_parameter_estimate_with_actual_weight_size(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    service = CatalogService(store, hf_api=FakeHfApi())  # type: ignore[arg-type]
    service.refresh_huggingface(limit=2)

    enriched = service.enrich_model_metadata("huggingface", "org/generator")

    assert enriched.metadata["weight_size_bytes"] == 3000
    assert enriched.metadata["repository_size_bytes"] == 3100
    assert enriched.metadata["weight_file_count"] == 2
    assert enriched.metadata["weight_format"] == "SAFETENSORS"
    assert enriched.metadata["weight_metadata_verified"] is True
    assert enriched.metadata["model_size_estimated"] is False

    service.refresh_huggingface(limit=2)
    cached = store.get("huggingface", "org/generator")
    assert cached is not None
    assert cached.metadata["weight_size_bytes"] == 3000


def test_gguf_variants_are_not_incorrectly_summed() -> None:
    metadata = CatalogService._summarize_repository_files(
        [
            ("model-Q4.gguf", 4_000),
            ("model-Q8.gguf", 8_000),
            ("README.md", 100),
        ]
    )

    assert metadata["weight_size_bytes"] is None
    assert metadata["repository_size_bytes"] == 12_100
    assert metadata["weight_file_count"] == 2
    assert metadata["weight_variants"] == [
        {"name": "model-Q4.gguf", "size_bytes": 4_000},
        {"name": "model-Q8.gguf", "size_bytes": 8_000},
    ]


def test_modelscope_refresh_paginates_at_api_page_limit(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    api = PaginatedModelScopeApi()
    service = CatalogService(
        store,
        hf_api=FakeHfApi(),  # type: ignore[arg-type]
        modelscope_api=api,  # type: ignore[arg-type]
    )

    assert service.refresh_modelscope(limit=120) == 120
    assert api.calls == [(1, 50), (2, 50), (3, 20), (1, 50), (2, 50), (3, 20)]
    assert store.search(source="modelscope", limit=200)[1] == 120


def test_live_search_populates_local_cache(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    service = CatalogService(
        store,
        hf_api=FakeHfApi(),  # type: ignore[arg-type]
        modelscope_api=FakeModelScopeApi(),  # type: ignore[arg-type]
    )

    huggingface = service.search_huggingface("generator", limit=10)
    modelscope = service.search_modelscope("Qwen", limit=10)

    assert len(huggingface) == 2
    assert len(modelscope) == 1
    assert store.search(limit=20)[1] == 3


def test_catalog_requests_popularity_sorting(tmp_path: Path) -> None:
    class RecordingHfApi(FakeHfApi):
        def __init__(self) -> None:
            self.sorts: list[str] = []

        def list_models(self, **kwargs: object) -> list[SimpleNamespace]:
            self.sorts.append(str(kwargs["sort"]))
            return super().list_models(**kwargs)

    class RecordingModelScopeApi(FakeModelScopeApi):
        def __init__(self) -> None:
            self.sorts: list[str] = []
            self.tasks: list[str | None] = []

        def list_repos(self, *args: object, **kwargs: object) -> SimpleNamespace:
            self.sorts.append(str(kwargs["sort"]))
            self.tasks.append(kwargs.get("task"))  # type: ignore[arg-type]
            return super().list_repos(*args, **kwargs)

    database = Database(tmp_path / "uniform.db")
    database.initialize()
    huggingface = RecordingHfApi()
    modelscope = RecordingModelScopeApi()
    service = CatalogService(
        CatalogStore(database),
        hf_api=huggingface,  # type: ignore[arg-type]
        modelscope_api=modelscope,  # type: ignore[arg-type]
    )

    service.refresh_huggingface(limit=2)
    service.search_huggingface("qwen", limit=2)
    service.refresh_modelscope(limit=1)
    service.search_modelscope("qwen", limit=1)

    assert huggingface.sorts == ["trending_score", "trending_score"]
    assert modelscope.sorts == ["downloads", "downloads", "downloads"]
    assert modelscope.tasks == ["text-generation", "image-text-to-text", None]


def test_modelscope_refresh_limits_sync_to_deployable_generation_tasks(
    tmp_path: Path,
) -> None:
    class TaskAwareModelScopeApi:
        def __init__(self) -> None:
            self.tasks: list[str] = []

        def list_repos(self, *args: object, **kwargs: object) -> SimpleNamespace:
            task = str(kwargs["task"])
            self.tasks.append(task)
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        id="Qwen/Qwen3-8B" if task == "text-generation" else "Qwen/Qwen3-VL-8B",
                        owner="Qwen",
                        tasks=[task],
                        tags=["library:transformers"],
                        downloads=100 if task == "text-generation" else 200,
                        likes=10,
                        last_modified=None,
                        gated=False,
                        private=False,
                        file_size=None,
                        license=None,
                    )
                ]
            )

    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    api = TaskAwareModelScopeApi()
    service = CatalogService(
        store,
        hf_api=FakeHfApi(),  # type: ignore[arg-type]
        modelscope_api=api,  # type: ignore[arg-type]
    )

    assert service.refresh_modelscope(limit=2) == 2
    assert api.tasks == ["text-generation", "image-text-to-text"]
    items, total = store.search(source="modelscope", limit=10)
    assert total == 2
    assert {item.model_id for item in items} == {
        "Qwen/Qwen3-8B",
        "Qwen/Qwen3-VL-8B",
    }
    assert {item.compatibility for item in items} == {"likely"}


def test_merged_catalog_prefers_huggingface_and_keeps_modelscope_fallback(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    store.upsert_many(
        [
            catalog_item("huggingface", "Qwen/Qwen3-8B"),
            catalog_item("modelscope", "AI-ModelScope/Qwen3-8B"),
        ]
    )

    items, total = store.search_merged()

    assert total == 1
    assert items[0].source == "huggingface"
    assert [source.source for source in items[0].sources] == [
        "huggingface",
        "modelscope",
    ]
    assert items[0].metadata["fallback_source"] == "modelscope"
    assert items[0].publisher is not None
    assert items[0].publisher.name == "Qwen"
    assert items[0].publisher.icon_url.startswith(
        "/api/v1/catalog/publishers/Qwen/icon"
    )


def test_same_name_is_not_collapsed_when_one_source_has_multiple_publishers(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    store.upsert_many(
        [
            catalog_item("huggingface", "publisher-a/shared-name"),
            catalog_item("huggingface", "publisher-b/shared-name"),
            catalog_item("modelscope", "publisher-c/shared-name"),
        ]
    )

    _, total = store.search_merged()

    assert total == 3


def test_merged_catalog_does_not_mix_quantization_variants(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    store.upsert_many(
        [
            catalog_item("modelscope", "Qwen/Qwen3-8B"),
            catalog_item("huggingface", "Qwen/Qwen3-8B-AWQ"),
            catalog_item("huggingface", "deepseek-ai/DeepSeek-R1"),
        ]
    )

    items, total = store.search_merged()

    assert total == 3
    assert {item.model_id for item in items} == {
        "Qwen/Qwen3-8B",
        "Qwen/Qwen3-8B-AWQ",
        "deepseek-ai/DeepSeek-R1",
    }


def test_merged_catalog_sort_options(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = CatalogStore(database)
    oldest = catalog_item("huggingface", "org/Alpha", rank=3).model_copy(
        update={
            "created_at": datetime(2024, 1, 1, tzinfo=UTC),
            "downloads": 900,
            "likes": 2,
        }
    )
    newest = catalog_item("huggingface", "org/Beta", rank=2).model_copy(
        update={
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "downloads": 20,
            "likes": 50,
        }
    )
    popular = catalog_item("huggingface", "org/Gamma", rank=1).model_copy(
        update={
            "created_at": datetime(2025, 1, 1, tzinfo=UTC),
            "downloads": 100,
            "likes": 10,
        }
    )
    store.upsert_many([oldest, newest, popular])

    assert store.search_merged(sort="recent")[0][0].model_id == "org/Beta"
    assert store.search_merged(sort="downloads")[0][0].model_id == "org/Alpha"
    assert store.search_merged(sort="likes")[0][0].model_id == "org/Beta"
    assert [item.model_id for item in store.search_merged(sort="name")[0]] == [
        "org/Alpha",
        "org/Beta",
        "org/Gamma",
    ]
    assert store.search_merged(sort="popularity")[0][0].model_id == "org/Gamma"
