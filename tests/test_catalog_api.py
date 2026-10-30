from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from uniform_dgxspark.catalog import CatalogError
from uniform_dgxspark.schemas import CatalogModelRead


def model(source: str, model_id: str) -> CatalogModelRead:
    return CatalogModelRead(
        source=source,
        model_id=model_id,
        revision=source,
        author=model_id.split("/", 1)[0],
        pipeline_tag="text-generation",
        library_name="transformers",
        tags=["safetensors"],
        downloads=100,
        likes=10,
        last_modified=None,
        gated=False,
        private=False,
        compatibility="likely",
        compatibility_reason="Supported text generation model",
        metadata={},
        refreshed_at=datetime.now(UTC),
        popularity_rank=1,
    )


def test_joint_search_uses_huggingface_live_and_modelscope_cache(
    client: TestClient,
) -> None:
    service = client.app.state.catalog_service  # type: ignore[union-attr]
    store = client.app.state.catalog_store  # type: ignore[union-attr]
    store.upsert_many([model("modelscope", "Qwen/Qwen3-8B")])
    service.search_huggingface = lambda query, limit: [
        model("huggingface", "Qwen/Qwen3-8B")
    ]

    def unexpected_modelscope(query: str, limit: int) -> list[CatalogModelRead]:
        del query, limit
        raise AssertionError("ModelScope must not delay a successful Hugging Face search")

    service.search_modelscope = unexpected_modelscope

    response = client.get(
        "/api/v1/catalog/search",
        params={"query": "Qwen3", "source": "all", "compatibility": "likely"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source"] == "huggingface"
    assert [entry["source"] for entry in payload["items"][0]["sources"]] == [
        "huggingface",
        "modelscope",
    ]


def test_joint_search_falls_back_to_live_modelscope(client: TestClient) -> None:
    service = client.app.state.catalog_service  # type: ignore[union-attr]
    calls: list[str] = []

    def failed_huggingface(query: str, limit: int) -> list[CatalogModelRead]:
        del query, limit
        calls.append("huggingface")
        raise CatalogError("Hugging Face unavailable")

    def working_modelscope(query: str, limit: int) -> list[CatalogModelRead]:
        del query, limit
        calls.append("modelscope")
        return [model("modelscope", "Qwen/Qwen3-8B")]

    service.search_huggingface = failed_huggingface
    service.search_modelscope = working_modelscope

    response = client.get(
        "/api/v1/catalog/search",
        params={"query": "Qwen3", "source": "all", "compatibility": "likely"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert calls == ["huggingface", "modelscope"]
    assert payload["live"] is True
    assert payload["source_errors"] == {"huggingface": "Hugging Face unavailable"}
    assert payload["items"][0]["source"] == "modelscope"


def test_remote_search_applies_requested_sort(client: TestClient) -> None:
    service = client.app.state.catalog_service  # type: ignore[union-attr]
    low = model("huggingface", "org/Low").model_copy(update={"downloads": 5})
    high = model("huggingface", "org/High").model_copy(update={"downloads": 500})
    service.search_huggingface = lambda query, limit: [low, high]

    response = client.get(
        "/api/v1/catalog/search",
        params={"query": "org", "source": "huggingface", "sort": "downloads"},
    )

    assert response.status_code == 200
    assert [item["model_id"] for item in response.json()["items"]] == [
        "org/High",
        "org/Low",
    ]
    assert client.get(
        "/api/v1/catalog/search",
        params={"query": "org", "sort": "invalid"},
    ).status_code == 422


def test_model_metadata_endpoint_returns_verified_file_sizes(
    client: TestClient,
) -> None:
    service = client.app.state.catalog_service  # type: ignore[union-attr]
    item = model("huggingface", "org/model").model_copy(
        update={
            "metadata": {
                "weight_size_bytes": 1234,
                "repository_size_bytes": 1300,
                "weight_metadata_verified": True,
            }
        }
    )
    service.enrich_model_metadata = lambda source, model_id: item

    response = client.get(
        "/api/v1/catalog/model-metadata",
        params={"source": "huggingface", "model_id": "org/model"},
    )

    assert response.status_code == 200
    assert response.json()["metadata"]["weight_size_bytes"] == 1234
