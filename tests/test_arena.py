from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentCreate, DeploymentParameters
from uniform_dgxspark.services.arena import ArenaManager
from uniform_dgxspark.services.docker_runtime import DockerCommandError
from uniform_dgxspark.state import DeploymentStatus


class ModelHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": "served-arena-model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def mark_running(client: TestClient, deployment_id: str) -> None:
    for deployment_status in (
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.RUNNING,
    ):
        client.app.state.store.transition(deployment_id, deployment_status, message="test")


def make_fake_uvx(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
def value(name):
    return args[args.index(name) + 1]

progress = value("--emit-progress")
with open(progress, "w", encoding="utf-8") as stream:
    stream.write(json.dumps({"type":"request_start","run_index":0}) + "\\n")
    stream.write(json.dumps({"type":"request_end","error":""}) + "\\n")
    stream.write(json.dumps({"type":"request_start","run_index":0}) + "\\n")
    stream.write(json.dumps({"type":"request_end","error":""}) + "\\n")
    stream.write(json.dumps({"type":"bench_complete","status":"ok"}) + "\\n")

metric = lambda mean, std: {"mean":mean,"std":std,"values":[mean]}
result = {
    "model": value("--model"),
    "benchmarks": [{
        "concurrency": int(value("--concurrency")),
        "context_size": int(value("--depth")),
        "prompt_size": int(value("--pp")),
        "response_size": int(value("--tg")),
        "is_context_prefill_phase": False,
        "pp_throughput": metric(500.0, 5.0),
        "tg_throughput": metric(40.0, 1.0),
        "peak_throughput": metric(45.0, 2.0),
        "ttfr": metric(4000.0, 100.0),
        "est_ppt": metric(3990.0, 100.0),
        "e2e_ttft": metric(4000.0, 100.0)
    }]
}
with open(value("--save-result"), "w", encoding="utf-8") as stream:
    json.dump(result, stream)
print("fake llama-benchy complete")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_arena_uses_configured_uv_runtime(tmp_path: Path) -> None:
    uvx = tmp_path / "bin" / "uvx"
    cache = tmp_path / "uv-cache"
    tools = tmp_path / "uv-tools"
    manager = ArenaManager(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        uvx_binary=str(uvx),
        uv_cache_dir=cache,
        uv_tool_dir=tools,
    )

    assert manager.uvx_binary == str(uvx)
    assert manager.uv_cache_dir == cache
    assert manager.uv_tool_dir == tools


def test_arena_run_executes_llama_benchy_and_compares_baseline(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app(Settings(data_dir=tmp_path / "data", worker_enabled=False))
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    fake_uvx = tmp_path / "uvx"
    make_fake_uvx(fake_uvx)
    try:
        with TestClient(app) as client:
            deployment = client.app.state.store.create(
                DeploymentCreate(
                    model_id="org/arena-model",
                    display_name="Arena model",
                    parameters=DeploymentParameters(port=server.server_address[1]),
                )
            )
            mark_running(client, deployment.id)
            manager = client.app.state.arena_manager
            manager.uvx_binary = str(fake_uvx)

            def fake_fetch(url: str) -> dict[str, Any]:
                assert url.endswith("baseline-id")
                return {
                    "modelName": "Leaderboard model",
                    "modelFullPath": "org/leaderboard-model",
                    "runtime": "vLLM",
                    "clusterSize": 2,
                    "recipe": {
                        "fullRecipe": {
                            "command": "vllm serve model --speculative-config '{config}'",
                            "defaults": {
                                "speculative_config": (
                                    '{"method":"draft","model":"org/draft",'
                                    '"num_speculative_tokens":4}'
                                )
                            },
                        }
                    },
                    "tests": [
                        {
                            "testName": "pp8 @ d16 (c1)",
                            "tokensPerSec": 400,
                            "tokensPerSecStdDev": 4,
                            "ttfr": 5000,
                            "ttfrStdDev": 100,
                        },
                        {
                            "testName": "tg2 @ d16 (c1)",
                            "tokensPerSec": 32,
                            "tokensPerSecStdDev": 2,
                        },
                    ],
                }

            manager._fetch_json = fake_fetch
            created = client.post(
                "/api/v1/arena/runs",
                json={
                    "target_type": "deployment",
                    "target_id": deployment.id,
                    "tokenizer": str(tokenizer),
                    "pp": 8,
                    "tg": 2,
                    "depth": 16,
                    "runs": 1,
                    "concurrency": 1,
                    "leaderboard_benchmark_id": "baseline-id",
                },
            )
            assert created.status_code == 201
            run_id = created.json()["id"]
            result: dict[str, Any] = {}
            for _ in range(200):
                result = client.get(f"/api/v1/arena/runs/{run_id}").json()
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)

            assert result["status"] == "completed"
            assert result["served_model_name"] == "served-arena-model"
            assert result["progress"]["completed_requests"] == 2
            assert result["result_summary"]["prefill_throughput"]["mean"] == 500
            assert result["result_summary"]["e2e_ttft_ms"]["p50"] == 4000
            assert result["result_summary"]["tpot_ms"]["p50"] == 25
            assert result["resource_summary"]["sample_count"] >= 1
            assert result["speculative_metrics"]["supported"] is False
            reference = result["leaderboard_reference"]
            assert reference["prefill_gap_percent"] == 25
            assert reference["decode_gap_percent"] == 25
            assert reference["ttfr_gap_percent"] == 20
            assert reference["local_runtime"]["speculative"]["enabled"] is False
            assert reference["local_runtime"]["speculative"]["evidence"] == (
                "平台托管部署参数"
            )
            assert reference["recipe"]["speculative"]["enabled"] is True
            assert any("开关不同" in note for note in reference["comparison_notes"])
            assert created.json()["command_preview"][0] == str(fake_uvx)
            assert "EMPTY" not in created.json()["command_preview"]
            assert client.get(f"/api/v1/arena/runs/{run_id}/result").status_code == 200
            assert "fake llama-benchy complete" in client.get(
                f"/api/v1/arena/runs/{run_id}/logs"
            ).json()["logs"]
            report = client.get(f"/api/v1/arena/runs/{run_id}/report")
            assert report.status_code == 200
            assert report.json()["benchmark"]["id"] == run_id
            run_directory = manager.store.run_directory(run_id)
            with (run_directory / "llama-benchy.log").open("a", encoding="utf-8") as log:
                log.write("\nclient request complete API_KEY=client-secret\n")
            runtime = client.app.state.runtime
            runtime.logs_for_container = lambda container_name, tail=200: (
                "server handled POST /v1/chat/completions "
                "Authorization: Bearer server-secret\n"
            )
            context = client.get(
                f"/api/v1/arena/runs/{run_id}/analysis-context"
            )
            assert context.status_code == 200
            analysis = context.json()
            assert "client request complete" in analysis["client_log"]
            assert "server handled POST" in analysis["server_log"]
            assert "client-secret" not in analysis["client_log"]
            assert "server-secret" not in analysis["server_log"]
            assert "<redacted>" in analysis["client_log"]
            assert "<redacted>" in analysis["server_log"]

            def unavailable_logs(container_name: str, *, tail: int = 200) -> str:
                raise DockerCommandError("container is no longer available")

            runtime.logs_for_container = unavailable_logs
            unavailable = client.get(
                f"/api/v1/arena/runs/{run_id}/analysis-context"
            )
            assert unavailable.status_code == 200
            assert unavailable.json()["server_log"] == ""
            assert "服务端日志不可用" in unavailable.json()["log_notes"][0]
            assert client.delete(f"/api/v1/arena/runs/{run_id}").status_code == 204
            assert client.get(f"/api/v1/arena/runs/{run_id}").status_code == 404
            assert not run_directory.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_arena_leaderboard_search_and_page(client: TestClient) -> None:
    manager = client.app.state.arena_manager
    manager._fetch_json = lambda url: (
        {
            "id": "one",
            "modelName": "GLM Flash",
            "modelFullPath": "org/glm",
            "runtime": "vLLM",
            "clusterSize": 2,
            "recipe": {
                "fullRecipe": {
                    "container": "ghcr.io/spark-arena/glm:test",
                    "command": (
                        "vllm serve {model} "
                        "--speculative-config '{local_speculative_config}'"
                    ),
                    "model_revision": "abc123",
                    "defaults": {
                        "tensor_parallel": 2,
                        "quantization": "exl3",
                        "speculative_config": json.dumps(
                            {
                                "method": "dflash",
                                "model": "org/draft-model",
                                "num_speculative_tokens": 7,
                                "draft_tensor_parallel_size": 1,
                            }
                        ),
                    },
                    "env": {"VLLM_NO_USAGE_STATS": "1"},
                    "pre_exec": ["python /opt/patch.py"],
                }
            },
            "tests": [
                {"testName": "pp2048 @ d16384 (c1)", "tokensPerSec": 450},
                {"testName": "tg128 @ d16384 (c1)", "tokensPerSec": 32},
                {"testName": "ctx_pp @ d16384 (c1)", "tokensPerSec": 1000},
            ],
        }
        if url.endswith("/one")
        else {
            "generatedAt": "2026-09-01T00:00:00Z",
            "benchmarks": [
                {
                    "id": "one",
                    "modelName": "GLM Flash",
                    "runtime": "vLLM",
                    "clusterSize": 2,
                },
                {
                    "id": "two",
                    "modelName": "Qwen",
                    "runtime": "SGLang",
                    "clusterSize": 1,
                },
            ],
        }
    )
    response = client.get("/api/v1/arena/leaderboard?query=glm")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == "one"
    detail = client.get("/api/v1/arena/leaderboard/one")
    assert detail.status_code == 200
    assert detail.json()["presets"] == [
        {
            "pp": 2048,
            "tg": 128,
            "depth": 16384,
            "concurrency": 1,
            "prefill_test_name": "pp2048 @ d16384 (c1)",
            "decode_test_name": "tg128 @ d16384 (c1)",
                "summary": {
                    "prefill_throughput": {
                        "mean": 450.0,
                        "std": 0.0,
                        "p50": None,
                        "p95": None,
                        "p99": None,
                    },
                    "decode_throughput": {
                        "mean": 32.0,
                        "std": 0.0,
                        "p50": None,
                        "p95": None,
                        "p99": None,
                    },
                    "peak_decode_throughput": None,
                    "ttfr_ms": None,
                    "estimated_prefill_ms": None,
                    "e2e_ttft_ms": None,
                    "tpot_ms": None,
                },
        }
    ]
    recipe = detail.json()["recipe"]
    assert recipe["container"] == "ghcr.io/spark-arena/glm:test"
    assert recipe["model_revision"] == "abc123"
    assert recipe["speculative"] == {
        "enabled": True,
        "method": "dflash",
        "draft_model": "org/draft-model",
        "num_speculative_tokens": 7,
        "draft_tensor_parallel_size": 1,
        "draft_sample_method": None,
        "rejection_sample_method": None,
        "evidence": "Spark Arena 公开 recipe",
        "raw": {
            "method": "dflash",
            "model": "org/draft-model",
            "num_speculative_tokens": 7,
            "draft_tensor_parallel_size": 1,
        },
    }
    assert "Spark Arena 对标评测" in client.get("/").text
    app_script = client.get("/static/app.js").text
    assert "投机解码" in app_script
    assert "analysis-context" in app_script
    assert "客户端评测日志（llama-benchy.log）" in app_script
    assert "服务端推理日志（容器日志）" in app_script
    assert "应用这组测试参数" in app_script
    assert "本地部署未记录" in app_script
    assert "结果只能并列展示" in app_script


def test_arena_recipe_without_speculative_decoding(client: TestClient) -> None:
    manager = client.app.state.arena_manager
    manager._fetch_json = lambda url: {
        "id": "plain",
        "modelName": "Plain model",
        "runtime": "vLLM",
        "clusterSize": 1,
        "recipe": {
            "fullRecipe": {
                "container": "example/vllm:latest",
                "command": "vllm serve {model} --tensor-parallel-size 1",
                "defaults": {"tensor_parallel": 1},
            }
        },
        "tests": [],
    }
    detail = client.get("/api/v1/arena/leaderboard/plain")
    assert detail.status_code == 200
    assert detail.json()["recipe"]["speculative"]["enabled"] is False


def test_arena_detail_without_recipe(client: TestClient) -> None:
    manager = client.app.state.arena_manager
    manager._fetch_json = lambda url: {
        "id": "legacy",
        "modelName": "Legacy model",
        "runtime": "vLLM",
        "clusterSize": 1,
        "tests": [],
    }
    detail = client.get("/api/v1/arena/leaderboard/legacy")
    assert detail.status_code == 200
    assert detail.json()["recipe"] is None


def test_local_speculative_detection_is_evidence_based() -> None:
    disabled = ArenaManager._speculative_config(
        command="./start.sh",
        environment={"SPEC_METHOD": "none", "DFLASH_TOKENS": "7"},
        explicit=None,
        complete=True,
        evidence="container",
    )
    assert disabled.enabled is False
    assert "SPEC_METHOD=none" in str(disabled.evidence)

    unknown = ArenaManager._speculative_config(
        command="./start.sh",
        environment={},
        explicit=None,
        complete=False,
        evidence="deployment result",
    )
    assert unknown.enabled is None

    delta = ArenaManager._speculative_delta(
        {
            "vllm:spec_decode_num_drafts_total": 10,
            "vllm:spec_decode_num_draft_tokens_total": 70,
            "vllm:spec_decode_num_accepted_tokens_total": 21,
        },
        {
            "vllm:spec_decode_num_drafts_total": 20,
            "vllm:spec_decode_num_draft_tokens_total": 140,
            "vllm:spec_decode_num_accepted_tokens_total": 56,
        },
    )
    assert delta.supported is True
    assert delta.accepted_tokens == 35
    assert delta.draft_tokens == 70
    assert delta.acceptance_rate_percent == 50
