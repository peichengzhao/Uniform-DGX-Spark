from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.ai_store import AISessionStore
from uniform_dgxspark.config import Settings
from uniform_dgxspark.database import Database
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import AISessionCreate, DeploymentCreate, DeploymentParameters
from uniform_dgxspark.services import ai_terminal as ai_terminal_module
from uniform_dgxspark.services.ai_terminal import AITerminalManager
from uniform_dgxspark.services.references import ReferenceError, SafeReferenceFetcher
from uniform_dgxspark.state import DeploymentStatus


def test_existing_ai_session_gets_compatible_name_and_target(tmp_path: Path) -> None:
    database_path = tmp_path / "uniform.db"
    workspace = tmp_path / "old-session"
    workspace.mkdir()
    (workspace / "deployment-context.json").write_text(
        json.dumps({"deployment": {"target_model": {"model_id": "org/Legacy-Model"}}}),
        encoding="utf-8",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE ai_sessions (
                id TEXT PRIMARY KEY, deployment_id TEXT, provider TEXT NOT NULL,
                execution_mode TEXT NOT NULL, status TEXT NOT NULL,
                initial_prompt TEXT NOT NULL, reference_urls_json TEXT NOT NULL,
                workspace_path TEXT NOT NULL, pid INTEGER, exit_code INTEGER,
                error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ai_sessions VALUES (
                'legacy', NULL, 'codex', 'autonomous', 'exited', '', '[]', ?,
                NULL, 0, NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
            )
            """,
            (str(workspace),),
        )

    database = Database(database_path)
    database.initialize()
    session = AISessionStore(database).get("legacy")

    assert session.display_name == "Legacy-Model"
    assert session.target_model_id == "org/Legacy-Model"
    assert session.ai_model is None
    assert session.reasoning_effort is None
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_sessions)")}
    assert "client_request_id" in columns


def test_ai_session_reads_related_service_from_saved_context(tmp_path: Path) -> None:
    database = Database(tmp_path / "uniform.db")
    database.initialize()
    store = AISessionStore(database)
    source_workspace = tmp_path / "source"
    source_workspace.mkdir()
    source = store.create(
        AISessionCreate(
            provider="codex",
            task_type="deployment",
            display_name="Qwen service",
        ),
        str(source_workspace),
        session_id="source-session",
    )
    stop_workspace = tmp_path / "stop"
    stop_workspace.mkdir()
    (stop_workspace / "deployment-context.json").write_text(
        json.dumps(
            {
                "deployment": {
                    "service_handoff": {
                        "source_session": {
                            "id": source.id,
                            "display_name": source.display_name,
                            "target_model_id": "org/Qwen-Test",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    stop = store.create(
        AISessionCreate(
            provider="codex",
            task_type="service_stop",
            display_name="Qwen service · 停服恢复",
        ),
        str(stop_workspace),
        session_id="stop-session",
    )

    related = store.get(stop.id)

    assert related.related_session_id == source.id
    assert related.related_service_name == "Qwen service"
    assert related.target_model_id == "org/Qwen-Test"
    assert (
        AITerminalManager._target_model_id(
            AISessionCreate(provider="codex", task_type="service_stop"),
            {"service_handoff": {"source_session": {"target_model_id": "org/Qwen-Test"}}},
        )
        == "org/Qwen-Test"
    )


def test_ai_provider_status_and_session_transcript(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/bin/echo",
            claude_binary="/definitely/missing/claude",
        )
    )
    with TestClient(app) as client:
        providers = client.get("/api/v1/ai/providers")
        assert providers.status_code == 200
        assert providers.json()[0]["available"] is True
        assert providers.json()[1]["available"] is False

        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "display_name": "模型部署助手",
                "ai_model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "initial_prompt": "inspect deployment",
            },
        )
        assert created.status_code == 201
        assert created.json()["execution_mode"] == "autonomous"
        assert created.json()["display_name"] == "模型部署助手"
        assert created.json()["target_model_id"] is None
        assert created.json()["ai_model"] == "gpt-5.6-terra"
        assert created.json()["reasoning_effort"] == "high"
        session_id = created.json()["id"]
        transcript = b""
        for _ in range(50):
            response = client.get(f"/api/v1/ai/sessions/{session_id}/transcript")
            transcript = response.content
            if transcript:
                break
            time.sleep(0.01)
        assert b"inspect deployment" in transcript

        audit = client.get(f"/api/v1/ai/sessions/{session_id}/audit")
        assert audit.status_code == 200
        assert any(item["event_type"] == "status_changed" for item in audit.json()["items"])

        missing = client.post(
            "/api/v1/ai/sessions",
            json={"provider": "claude"},
        )
        assert missing.status_code == 409


def test_deepseek_command_places_configured_flags_before_the_prompt(
    tmp_path: Path,
) -> None:
    request = AISessionCreate(
        provider="deepseek",
        ai_model="deepseek-v4-flash",
        initial_prompt="inspect the workspace",
    )
    command = AITerminalManager._command(
        "/usr/bin/deepseek",
        request,
        tmp_path,
        deepseek_args=["--auto-approve", "--sandbox", "workspace"],
    )
    assert command[:5] == [
        "/usr/bin/deepseek",
        "--auto-approve",
        "--sandbox",
        "workspace",
        "--model",
    ]
    assert command[5] == "deepseek-v4-flash"
    assert "inspect the workspace" in command[-1]


def test_deepseek_harness_provider_passes_configured_flags_and_prompt(
    tmp_path: Path,
) -> None:
    fake_deepseek = tmp_path / "fake-deepseek"
    fake_deepseek.write_text('#!/bin/sh\nprintf "[%s]\\n" "$@"\n', encoding="utf-8")
    fake_deepseek.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary="/definitely/missing/codex",
            claude_binary="/definitely/missing/claude",
            deepseek_binary=str(fake_deepseek),
            deepseek_args="--auto-approve --sandbox workspace",
        )
    )
    with TestClient(app) as client:
        providers = {item["provider"]: item for item in client.get("/api/v1/ai/providers").json()}
        assert providers["deepseek"]["available"] is True
        assert providers["codex"]["available"] is False

        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "deepseek",
                "ai_model": "deepseek-v4-flash",
                "initial_prompt": "inspect the workspace",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["provider"] == "deepseek"
        session_id = created.json()["id"]
        transcript = b""
        for _ in range(100):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            if b"inspect the workspace" in transcript:
                break
            time.sleep(0.01)
        assert b"[--auto-approve]" in transcript
        assert b"[--model]" in transcript
        assert b"[deepseek-v4-flash]" in transcript
        assert b"inspect the workspace" in transcript


def test_legacy_ai_sessions_table_accepts_deepseek_after_migration(tmp_path: Path) -> None:
    path = tmp_path / "uniform.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE ai_sessions (
                id TEXT PRIMARY KEY,
                deployment_id TEXT,
                provider TEXT NOT NULL CHECK(provider IN ('codex', 'claude')),
                status TEXT NOT NULL,
                initial_prompt TEXT NOT NULL,
                reference_urls_json TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                pid INTEGER,
                exit_code INTEGER,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO ai_sessions(
                id, provider, status, initial_prompt, reference_urls_json,
                workspace_path, created_at, updated_at
            ) VALUES (
                'legacy-session', 'codex', 'exited', 'legacy prompt', '[]',
                '/tmp/legacy', '2026-10-20T00:00:00Z', '2026-10-20T00:00:00Z'
            );
            """
        )
        connection.commit()

    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) AS count FROM ai_sessions WHERE id = 'legacy-session'"
            ).fetchone()["count"]
            == 1
        )
        connection.executemany(
            "INSERT INTO ai_sessions("
            "id, provider, status, initial_prompt, reference_urls_json, workspace_path,"
            " created_at, updated_at"
            ") VALUES (?, ?, 'exited', 'prompt', '[]', ?,"
            " '2026-10-30T00:00:00Z', '2026-10-30T00:00:00Z')",
            [
                ("deepseek-session", "deepseek", "/tmp/deepseek"),
                ("dsh-session", "dsh", "/tmp/dsh"),
            ],
        )
        connection.commit()


