from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app


def create_model(root: Path, name: str, *, missing_shard: bool = False) -> Path:
    model = root / name
    return create_model_at(model, missing_shard=missing_shard)


def create_model_at(model: Path, *, missing_shard: bool = False) -> Path:
    model.mkdir(parents=True)
    (model / "config.json").write_text(
        json.dumps(
            {
                "model_type": "test_model",
                "architectures": ["TestForCausalLM"],
                "quantization_config": {"quant_method": "nvfp4"},
            }
        ),
        encoding="utf-8",
    )
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model / "model-00001-of-00001.safetensors").write_bytes(b"weights")
    if missing_shard:
        (model / "model.safetensors.index.json").write_text(
            json.dumps(
                {
                    "weight_map": {
                        "present": "model-00001-of-00001.safetensors",
                        "missing": "model-00002-of-00002.safetensors",
                    }
                }
            ),
            encoding="utf-8",
        )
    return model


def test_local_model_scan_finds_huggingface_snapshot_layout(tmp_path: Path) -> None:
    models = tmp_path / "models"
    create_model(models, "direct-model")
    snapshot = (
        models
        / "models--deepseek-ai--DeepSeek-Test"
        / "snapshots"
        / "master"
    )
    create_model_at(snapshot)
    inference = snapshot / "inference"
    inference.mkdir()
    (inference / "config.json").write_text("{}", encoding="utf-8")
    unrelated = models / "runtime" / "lib" / "python" / "site-packages"
    unrelated.mkdir(parents=True)
    (unrelated / "library.bin").write_bytes(b"not a model")

    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    with TestClient(app) as client:
        added = client.post(
            "/api/v1/local-models/roots", json={"path": str(models)}
        )
        assert added.status_code == 201
        inventory = client.get("/api/v1/local-models").json()

    assert inventory["total"] == 2
    by_name = {item["name"]: item for item in inventory["items"]}
    assert set(by_name) == {"direct-model", "deepseek-ai--DeepSeek-Test"}
    assert by_name["deepseek-ai--DeepSeek-Test"]["path"] == str(snapshot.resolve())
    assert by_name["deepseek-ai--DeepSeek-Test"]["status"] == "ready"


def test_local_model_roots_are_configurable_and_read_only(tmp_path: Path) -> None:
    models = tmp_path / "models"
    ready = create_model(models, "ready-nvfp4")
    create_model(models, "incomplete", missing_shard=True)
    marker = ready / "model-00001-of-00001.safetensors"

    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    with TestClient(app) as client:
        relative = client.post(
            "/api/v1/local-models/roots", json={"path": "relative/models"}
        )
        assert relative.status_code == 422

        added = client.post(
            "/api/v1/local-models/roots", json={"path": str(models)}
        )
        assert added.status_code == 201
        assert added.json()["path"] == str(models.resolve())

        inventory = client.get("/api/v1/local-models")
        assert inventory.status_code == 200
        payload = inventory.json()
        assert payload["total"] == 2
        by_name = {item["name"]: item for item in payload["items"]}
        assert by_name["ready-nvfp4"]["status"] == "ready"
        assert by_name["ready-nvfp4"]["architecture"] == "TestForCausalLM"
        assert by_name["ready-nvfp4"]["quantization"] == "nvfp4"
        assert by_name["ready-nvfp4"]["weight_files"] == 1
        assert by_name["ready-nvfp4"]["usable_weight_files"] == 1
        assert by_name["ready-nvfp4"]["expected_weight_bytes"] == len(b"weights")
        assert by_name["ready-nvfp4"]["downloaded_weight_bytes"] == len(b"weights")
        assert by_name["incomplete"]["status"] == "incomplete"
        assert by_name["incomplete"]["missing_weight_files"] == 1

        removed = client.delete(
            "/api/v1/local-models/roots", params={"path": str(models)}
        )
        assert removed.json() == {"removed": True}
        assert marker.read_bytes() == b"weights"
        assert client.get("/api/v1/local-models").json()["total"] == 0


def test_interrupted_git_lfs_weight_reports_declared_size_and_partial_download(
    tmp_path: Path,
) -> None:
    models = tmp_path / "models"
    model = models / "qwen-dflash2"
    model.mkdir(parents=True)
    config = model / "config.json"
    tokenizer = model / "tokenizer.json"
    weight = model / "model.safetensors"
    config.write_text('{"model_type":"qwen"}', encoding="utf-8")
    tokenizer.write_text("{}", encoding="utf-8")

    oid = "67fc76d68dc5a9415511a4f394ef744d67510cd20e93b37cc2cc7d28e4bab65c"
    expected_bytes = 3_848_817_896
    partial_bytes = 2_037_831_230
    weight.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{oid}\n"
        f"size {expected_bytes}\n",
        encoding="ascii",
    )
    subprocess.run(["git", "init", "-q"], cwd=model, check=True)
    subprocess.run(
        ["git", "add", "config.json", "tokenizer.json", "model.safetensors"],
        cwd=model,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=model,
        check=True,
    )

    weight.write_bytes(b"")
    incomplete = model / ".git" / "lfs" / "incomplete" / f"{oid}1234567890"
    incomplete.parent.mkdir(parents=True)
    with incomplete.open("wb") as handle:
        handle.truncate(partial_bytes)

    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    with TestClient(app) as client:
        response = client.post("/api/v1/local-models/roots", json={"path": str(models)})
        assert response.status_code == 201
        inventory = client.get("/api/v1/local-models").json()

    item = inventory["items"][0]
    support_bytes = config.stat().st_size + tokenizer.stat().st_size
    assert item["status"] == "incomplete"
    assert item["status_reason"] == "Git LFS 下载未完成"
    assert item["weight_files"] == 1
    assert item["usable_weight_files"] == 0
    assert item["incomplete_weight_files"] == 1
    assert item["weight_bytes"] == 0
    assert item["expected_weight_bytes"] == expected_bytes
    assert item["downloaded_weight_bytes"] == partial_bytes
    assert item["total_bytes"] == expected_bytes + support_bytes
    assert inventory["total_bytes"] == item["total_bytes"]


def test_local_model_can_start_scoped_ai_deployment(tmp_path: Path) -> None:
    models = tmp_path / "models"
    model = create_model(models, "local-model")
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
        )
    )
    with TestClient(app) as client:
        client.post("/api/v1/local-models/roots", json={"path": str(models)})
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "local-model",
                "target_local_path": str(model),
            },
        )
        assert created.status_code == 201
        session_id = created.json()["id"]
        manager = client.app.state.ai_terminal
        capability = manager._capabilities[session_id]

        deployed = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/deploy",
            json={"port": 19002},
            headers={"X-AI-Capability": capability},
        )
        assert deployed.status_code == 200
        assert deployed.json()["model_id"] == "local-model"
        assert deployed.json()["parameters"]["local_model_path"] == str(model.resolve())

        client.post(f"/api/v1/ai/sessions/{session_id}/stop")
        for _ in range(100):
            if not manager.validate_capability(session_id, capability):
                break
            time.sleep(0.01)
