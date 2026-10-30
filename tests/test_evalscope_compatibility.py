from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROBE_PATH = PROJECT_ROOT / "scripts" / "probe-evalscope-accuracy.py"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "evalscope" / "gsm8k-report-v2.json"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_evalscope_accuracy", PROBE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load_probe()


@pytest.fixture
def report() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_frozen_evalscope_identity(probe: ModuleType) -> None:
    assert probe.EXPECTED_EVALSCOPE_VERSION == "0.0.0_dev"
    assert probe.EXPECTED_EVALSCOPE_COMMIT == "ccb9019ff56fd7dcc8602f4d17b706b31228edca"
    assert probe.EXPECTED_REPORT_SCHEMA_VERSION == 2


def test_v2_gsm8k_report_fixture_is_accepted(probe: ModuleType, report: dict[str, Any]) -> None:
    result = probe.validate_evalscope_report(report, expected_model="stage-36-fixture-model")

    assert result == {
        "schema_version": 2,
        "model_name": "stage-36-fixture-model",
        "dataset_name": "gsm8k",
        "metric": "Accuracy",
        "score": 1.0,
        "requested": 2,
        "succeeded": 2,
        "errored": 0,
        "complete": True,
    }


def test_unknown_report_schema_is_rejected(probe: ModuleType, report: dict[str, Any]) -> None:
    report["schema_version"] = 3

    with pytest.raises(probe.ProbeError, match="unsupported EvalScope report schema_version=3"):
        probe.validate_evalscope_report(report)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("succeeded", 1, "both GSM8K samples must complete successfully"),
        ("errored", 1, "both GSM8K samples must complete successfully"),
        ("incomplete", True, "both GSM8K samples must complete successfully"),
    ],
)
def test_incomplete_execution_is_rejected(
    probe: ModuleType,
    report: dict[str, Any],
    field: str,
    value: Any,
    message: str,
) -> None:
    report["execution_summary"][field] = value

    with pytest.raises(probe.ProbeError, match=message):
        probe.validate_evalscope_report(report)


def test_completed_progress_is_accepted(probe: ModuleType) -> None:
    result = probe.validate_progress(
        {
            "status": "completed",
            "pipeline": "eval",
            "total_count": 5,
            "processed_count": 5,
            "percent": 100.0,
            "updated_at": "2026-10-21T00:00:00",
        }
    )

    assert result["status"] == "completed"
    assert result["percent"] == 100.0


def test_command_is_controlled_and_contains_no_api_key(probe: ModuleType, tmp_path: Path) -> None:
    command = probe.build_evalscope_command(
        Path("/opt/evalscope/bin/evalscope"),
        "http://127.0.0.1:8000/v1",
        "local-model",
        tmp_path,
    )

    assert command[0] == "/opt/evalscope/bin/evalscope"
    assert "--datasets" in command and command[command.index("--datasets") + 1] == "gsm8k"
    assert "--limit" in command and command[command.index("--limit") + 1] == "2"
    assert "--dataset-dir" in command
    assert command[command.index("--dataset-dir") + 1] == str(tmp_path / "datasets")
    assert json.loads(command[command.index("--dataset-args") + 1]) == {
        "gsm8k": {"few_shot_num": 0}
    }
    assert "--enable-progress-tracker" in command
    assert "--no-collect-perf" in command
    assert "--api-key" not in command
    assert all(";" not in argument for argument in command)

    environment = probe.build_evalscope_environment(tmp_path)
    assert environment["MODELSCOPE_CACHE"] == str(tmp_path / "modelscope-cache")
    assert environment["EVALSCOPE_CACHE"] == str(tmp_path / "evalscope-cache")
    assert environment["HF_HOME"] == str(tmp_path / "huggingface-cache")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000/v1",
        "http://example.com:8000/v1",
        "http://127.0.0.1:8000/not-v1",
        "http://user:secret@127.0.0.1:8000/v1",
    ],
)
def test_nonlocal_or_unsafe_base_url_is_rejected(probe: ModuleType, base_url: str) -> None:
    with pytest.raises(probe.ProbeError):
        probe.validate_local_base_url(base_url)


def test_fake_openai_models_and_chat_completions_are_compatible(probe: ModuleType) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(("GET", self.path, None))
            self._send({"object": "list", "data": [{"id": "fake-local-model", "object": "model"}]})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            requests.append(("POST", self.path, body))
            self._send(
                {
                    "id": "chatcmpl-stage36",
                    "object": "chat.completion",
                    "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "4"}}],
                }
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        result = probe.probe_openai_compatibility(f"http://{host}:{port}/v1", "fake-local-model")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["models_endpoint"] == "compatible"
    assert result["chat_completions_endpoint"] == "compatible"
    assert [(method, path) for method, path, _ in requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ]
    assert requests[1][2] == {
        "model": "fake-local-model",
        "messages": [{"role": "user", "content": "Return the number 4 and nothing else."}],
        "temperature": 0,
        "max_tokens": 16,
    }