def test_benchmark_analysis_session_does_not_require_deployment_result(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/bin/echo",
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "task_type": "benchmark_analysis",
                "display_name": "Performance / analysis",
                "initial_prompt": "Analyze these aggregate benchmark metrics only.",
            },
        )
        assert created.status_code == 201
        assert created.json()["task_type"] == "benchmark_analysis"
        workspace = Path(created.json()["workspace_path"])
        assert "Do not submit" in (workspace / "RESULT_SCHEMA.md").read_text()
        policy = (workspace / "SECURITY.md").read_text()
        assert "analysis-only" in policy
        assert "Do not deploy" in policy
        assert (
            "Analyze these aggregate benchmark metrics only."
            in (workspace / "ARENA_ANALYSIS_INPUT.md").read_text()
        )
        requirements = (workspace / "REPORT_REQUIREMENTS.md").read_text()
        assert "exactly one report" in requirements
        assert "MODEL_PERFORMANCE_REPORT.md" in requirements
        report_status = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/performance-report")
        assert report_status.status_code == 200
        assert report_status.json()["available"] is False
        report_date = (
            datetime.fromisoformat(created.json()["created_at"].replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d_%H%M")
        )
        expected_filename = f"Performance_analysis_{report_date}_综合报告.md"
        assert report_status.json()["filename"] == expected_filename
        report_path = workspace / "MODEL_PERFORMANCE_REPORT.md"
        assert report_status.json()["path"] == str(report_path)
        report_path.write_text("# Combined performance report\n", encoding="utf-8")
        report_status = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/performance-report")
        assert report_status.json()["available"] is True
        assert report_status.json()["filename"] == expected_filename
        downloaded = client.get(
            f"/api/v1/ai/sessions/{created.json()['id']}/performance-report?download=true"
        )
        assert downloaded.status_code == 200
        assert "Combined performance report" in downloaded.text
        assert downloaded.headers["content-disposition"] == (
            f"attachment; filename*=utf-8''{quote(expected_filename)}"
        )


def test_ai_benchmark_receives_live_deployment_context_and_internal_code(
    tmp_path: Path,
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    reference_dir = tmp_path / "reference"
    history_csv = tmp_path / "模型性能测试 - Sheet2.csv"
    history_csv.write_text(
        "\ufeff机器,Model,KV Cache Dtype,Engine,Max Model Length (tokens),"
        "GPU Memory Utilization Target (%),Input,Output,Max-Concurrency,Num,"
        "Successful Req,Duration(s),Total input tokens,Total generated tokens,Req/s,"
        "prefill,Decode Tok/s ,Output Tok/s,Total Tok/s,Mean TTFT(ms),Median TTFT(ms),"
        "P99 TTFT(ms),Mean TPOT (ms),Median TPOT(ms),P95 TPOT(ms),P99 TPOT(ms),,,,\n"
        "legacy-machine,legacy-model,fp8,vllm,8192,90,1,1,1,1,1,1,1,1,1,1,1,1,"
        "1,1,1,1,1,1,1,1,,,,\n",
        encoding="utf-8",
    )
    (reference_dir / "test_vllm").mkdir(parents=True)
    (reference_dir / "test_vllm" / "run_bench.sh").write_text(
        "#!/bin/sh\necho benchmark\n", encoding="utf-8"
    )
    (reference_dir / "collect_unified_memory.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    (reference_dir / "historical.log").write_text("do not copy", encoding="utf-8")
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
            benchmark_reference_dir=reference_dir,
            benchmark_history_csv=history_csv,
        )
    )
    with TestClient(app) as client:
        deployment = client.app.state.store.create(
            DeploymentCreate(
                model_id="org/model",
                display_name="Live model",
                parameters=DeploymentParameters(
                    port=18081,
                    served_model_name="served-model",
                    max_model_len=8192,
                ),
            )
        )
        for status in (
            DeploymentStatus.PREFLIGHT,
            DeploymentStatus.DOWNLOADING_MODEL,
            DeploymentStatus.PULLING_IMAGE,
            DeploymentStatus.STARTING_CONTAINER,
            DeploymentStatus.LOADING_MODEL,
            DeploymentStatus.HEALTH_CHECKING,
            DeploymentStatus.RUNNING,
        ):
            client.app.state.store.transition(deployment.id, status, message="test")
        client.app.state.runtime.container_metadata = lambda _: {
            "container_name": "uniform-test",
            "container_id": "container-id",
            "launch_command": "docker run --name uniform-test image",
        }
        client.app.state.runtime.logs = lambda _id, tail=1000: "service ready"
        client.app.state.metrics.inference_telemetry = lambda _port: {
            "available": True,
            "request_success_total": 12,
        }
        client.app.state.metrics.snapshot = lambda: {"gpu": {"name": "GB10"}}
        client.app.state.cluster_monitoring.snapshot = lambda: {
            "items": [{"id": "local", "online": True}],
            "total": 1,
        }

        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "task_type": "benchmark_execution",
                "display_name": "Live model benchmark",
                "initial_prompt": "Run a representative benchmark.",
                "benchmark_target_type": "deployment",
                "benchmark_target_id": deployment.id,
                "benchmark_plan": {
                    "io_shapes": [
                        {"input_tokens": 512, "output_tokens": 128},
                        {"input_tokens": 4096, "output_tokens": 512},
                    ],
                    "concurrencies": [1, 8],
                    "requests_per_case": 20,
                    "runs_per_case": 3,
                    "warmup_requests": 2,
                    "timeout_seconds": 600,
                    "cooldown_seconds": 5,
                    "endpoint": "chat_completions",
                    "dataset": "specified",
                    "dataset_path": "datasets/prompts.jsonl",
                    "require_exact_output": True,
                    "prefix_cache": "compare",
                    "metrics": ["ttft", "tpot", "prefill_throughput"],
                    "resource_metrics": ["unified_memory", "network_rdma"],
                    "ab_test_enabled": True,
                    "ab_test_dimensions": "tensor_parallel_size=1/2",
                },
            },
        )

        assert created.status_code == 201
        assert created.json()["task_type"] == "benchmark_execution"
        assert created.json()["target_model_id"] == "served-model"
        continuation = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/auto-resume").json()[
            "prompt"
        ]
        assert continuation.startswith("继续执行上次性能评测")
        assert "部署" not in continuation
        workspace = Path(created.json()["workspace_path"])
        context = json.loads((workspace / "benchmark-context.json").read_text())
        assert context["benchmark_target"]["service"]["port"] == 18081
        assert context["benchmark_target"]["service"]["container"]["container_name"] == (
            "uniform-test"
        )
        assert context["benchmark_target"]["service"]["runtime_profile"]["max_model_len"] == 8192
        assert (
            context["benchmark_target"]["service"]["runtime_profile"]["gpu_memory_utilization"]
            == 0.9
        )
        assert context["benchmark_target"]["host_metrics"]["gpu"]["name"] == "GB10"
        assert context["benchmark_plan"]["io_shapes"][1] == {
            "input_tokens": 4096,
            "output_tokens": 512,
        }
        assert context["benchmark_plan"]["concurrencies"] == [1, 8]
        assert context["benchmark_plan"]["dataset_path"] == "datasets/prompts.jsonl"
        assert context["benchmark_plan"]["require_exact_output"] is True
        assert context["benchmark_target"]["benchmark_plan"] == context["benchmark_plan"]
        assert (workspace / "benchmark-reference" / "test_vllm" / "run_bench.sh").is_file()
        assert (workspace / "benchmark-reference" / "collect_unified_memory.py").is_file()
        assert not (workspace / "benchmark-reference" / "historical.log").exists()
        benchmark_task = (workspace / "BENCHMARK_TASK.md").read_text()
        assert "historical absolute paths" in benchmark_task
        assert "takes priority over defaults" in benchmark_task
        assert "literal value `reference`" in benchmark_task
        assert "collect_unified_memory.py" in benchmark_task
        assert "mandatory default metric" in benchmark_task
        assert (workspace / "REPORT_REQUIREMENTS.md").is_file()
        assert (workspace / "BENCHMARK_RESULT_SCHEMA.json").is_file()
        result_schema = json.loads((workspace / "BENCHMARK_RESULT_SCHEMA.json").read_text())
        record_properties = result_schema["$defs"]["AIBenchmarkResultRecord"]["properties"]
        assert "prefill_duration_seconds" in record_properties
        assert "mean_prefill_ms" in record_properties
        assert "prefill_measurement_source" in record_properties
        assert "gpu_utilization_peak_by_device_percent" in record_properties

        (workspace / "BENCHMARK_RESULT.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "machine": "dgx-spark",
                            "model": "served-model",
                            "kv_cache_dtype": "fp8",
                            "engine": "vllm",
                            "input_tokens": 512,
                            "output_tokens": 128,
                            "max_concurrency": 8,
                            "requests": 20,
                            "successful_requests": 20,
                            "duration_seconds": 12.5,
                            "total_input_tokens": 10240,
                            "total_generated_tokens": 2560,
                            "request_throughput": 999,
                            "decode_token_throughput": 210.2,
                            "output_token_throughput": 999,
                            "total_token_throughput": 999,
                            "mean_ttft_ms": 42.1,
                            "median_ttft_ms": 40.0,
                            "p99_ttft_ms": 55.4,
                            "mean_tpot_ms": 9.8,
                            "median_tpot_ms": 9.5,
                            "p95_tpot_ms": 11.2,
                            "p99_tpot_ms": 12.4,
                            "mean_itl_ms": 9.7,
                            "prefill_duration_seconds": 3.2,
                            "prefill_observation_count": 20,
                            "prefill_computed_tokens": 10240,
                            "mean_prefill_ms": 160.0,
                            "median_prefill_ms": 150.0,
                            "p95_prefill_ms": 210.0,
                            "p99_prefill_ms": 240.0,
                            "prefill_token_throughput": 820.0,
                            "prefill_measurement_source": (
                                "vllm:request_prefill_time_seconds; "
                                "vllm:request_prefill_kv_computed_tokens"
                            ),
                            "unified_memory_peak_bytes": 123_456_789_012,
                            "gpu_utilization_peak_by_device_percent": [88.5, 77.25],
                            "rdma_rx_peak_bps": 1000,
                            "rdma_tx_peak_bps": 2000,
                            "speculative_acceptance_rate": 0.72,
                        }
                    ],
                    "notes": ["observed locally"],
                }
            ),
            encoding="utf-8",
        )
        read_only = client.get(
            f"/api/v1/ai/sessions/{created.json()['id']}/benchmark-result?publish=false"
        )
        assert read_only.status_code == 200
        assert read_only.json()["records"][0]["mean_ttft_ms"] == 42.1
        assert read_only.json()["records"][0]["request_throughput"] == 1.6
        assert read_only.json()["records"][0]["output_token_throughput"] == 204.8
        assert read_only.json()["records"][0]["total_token_throughput"] == 1024.0
        assert read_only.json()["records"][0]["prefill_token_throughput"] == 3200.0
        assert read_only.json()["history"]["appended"] is False
        assert not (workspace / ".benchmark-history-published.json").exists()
        with history_csv.open(encoding="utf-8-sig", newline="") as stream:
            assert len(list(csv.reader(stream))) == 2

        structured = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/benchmark-result")
        assert structured.status_code == 200
        assert structured.json()["records"][0]["mean_ttft_ms"] == 42.1
        assert structured.json()["records"][0]["mean_prefill_ms"] == 160.0
        assert structured.json()["history"]["appended"] is True
        repeated = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/benchmark-result")
        assert repeated.json()["history"]["appended"] is True
        with history_csv.open(encoding="utf-8-sig", newline="") as stream:
            history_rows = list(csv.reader(stream))
        assert len(history_rows) == 3
        assert len(history_rows[0]) == 52
        assert history_rows[0][15] == "Mean Prefill(ms)"
        assert history_rows[1][0:2] == ["legacy-machine", "legacy-model"]
        assert history_rows[1][26:] == [""] * 26
        assert history_rows[2][0:4] == ["dgx-spark", "served-model", "fp8", "vllm"]
        assert history_rows[2][4:6] == ["8192", "90.0"]
        assert history_rows[2][15] == "160.0"
        assert history_rows[2][19:22] == ["42.1", "40.0", "55.4"]
        assert history_rows[2][26] == "9.7"
        assert history_rows[2][30:38] == [
            "3.2",
            "20",
            "10240",
            "150.0",
            "210.0",
            "240.0",
            "3200.0",
            "vllm:request_prefill_time_seconds; vllm:request_prefill_kv_computed_tokens",
        ]
        assert history_rows[2][38:42] == [
            "114.98",
            "88.5",
            "77.25",
            "72.0",
        ]
        assert history_rows[2][48:51] == [
            "completed",
            "",
            created.json()["id"],
        ]

        report_status = client.get(f"/api/v1/ai/sessions/{created.json()['id']}/performance-report")
        assert report_status.status_code == 200


