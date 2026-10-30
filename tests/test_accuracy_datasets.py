from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.schemas import AccuracyRunCreate
from uniform_dgxspark.services.dataset_integration import DatasetIntegrationManager
from uniform_dgxspark.services.evalscope_accuracy import (
    EvalScopeAccuracyAdapter,
    EvalScopeAccuracyError,
)

ROOT = Path(__file__).resolve().parents[1]


def test_catalog_is_complete_pinned_and_does_not_expand_execution(client: TestClient) -> None:
    response = client.get("/static/accuracy-datasets.json")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["schema_version"] == 1
    assert catalog["source"]["commit"] == EvalScopeAccuracyAdapter.commit
    assert catalog["source"]["version"] == EvalScopeAccuracyAdapter.version
    assert len(catalog["source"]["metadata_sha256"]) == 64
    items = catalog["items"]
    names = {item["name"] for item in items}
    assert len(names) == len(items) == 275
    supported = EvalScopeAccuracyAdapter.dataset_catalog()
    assert {item["name"]: item["platform"] for item in items if item["platform"]} == supported
    assert {"mmlu", "aime24", "aime25", "aime26", "live_code_bench", "docvqa"} <= names
    for item in items:
        assert item["category"] in {"llm", "vlm", "agent", "aigc"}
        assert isinstance(item["summary"], str)
        assert isinstance(item["description"], str)
        assert all(isinstance(subset, str) for subset in item["subsets"])
        if not item["platform"]:
            adapter = EvalScopeAccuracyAdapter(Path("/tmp/not-executed"), Path("/tmp/not-read"))
            request = AccuracyRunCreate(
                target_type="deployment",
                target_id="test",
                datasets=[{"name": item["name"], "subset_list": ["default"], "limit": 10}],
            )
            with pytest.raises(EvalScopeAccuracyError, match="not integrated"):
                adapter.resolve_config(request, {})


def test_catalog_export_reproduces_snapshot_without_loading_evalscope() -> None:
    source = Path("/home/dgx-spark/Downloads/evalscope")
    if not (source / "evalscope/benchmarks/_index.json").is_file():
        pytest.skip("Pinned local EvalScope source is not available")
    spec = importlib.util.spec_from_file_location(
        "export_accuracy_datasets", ROOT / "scripts/export-accuracy-datasets.py"
    )
    assert spec and spec.loader
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    expected = json.loads((ROOT / "src/uniform_dgxspark/web/accuracy-datasets.json").read_text())
    assert exporter.build_catalog(source) == expected


def test_amc_can_propose_single_subset_two_sample_zero_shot_probe() -> None:
    source = Path("/home/dgx-spark/Downloads/evalscope")
    if not (source / "evalscope/benchmarks/amc/amc_adapter.py").is_file():
        pytest.skip("Pinned local EvalScope source is not available")
    manager = DatasetIntegrationManager(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(  # type: ignore[arg-type]
            adapter=SimpleNamespace(
                evalscope_source=source,
                commit=EvalScopeAccuracyAdapter.commit,
            ),
            target_resolver=SimpleNamespace(
                resolve=lambda *_: SimpleNamespace(snapshot={"target": "test"}, model_name="test")
            ),
        ),
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    spec, fingerprint = manager.candidate("amc")
    request, identity = manager._plan_identity(
        {
            "dataset_name": "amc",
            "accuracy_target": {"target_type": "deployment", "target_id": "test"},
        },
        "amc22",
    )

    assert spec.dataset_id == "evalscope/amc_22-24"
    assert spec.subsets == frozenset({"amc22", "amc23", "amc24"})
    assert spec.zero_shot_only
    assert len(fingerprint) == 64
    assert request.datasets[0].name == "amc"
    assert request.datasets[0].subset_list == ["amc22"]
    assert request.datasets[0].limit == 2
    assert request.datasets[0].few_shot_num == 0
    assert len(identity) == 64


def test_catalog_ui_exposes_search_filters_pagination_and_explicit_status(
    client: TestClient,
) -> None:
    html = client.get("/").text
    for element in (
        "accuracy-dataset-search",
        "accuracy-dataset-status",
        "accuracy-dataset-category",
        "accuracy-dataset-tag",
        "accuracy-dataset-list",
        "accuracy-dataset-dialog",
        "accuracy-dataset-prev",
        "accuracy-dataset-next",
    ):
        assert f'id="{element}"' in html
    assert "尚未接入可启动 AI 接入检查，验证成功后才允许评测" in html
    assert 'id="dataset-integration-list"' in html
    script = client.get("/static/app.js").text
    assert "/static/accuracy-datasets.json?v=20261027-accuracy-datasets" in script
    assert "accuracyDatasetBlockReason" in script
    assert "escapeHtml(item.description" in script
