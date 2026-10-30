"""Small stdio MCP adapter: only scoped platform accuracy actions, never a shell."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.parse import urlparse

import httpx

from .services.ai_accuracy import AIAccuracyAnalysis, AIAccuracyProposal


def tool_definitions() -> list[dict[str, Any]]:
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    definitions = [
        (
            "accuracy_inspect",
            "Inspect the selected target, sample budget, environment and whitelist.",
            empty,
        ),
        (
            "accuracy_propose_plan",
            "Propose a plan for Web UI user confirmation; does not run models.",
            AIAccuracyProposal.model_json_schema(),
        ),
        (
            "accuracy_status",
            "Read this workflow's real runs and canonical EvalScope metrics.",
            empty,
        ),
        (
            "accuracy_start",
            "Start only an already user-confirmed plan; retries are idempotent.",
            empty,
        ),
        ("accuracy_logs", "Read only this workflow's redacted evaluation logs.", empty),
        ("accuracy_cancel", "Cancel this workflow's evaluation, never the model service.", empty),
        (
            "accuracy_record_analysis",
            "Store interpretation of the completed full run, not new scores.",
            AIAccuracyAnalysis.model_json_schema(),
        ),
    ]
    read_only = {"accuracy_inspect", "accuracy_status", "accuracy_logs"}
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": name in read_only},
        }
        for name, description, schema in definitions
    ]


class AccuracyMCP:
    server_name = "uniform_accuracy"

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return tool_definitions()

    def __init__(self, base: str, session_id: str, token: str) -> None:
        parsed = urlparse(base)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("accuracy MCP requires the platform's loopback control URL")
        if not session_id or any(char not in "0123456789abcdef-" for char in session_id):
            raise ValueError("invalid scoped session ID")
        if not token:
            raise ValueError("missing scoped capability")
        self.base = f"{base.rstrip('/')}/api/v1/ai/sessions/{session_id}/actions/accuracy"
        self.client = httpx.Client(
            headers={"X-AI-Capability": token},
            trust_env=False,
            follow_redirects=False,
            timeout=40,
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        actions = {
            "accuracy_inspect": ("GET", "inspect"),
            "accuracy_propose_plan": ("POST", "plan"),
            "accuracy_status": ("GET", "status"),
            "accuracy_start": ("POST", "start"),
            "accuracy_logs": ("GET", "logs"),
            "accuracy_cancel": ("POST", "cancel"),
            "accuracy_record_analysis": ("POST", "analysis"),
        }
        if name not in actions:
            raise ValueError("unknown accuracy tool")
        if name == "accuracy_propose_plan":
            arguments = AIAccuracyProposal.model_validate(arguments).model_dump(mode="json")
        elif name == "accuracy_record_analysis":
            arguments = AIAccuracyAnalysis.model_validate(arguments).model_dump(mode="json")
        elif arguments:
            raise ValueError("this tool accepts no arguments")
        method, suffix = actions[name]
        response = self.client.request(
            method, f"{self.base}/{suffix}", json=arguments if method == "POST" else None
        )
        if response.is_redirect:
            raise ValueError("platform redirects are not allowed")
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": "platform returned a non-JSON response"}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": response.status_code >= 400,
        }

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if "id" not in request:
            return None
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            if method == "initialize":
                supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
                requested = params.get("protocolVersion")
                response["result"] = {
                    "protocolVersion": requested if requested in supported else "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.server_name, "version": "1.0.0"},
                }
            elif method == "ping":
                response["result"] = {}
            elif method == "tools/list":
                response["result"] = {"tools": self.definitions()}
            elif method == "tools/call":
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                response["result"] = self.call_tool(params.get("name", ""), arguments)
            else:
                response["error"] = {"code": -32601, "message": "method not found"}
        except (ValueError, httpx.HTTPError):
            # Never serialize HTTP request headers, credentials, or a validation input value.
            if method == "tools/call":
                response["result"] = {
                    "content": [
                        {"type": "text", "text": "Invalid tool arguments or platform unavailable"}
                    ],
                    "isError": True,
                }
            else:
                response["error"] = {"code": -32602, "message": "invalid params"}
        return response


def main(server_type: type[AccuracyMCP] = AccuracyMCP) -> None:
    server = server_type(
        os.environ["UNIFORM_CONTROL_URL"],
        os.environ["UNIFORM_AI_SESSION_ID"],
        os.environ["UNIFORM_AI_CAPABILITY_TOKEN"],
    )
    try:
        while line := sys.stdin.readline(1_000_001):
            if len(line) > 1_000_000:
                break
            try:
                request = json.loads(line)
                response = server.handle(request) if isinstance(request, dict) else None
            except ValueError:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        server.client.close()


if __name__ == "__main__":
    main()