def test_ai_session_creation_is_idempotent(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/bin/echo",
        )
    )
    payload = {
        "provider": "codex",
        "client_request_id": "deploy-request-12345678",
        "display_name": "single deployment session",
        "initial_prompt": "deploy once",
    }
    with TestClient(app) as client:
        first = client.post("/api/v1/ai/sessions", json=payload)
        duplicate = client.post("/api/v1/ai/sessions", json=payload)

        assert first.status_code == 201
        assert duplicate.status_code == 201
        assert duplicate.json()["id"] == first.json()["id"]
        sessions = client.get("/api/v1/ai/sessions?include_archived=true").json()
        assert sessions["total"] == 1
        audit = client.get(f"/api/v1/ai/sessions/{first.json()['id']}/audit").json()["items"]
        assert sum(item["event_type"] == "created" for item in audit) == 1


def test_ai_sessions_filter_and_resume_existing_model_context(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "resume" ]; then\n'
        '  echo resumed:"$*"\n'
        "  sleep 5\n"
        "else\n"
        "  echo initial-session\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
        )
    )
    with TestClient(app) as client:
        request = AISessionCreate(
            provider="codex",
            target_model_id="org/reusable-model",
            target_source="huggingface",
            initial_prompt="deploy this model",
        )
        workspace = tmp_path / "data" / "ai-sessions" / "reusable"
        workspace.mkdir(parents=True)
        session = client.app.state.ai_store.create(
            request,
            str(workspace),
            session_id="reusable",
            display_name="Reusable deployment",
        )
        client.app.state.ai_store.update_status(session.id, "exited", exit_code=0)
        benchmark_workspace = tmp_path / "data" / "ai-sessions" / "benchmark-reusable"
        benchmark_workspace.mkdir(parents=True)
        client.app.state.ai_store.create(
            AISessionCreate(
                provider="codex",
                task_type="benchmark_analysis",
                display_name="Benchmark with same model",
            ),
            str(benchmark_workspace),
            session_id="benchmark-reusable",
            target_model_id="org/reusable-model",
        )

        matching = client.get(
            "/api/v1/ai/sessions",
            params={
                "include_archived": True,
                "target_model_id": "org/reusable-model",
                "task_type": "deployment",
            },
        )
        missing = client.get(
            "/api/v1/ai/sessions",
            params={"target_model_id": "org/other-model"},
        )
        assert matching.status_code == 200
        assert [item["id"] for item in matching.json()["items"]] == ["reusable"]
        assert missing.json()["total"] == 0

        resumed = client.post(
            "/api/v1/ai/sessions/reusable/resume",
            json={"prompt": "继续执行上次部署"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["id"] == "reusable"
        assert resumed.json()["status"] == "running"

        transcript = b""
        for _ in range(100):
            transcript = client.get("/api/v1/ai/sessions/reusable/transcript").content
            if b"resumed:" in transcript:
                break
            time.sleep(0.01)
        assert b"resume --last" in transcript
        assert "继续执行上次部署".encode() in transcript
        audit = client.get("/api/v1/ai/sessions/reusable/audit").json()["items"]
        assert any(item["event_type"] == "resumed" for item in audit)


def test_resume_atomically_replaces_uniformctl_symlink(tmp_path: Path) -> None:
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
        workspace = tmp_path / "data" / "ai-sessions" / "symlink-resume"
        workspace.mkdir(parents=True)
        session = client.app.state.ai_store.create(
            AISessionCreate(
                provider="codex",
                task_type="deployment",
                target_model_id="org/symlink-model",
                target_source="huggingface",
            ),
            str(workspace),
            session_id="symlink-resume",
        )
        client.app.state.ai_store.update_status(session.id, "exited", exit_code=0)
        outside = tmp_path / "must-not-be-overwritten"
        outside.write_text("sentinel", encoding="utf-8")
        control_script = workspace / "uniformctl"
        control_script.symlink_to(outside)

        resumed = client.post(
            f"/api/v1/ai/sessions/{session.id}/resume",
            json={"prompt": "继续部署"},
        )

        assert resumed.status_code == 200
        assert outside.read_text(encoding="utf-8") == "sentinel"
        assert not control_script.is_symlink()
        assert control_script.stat().st_mode & 0o777 == 0o700
        assert "Scoped Uniform deployment controller" in control_script.read_text(encoding="utf-8")


def test_ai_session_archive_restore_and_delete(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/bin/echo",
            claude_binary="/definitely/missing/claude",
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/ai/sessions",
            json={"provider": "codex", "display_name": "可存档会话"},
        ).json()
        session_id = created["id"]
        for _ in range(100):
            session = client.get(f"/api/v1/ai/sessions/{session_id}").json()
            if session["status"] not in {"starting", "running"}:
                break
            time.sleep(0.01)

        archived = client.post(f"/api/v1/ai/sessions/{session_id}/archive")
        assert archived.status_code == 200
        assert archived.json()["archived_at"] is not None
        assert client.get("/api/v1/ai/sessions").json()["total"] == 0
        archived_list = client.get("/api/v1/ai/sessions", params={"include_archived": True}).json()
        assert archived_list["total"] == 1

        restored = client.post(f"/api/v1/ai/sessions/{session_id}/restore")
        assert restored.status_code == 200
        assert restored.json()["archived_at"] is None
        workspace = Path(restored.json()["workspace_path"])
        assert workspace.exists()

        deleted = client.delete(f"/api/v1/ai/sessions/{session_id}")
        assert deleted.status_code == 204
        assert not workspace.exists()
        assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 404


def test_running_ai_session_can_be_force_deleted(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(data_dir=tmp_path / "data", worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        session = client.post("/api/v1/ai/sessions", json={"provider": "codex"}).json()
        session_id = session["id"]

        assert client.post(f"/api/v1/ai/sessions/{session_id}/archive").status_code == 409
        assert client.delete(f"/api/v1/ai/sessions/{session_id}").status_code == 409
        workspace = Path(session["workspace_path"])
        deleted = client.delete(f"/api/v1/ai/sessions/{session_id}?force=true")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/ai/sessions/{session_id}").status_code == 404
        assert not workspace.exists()


def test_instruction_is_sent_to_an_active_ai_session(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "IFS= read -r instruction\n"
        "printf 'received:%s\\n' \"$instruction\"\n"
        "sleep 0.1\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(data_dir=tmp_path / "data", worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        session = client.post(
            "/api/v1/ai/sessions",
            json={"provider": "codex", "initial_prompt": "wait for instruction"},
        ).json()
        client.app.state.ai_terminal.send_instruction(session["id"], "stop this service")
        transcript = b""
        for _ in range(100):
            transcript = client.get(f"/api/v1/ai/sessions/{session['id']}/transcript").content
            if b"received:stop this service" in transcript:
                break
            time.sleep(0.01)
        assert b"received:stop this service" in transcript
        audit = client.get(f"/api/v1/ai/sessions/{session['id']}/audit").json()["items"]
        assert audit[-1]["event_type"] == "user_instruction_sent"
        assert audit[-1]["details"]["delivery"] == "active_session"


def test_terminal_prompt_waits_before_sending_enter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = ai_terminal_module._ActiveSession(
        pid=1,
        master_fd=42,
        transcript_path=tmp_path / "terminal.cast",
    )
    writes: list[tuple[int, bytes]] = []
    delays: list[float] = []
    monkeypatch.setattr(
        ai_terminal_module.os,
        "write",
        lambda fd, data: writes.append((fd, data)) or len(data),
    )
    monkeypatch.setattr(ai_terminal_module.time, "sleep", delays.append)

    AITerminalManager._submit_terminal_prompt(active, "continue benchmark")

    assert writes == [(42, b"continue benchmark"), (42, b"\r")]
    assert delays == [0.15]


def test_ai_session_auto_resumes_after_rate_limit_and_honors_retry_limit(
    tmp_path: Path,
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "echo 'stream disconnected before completion: exceeded rate limit'\n"
        "IFS= read -r reply\n"
        "echo received:$reply\n"
        "sleep 0.1\n"
        "echo 'stream disconnected before completion: exceeded rate limit'\n"
        "sleep 5\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(data_dir=tmp_path / "data", worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        session = client.post("/api/v1/ai/sessions", json={"provider": "codex"}).json()
        session_id = session["id"]
        for _ in range(100):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            if b"exceeded rate limit" in transcript:
                break
            time.sleep(0.01)

        configured = client.post(
            f"/api/v1/ai/sessions/{session_id}/auto-resume",
            json={
                "enabled": True,
                "max_retries": 1,
                "base_delay_seconds": 1,
                "prompt": "continue deployment",
            },
        )
        assert configured.status_code == 200
        assert configured.json()["state"] == "waiting"
        assert configured.json()["retry_count"] == 1

        for _ in range(300):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            status = client.get(f"/api/v1/ai/sessions/{session_id}/auto-resume").json()
            if b"received:continue deployment" in transcript and status["state"] == "exhausted":
                break
            time.sleep(0.01)
        assert b"received:continue deployment" in transcript
        assert status["state"] == "exhausted"
        assert status["retry_count"] == 1
        audit = client.get(f"/api/v1/ai/sessions/{session_id}/audit").json()["items"]
        assert any(item["event_type"] == "auto_resume_sent" for item in audit)
        assert any(item["event_type"] == "auto_resume_exhausted" for item in audit)


def test_auto_resume_restarts_cli_after_unexpected_exit(tmp_path: Path) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
            'if [ "$2" = "resume" ]; then\n'
        "  echo resumed:$*\n"
        "  sleep 5\n"
        "  exit 0\n"
        "fi\n"
        "echo 'connection reset by peer'\n"
        "sleep 0.2\n"
        "exit 7\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(data_dir=tmp_path / "data", worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        session_id = client.post("/api/v1/ai/sessions", json={"provider": "codex"}).json()["id"]
        for _ in range(100):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            if b"connection reset by peer" in transcript:
                break
            time.sleep(0.01)
        configured = client.post(
            f"/api/v1/ai/sessions/{session_id}/auto-resume",
            json={
                "enabled": True,
                "max_retries": 2,
                "base_delay_seconds": 1,
                "prompt": "continue task",
            },
        )
        assert configured.status_code == 200

        for _ in range(300):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            if b"resumed:" in transcript:
                break
            time.sleep(0.01)
        assert b"resumed:" in transcript
        # PTY output can arrive before the background worker persists its audit event.
        deadline = time.monotonic() + 3
        sent = []
        while time.monotonic() < deadline:
            audit = client.get(f"/api/v1/ai/sessions/{session_id}/audit").json()["items"]
            sent = [item for item in audit if item["event_type"] == "auto_resume_sent"]
            if sent:
                break
            time.sleep(0.01)
        assert sent, "auto-resume must persist the delivery audit event"
        assert sent[-1]["details"]["delivery"] == "resumed_cli_session"


def test_auto_resume_uses_fresh_context_when_encrypted_context_is_invalid(
    tmp_path: Path,
) -> None:
    fake_codex = tmp_path / "fake-codex"
    invocations = tmp_path / "invocations.txt"
    fake_codex.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{invocations}'\n"
        "echo 'invalid_encrypted_content: encrypted content could not be decrypted'\n"
        "sleep 5\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(data_dir=tmp_path / "data", worker_enabled=False, codex_binary=str(fake_codex))
    )
    with TestClient(app) as client:
        session_id = client.post("/api/v1/ai/sessions", json={"provider": "codex"}).json()["id"]
        for _ in range(100):
            transcript = client.get(f"/api/v1/ai/sessions/{session_id}/transcript").content
            if b"invalid_encrypted_content" in transcript:
                break
            time.sleep(0.01)
        configured = client.post(
            f"/api/v1/ai/sessions/{session_id}/auto-resume",
            json={
                "enabled": True,
                "max_retries": 2,
                "base_delay_seconds": 1,
                "prompt": "continue from workspace",
            },
        )
        assert configured.status_code == 200
        assert configured.json()["last_reason"] == "AI 对话上下文无法解密"

        for _ in range(300):
            audit = client.get(f"/api/v1/ai/sessions/{session_id}/audit").json()["items"]
            sent = [item for item in audit if item["event_type"] == "auto_resume_sent"]
            if sent:
                break
            time.sleep(0.01)
        assert sent[-1]["details"]["delivery"] == "fresh_workspace_session"
        commands = invocations.read_text(encoding="utf-8").splitlines()
        assert len(commands) >= 2
        assert not commands[1].startswith("resume --last")
        assert "continue from workspace" in commands[1]


def test_reference_fetcher_blocks_local_network() -> None:
    with pytest.raises(ReferenceError, match="private, loopback"):
        SafeReferenceFetcher().fetch("http://127.0.0.1/private")


def test_benchmark_plan_defaults_mean_reference_script() -> None:
    request = AISessionCreate(
        provider="codex",
        task_type="benchmark_execution",
        benchmark_target_type="deployment",
        benchmark_target_id="deployment-1",
        benchmark_plan={},
    )

    assert request.benchmark_plan is not None
    assert request.benchmark_plan.io_shapes == []
    assert request.benchmark_plan.concurrencies == []
    assert request.benchmark_plan.endpoint == "reference"
    assert request.benchmark_plan.dataset == "reference"
    assert request.benchmark_plan.require_exact_output is None
    assert request.benchmark_plan.metrics == []

    with pytest.raises(ValueError, match="specified dataset requires dataset_path"):
        AISessionCreate(
            provider="codex",
            task_type="benchmark_execution",
            benchmark_target_type="deployment",
            benchmark_target_id="deployment-1",
            benchmark_plan={"dataset": "specified"},
        )

    with pytest.raises(ValueError, match="requires deployment parameter dimensions"):
        AISessionCreate(
            provider="codex",
            task_type="benchmark_execution",
            benchmark_target_type="deployment",
            benchmark_target_id="deployment-1",
            benchmark_plan={"ab_test_enabled": True},
        )


def test_ai_environment_does_not_forward_platform_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNIFORM_API_TOKEN", "secret")
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    environment = AITerminalManager._safe_environment()

    assert "UNIFORM_API_TOKEN" not in environment
    assert "HF_TOKEN" not in environment
    assert environment["OPENAI_API_KEY"] == "provider-secret"


def test_codex_and_claude_use_approval_modes(tmp_path: Path) -> None:
    codex = AITerminalManager._command(
        "codex",
        AISessionCreate(provider="codex", initial_prompt="debug", execution_mode="interactive"),
        tmp_path,
    )
    claude = AITerminalManager._command(
        "claude",
        AISessionCreate(
            provider="claude",
            initial_prompt="debug",
            execution_mode="interactive",
            ai_model="claude-opus-4-1",
        ),
        tmp_path,
    )

    sandbox_index = codex.index("--sandbox")
    approval_index = codex.index("--ask-for-approval")
    assert codex[sandbox_index : sandbox_index + 2] == ["--sandbox", "workspace-write"]
    assert codex[approval_index : approval_index + 2] == [
        "--ask-for-approval",
        "untrusted",
    ]
    assert claude[claude.index("--model") + 1] == "claude-opus-4-1"
    claude_permission_index = claude.index("--permission-mode")
    assert claude[claude_permission_index : claude_permission_index + 2] == [
        "--permission-mode",
        "default",
    ]


def test_autonomous_and_full_access_modes() -> None:
    autonomous = AITerminalManager._command(
        "codex",
        AISessionCreate(provider="codex", initial_prompt="deploy"),
        Path("/tmp/session"),
    )
    assert autonomous[autonomous.index("--ask-for-approval") + 1] == "on-request"
    assert autonomous[autonomous.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in autonomous
    assert 'approvals_reviewer="auto_review"' in autonomous

    full_access = AITerminalManager._command(
        "codex",
        AISessionCreate(
            provider="codex",
            initial_prompt="deploy",
            execution_mode="full_access",
            full_access_acknowledged=True,
        ),
        Path("/tmp/session"),
    )
    assert full_access[full_access.index("--ask-for-approval") + 1] == "never"
    assert full_access[full_access.index("--sandbox") + 1] == "danger-full-access"
    selected_model = AITerminalManager._command(
        "codex",
        AISessionCreate(
            provider="codex",
            initial_prompt="deploy",
            ai_model="gpt-5.6-terra",
            reasoning_effort="xhigh",
        ),
        Path("/tmp/session"),
    )
    assert selected_model[selected_model.index("--model") + 1] == "gpt-5.6-terra"
    assert 'model_reasoning_effort="xhigh"' in selected_model

    with pytest.raises(ValueError, match="full_access_acknowledged"):
        AISessionCreate(
            provider="codex",
            execution_mode="full_access",
        )

    targeted = AITerminalManager._command(
        "codex",
        AISessionCreate(
            provider="codex",
            initial_prompt="use this tutorial",
            target_model_id="org/model",
            target_source="huggingface",
        ),
        Path("/tmp/session"),
    )
    assert "RESULT_SCHEMA.md" in targeted[-1]
    assert "MULTI_NODE_TP_REFERENCE.md" not in targeted[-1]
    assert "scoped to one local node" in targeted[-1]
    assert "Do not claim success" in targeted[-1]

    multi_node = AITerminalManager._command(
        "codex",
        AISessionCreate(
            provider="codex",
            initial_prompt="deploy this model",
            target_model_id="org/model",
            target_source="huggingface",
            deployment_topology="multi_node",
        ),
        Path("/tmp/session"),
    )
    assert "MULTI_NODE_TP_REFERENCE.md" in multi_node[-1]
    assert "explicitly requested multi-node" in multi_node[-1]


def test_deployment_topology_requires_explicit_multi_node_intent() -> None:
    conditional = AISessionCreate(
        provider="codex",
        target_model_id="org/model",
        target_source="huggingface",
        initial_prompt="双机部署时核对通信配置，然后完成部署。",
    )
    explicit = conditional.model_copy(update={"initial_prompt": "请使用双机部署这个模型。"})
    single = conditional.model_copy(update={"initial_prompt": "请在单机部署，不要连接其它节点。"})

    assert AITerminalManager._resolve_deployment_topology(conditional) == (
        "single_node",
        "auto_default",
    )
    assert AITerminalManager._resolve_deployment_topology(explicit) == (
        "multi_node",
        "initial_prompt",
    )
    assert AITerminalManager._resolve_deployment_topology(single) == (
        "single_node",
        "initial_prompt",
    )
    assert AITerminalManager._resolve_deployment_topology(
        conditional,
        {"parameters": {"extra_args": ["--nnodes", "2"]}},
    ) == ("multi_node", "deployment.parameters.launch_command")


def test_benchmark_analysis_does_not_depend_on_auto_review(tmp_path: Path) -> None:
    request = AISessionCreate(
        provider="codex",
        task_type="benchmark_analysis",
        initial_prompt="analyze benchmark",
        execution_mode="autonomous",
    )

    command = AITerminalManager._command("codex", request, tmp_path)

    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert not any("approvals_reviewer" in item for item in command)


def test_codex_commands_always_disable_the_shared_daemon(tmp_path: Path) -> None:
    request = AISessionCreate(
        provider="codex",
        task_type="deployment",
        target_model_id="org/daemon-test",
        target_source="huggingface",
        initial_prompt="deploy this model",
    )

    command = AITerminalManager._command("codex", request, tmp_path)
    assert command[1] == "--no-daemon"
    assert command.count("--no-daemon") == 1

    database = Database(tmp_path / "daemon.db")
    database.initialize()
    session = AISessionStore(database).create(request, str(tmp_path))
    resumed = AITerminalManager._resume_command("codex", session, tmp_path, "继续")
    assert resumed[1] == "--no-daemon"
    assert resumed.count("--no-daemon") == 1

    fresh = AITerminalManager._fresh_resume_command(
        "codex", session, tmp_path, "继续"
    )
    assert fresh[1] == "--no-daemon"
    assert fresh.count("--no-daemon") == 1
    assert "resume" not in fresh
    assert "--last" not in fresh


def test_ai_model_session_has_scoped_deployment_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            worker_enabled=False,
            codex_binary=str(fake_codex),
            claude_binary="/definitely/missing/claude",
            api_token="platform-secret",
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "Qwen/Qwen-Test",
                "target_source": "huggingface",
            },
            headers={"X-API-Key": "platform-secret"},
        )
        assert created.status_code == 201
        assert created.json()["display_name"] == "Qwen-Test"
        assert created.json()["target_model_id"] == "Qwen/Qwen-Test"
        session_id = created.json()["id"]
        manager = client.app.state.ai_terminal
        capability = manager._capabilities[session_id]

        forbidden = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/deploy",
            json={"port": 19001},
            headers={"X-AI-Capability": "wrong"},
        )
        assert forbidden.status_code == 403

        deployed = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/deploy",
            json={"port": 19001},
            headers={"X-AI-Capability": capability},
        )
        assert deployed.status_code == 200
        assert deployed.json()["model_id"] == "Qwen/Qwen-Test"
        assert deployed.json()["parameters"]["source"] == "huggingface"

        status = client.get(
            f"/api/v1/ai/sessions/{session_id}/actions/deployment",
            headers={"X-AI-Capability": capability},
        )
        assert status.status_code == 200
        assert status.json()["id"] == deployed.json()["id"]

        unsafe = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/deploy",
            json={"extra_args": ["--port 9999"]},
            headers={"X-AI-Capability": capability},
        )
        assert unsafe.status_code == 422

        workspace = Path(created.json()["workspace_path"])
        control_script = workspace / "uniformctl"
        assert control_script.stat().st_mode & 0o100
        control_source = control_script.read_text(encoding="utf-8")
        compile(control_source, str(control_script), "exec")
        assert 'sub.add_parser("report")' in control_source
        assert 'request("POST", "result/managed", {})' in control_source
        result_schema = (workspace / "RESULT_SCHEMA.md").read_text(encoding="utf-8")
        assert '"container_name"' in result_schema
        assert '"launch_command"' in result_schema
        assert '"health_check"' in result_schema
        assert '"inference_test"' in result_schema
        security_policy = (workspace / "SECURITY.md").read_text(encoding="utf-8")
        assert "Do not force a particular inference engine" in security_policy
        assert "tutorial's explicitly recommended engine, image, and version" in security_policy
        assert "Do not pause for confirmation" in security_policy
        assert not (workspace / "MULTI_NODE_TP_REFERENCE.md").exists()
        deployment_context = json.loads(
            (workspace / "deployment-context.json").read_text(encoding="utf-8")
        )
        assert deployment_context["deployment_topology"] == {
            "requested": "auto",
            "resolved": "single_node",
            "source": "auto_default",
        }
        assert "single local node" in security_policy

        multi_created = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "Qwen/Qwen-Multi-Test",
                "target_source": "huggingface",
                "deployment_topology": "multi_node",
            },
            headers={"X-API-Key": "platform-secret"},
        )
        assert multi_created.status_code == 201
        multi_workspace = Path(multi_created.json()["workspace_path"])
        tp_reference = (multi_workspace / "MULTI_NODE_TP_REFERENCE.md").read_text(encoding="utf-8")
        assert "10.0.22.1" in tp_reference
        assert "rocep1s0f1" in tp_reference
        assert "NCCL_IB_GID_INDEX=3" in tp_reference

        reported = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result",
            headers={"X-AI-Capability": capability},
            json={
                "engine": "sglang",
                "container_name": "tutorial-container",
                "container_id": "sha256:abc",
                "image": "example/sglang:recommended",
                "port": 19001,
                "service_url": "http://127.0.0.1:19001",
                "launch_command": "docker run example/sglang:recommended",
                "health_check": {
                    "method": "GET",
                    "url": "http://127.0.0.1:19001/health",
                    "status_code": 200,
                    "passed": True,
                    "details": "healthy",
                },
                "inference_test": {
                    "method": "POST",
                    "url": "http://127.0.0.1:19001/v1/chat/completions",
                    "status_code": 200,
                    "passed": True,
                    "request_summary": "one short prompt",
                    "response_summary": "one choice returned",
                },
            },
        )
        assert reported.status_code == 200
        assert reported.json()["status"] == "verified"
        assert reported.json()["engine"] == "sglang"
        snapshot = (
            client.app.state.ai_terminal.sessions_dir.parent
            / "ai-service-snapshots"
            / session_id
            / "SERVICE_HANDOFF.json"
        )
        assert snapshot.is_file()
        snapshot_payload = json.loads(snapshot.read_text(encoding="utf-8"))
        assert snapshot_payload["deployment_result"]["container_name"] == "tutorial-container"
        failed_payload = reported.json()
        for generated_field in ("session_id", "status", "created_at", "updated_at"):
            failed_payload.pop(generated_field)
        failed_payload["inference_test"]["passed"] = False
        failed = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result",
            headers={"X-AI-Capability": capability},
            json=failed_payload,
        )
        assert failed.status_code == 200
        assert failed.json()["status"] == "failed"
        assert failed.json()["created_at"] == reported.json()["created_at"]

        result = client.get(
            f"/api/v1/ai/sessions/{session_id}/result",
            headers={"X-API-Key": "platform-secret"},
        )
        assert result.status_code == 200
        assert result.json()["container_name"] == "tutorial-container"
        assert result.json()["status"] == "failed"

        deployment_store = client.app.state.store
        for target_status in [
            DeploymentStatus.PREFLIGHT,
            DeploymentStatus.DOWNLOADING_MODEL,
            DeploymentStatus.PULLING_IMAGE,
            DeploymentStatus.STARTING_CONTAINER,
            DeploymentStatus.LOADING_MODEL,
            DeploymentStatus.HEALTH_CHECKING,
            DeploymentStatus.RUNNING,
        ]:
            deployment_store.transition(
                deployed.json()["id"],
                target_status,
                message="test transition",
            )
        monkeypatch.setattr(
            client.app.state.runtime,
            "container_metadata",
            lambda deployment: {
                "container_name": f"uniform-vllm-{deployment.id[:8]}",
                "container_id": "managed-container-id",
                "launch_command": "docker run managed-vllm",
            },
        )
        managed = client.post(
            f"/api/v1/ai/sessions/{session_id}/actions/result/managed",
            headers={"X-AI-Capability": capability},
            json={},
        )
        assert managed.status_code == 200
        assert managed.json()["status"] == "verified"
        assert managed.json()["engine"] == "vllm"
        assert managed.json()["container_id"] == "managed-container-id"
        assert managed.json()["health_check"]["passed"] is True
        assert managed.json()["inference_test"]["passed"] is True

        audit = client.get(
            f"/api/v1/ai/sessions/{session_id}/audit",
            headers={"X-API-Key": "platform-secret"},
        )
        assert (
            sum(
                item["event_type"] == "deployment_result_reported" for item in audit.json()["items"]
            )
            == 3
        )

        client.post(
            f"/api/v1/ai/sessions/{session_id}/stop",
            headers={"X-API-Key": "platform-secret"},
        )
        for _ in range(100):
            if not manager.validate_capability(session_id, capability):
                break
            time.sleep(0.01)
        assert manager.validate_capability(session_id, capability) is False


def test_ai_model_target_validation(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path,
            worker_enabled=False,
            codex_binary="/bin/echo",
        )
    )
    with TestClient(app) as client:
        whitespace = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "target_model_id": "not a/model",
                "target_source": "huggingface",
            },
        )
        assert whitespace.status_code == 422

        ambiguous = client.post(
            "/api/v1/ai/sessions",
            json={
                "provider": "codex",
                "deployment_id": "existing",
                "target_model_id": "Qwen/Qwen-Test",
                "target_source": "huggingface",
            },
        )
        assert ambiguous.status_code == 422

        invalid_ai_model = client.post(
            "/api/v1/ai/sessions",
            json={"provider": "codex", "ai_model": "invalid model"},
        )
        assert invalid_ai_model.status_code == 422

        unsupported_effort = client.post(
            "/api/v1/ai/sessions",
            json={"provider": "claude", "reasoning_effort": "high"},
        )
        assert unsupported_effort.status_code == 422
