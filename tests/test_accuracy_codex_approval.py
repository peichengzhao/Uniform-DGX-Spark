"""Real installed Codex approval engine; model and platform HTTP are local test doubles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from uniform_dgxspark.accuracy_mcp import tool_definitions
from uniform_dgxspark.dataset_integration_mcp import DatasetIntegrationMCP
from uniform_dgxspark.services.ai_terminal import AITerminalManager


def response_events(output: dict[str, Any], sequence: int) -> bytes:
    response = {
        "id": f"resp_local_{sequence}",
        "object": "response",
        "status": "completed",
        "output": [output],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {"type": "response.output_item.added", "output_index": 0, "item": output},
    ]
    if output["type"] == "function_call":
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "item_id": output["id"],
                "output_index": 0,
                "arguments": output["arguments"],
            }
        )
    else:
        events.append(
            {
                "type": "response.output_text.delta",
                "item_id": output["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": output["content"][0]["text"],
            }
        )
    events.extend(
        [
            {"type": "response.output_item.done", "output_index": 0, "item": output},
            {"type": "response.completed", "response": response},
        ]
    )
    return "".join(
        "event: "
        + event["type"]
        + "\ndata: "
        + json.dumps({**event, "sequence_number": i})
        + "\n\n"
        for i, event in enumerate(events)
    ).encode()


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("workflow", ["accuracy", "dataset"])
def test_installed_codex_rejects_prompt_but_calls_explicitly_approved_inspect(
    tmp_path: Path, approved: bool, workflow: str
) -> None:
    server_name = "uniform_accuracy" if workflow == "accuracy" else "uniform_dataset"
    inspect_tool = "accuracy_inspect" if workflow == "accuracy" else "dataset_inspect"
    action_path = "accuracy" if workflow == "accuracy" else "datasets"
    definitions = (
        tool_definitions() if workflow == "accuracy" else DatasetIntegrationMCP.definitions()
    )
    executable = os.getenv("UNIFORM_TEST_CODEX") or shutil.which("codex")
    if not executable and Path("/usr/lib/chatgpt/resources/codex").is_file():
        executable = "/usr/lib/chatgpt/resources/codex"
    if not executable:
        if os.getenv("UNIFORM_REQUIRE_CODEX") == "1":
            pytest.fail("Installed Codex is required for the MCP approval regression")
        pytest.skip("Codex is not installed")
    inspected: list[str] = []
    requests: list[dict[str, Any]] = []
    errors: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == f"/api/v1/ai/sessions/aaaaaaaa/actions/{action_path}/inspect":
                if self.headers.get("X-AI-Capability") != "local-test-capability":
                    self.send(403, b"{}", "application/json")
                    return
                inspected.append(self.path)
                self.send(200, b'{"test_marker":"INSPECT_OK_LOCAL_TEST"}', "application/json")
            else:
                errors.append("Unexpected GET " + self.path)
                self.send(404, b"{}", "application/json")

        def do_POST(self) -> None:
            if self.path != "/v1/responses":
                errors.append("Unexpected POST " + self.path)
                self.send(404, b"{}", "application/json")
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                output = {
                    "id": "fc_local_inspect",
                    "type": "function_call",
                    "call_id": "call_local_inspect",
                    "namespace": "mcp__" + server_name,
                    "name": inspect_tool,
                    "arguments": "{}",
                    "status": "completed",
                }
            else:
                output = {
                    "id": "msg_local_done",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Local approval check complete.",
                            "annotations": [],
                        }
                    ],
                }
            self.send(200, response_events(output, len(requests)), "text/event-stream")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    config = (
        AITerminalManager._accuracy_codex_config()
        if workflow == "accuracy"
        else AITerminalManager._dataset_codex_config()
    )
    if not approved:
        config += [
            "-c",
            f'mcp_servers.{server_name}.tools.{inspect_tool}.approval_mode="prompt"',
        ]
    command = [
        executable,
        "--no-daemon",
        *config,
        "-c",
        'model_provider="local_approval_test"',
        "-c",
        'model_providers.local_approval_test={name="Local approval test",base_url='
        + json.dumps(base + "/v1")
        + ',wire_api="responses",requires_openai_auth=false}',
        "-c",
        "features.enable_request_compression=false",
        "-c",
        "sqlite_home=" + json.dumps(str(tmp_path / "codex-state")),
        "-c",
        "log_dir=" + json.dumps(str(tmp_path / "codex-logs")),
        "-c",
        'history.persistence="none"',
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--cd",
        str(tmp_path),
        "--model",
        "local-approval-test",
        "--json",
        f"Call {inspect_tool} once, then stop. This is an isolated local approval test.",
    ]
    environment = {
        **os.environ,
        "UNIFORM_CONTROL_URL": base,
        "UNIFORM_AI_SESSION_ID": "aaaaaaaa",
        "UNIFORM_AI_CAPABILITY_TOKEN": "local-test-capability",
    }
    try:
        result = subprocess.run(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=40,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert not errors, errors
    assert len(requests) == 2, result.stdout + result.stderr
    model_tools = requests[0]["tools"]
    namespaces = [tool for tool in model_tools if tool["type"] == "namespace"]
    assert [tool["name"] for tool in namespaces] == ["mcp__" + server_name]
    assert {tool["name"] for tool in namespaces[0]["tools"]} == {
        tool["name"] for tool in definitions
    }
    assert not {tool["name"] for tool in model_tools if "name" in tool} & {
        "exec_command",
        "shell_command",
        "apply_patch",
        "spawn_agent",
        "send_input",
    }
    tool_output = json.dumps(requests[-1].get("input", []))
    if approved:
        assert len(inspected) == 1, tool_output
        assert "INSPECT_OK_LOCAL_TEST" in tool_output
        assert "MCP tool call requires approval" not in tool_output
    else:
        assert inspected == []
        assert "MCP tool call requires approval, but approval policy is never" in tool_output
