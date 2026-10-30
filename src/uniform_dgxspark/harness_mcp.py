"""Standard stdio MCP server that lets an external AI harness drive the platform.

The server only speaks the platform's HTTP API with an API key, so it works from
another laptop on the LAN or over a tunnel. It never runs shell commands, never
touches Docker directly, and never prints the configured API key.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx

TERMINAL_STATUSES = {"running", "stopped", "failed", "cancelled"}


def _schema(
    properties: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
    *,
    additional: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _target(description: str = "deployment or ai_session") -> dict[str, Any]:
    return {
        "type": "string",
        "enum": ["deployment", "ai_session"],
        "description": description,
    }


def tool_definitions() -> list[dict[str, Any]]:
    empty = _schema()
    target_type = _target()
    target_id = {"type": "string", "description": "Deployment ID or AI session ID."}
    tools: list[tuple[str, str, dict[str, Any], bool]] = [
        (
            "platform_status",
            "Check that the platform is reachable and healthy.",
            empty,
            True,
        ),
        (
            "open_platform_ui",
            "Return the platform web UI address for this machine.",
            empty,
            True,
        ),
        (
            "list_catalog_models",
            "Search the platform catalog (Hugging Face + ModelScope).",
            _schema(
                {
                    "query": {"type": "string"},
                    "source": {"type": "string", "enum": ["huggingface", "modelscope"]},
                    "sort": {
                        "type": "string",
                        "enum": ["popularity", "recent", "downloads", "likes", "name"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                }
            ),
            True,
        ),
        (
            "list_local_models",
            "List models already present on the DGX Spark disks.",
            empty,
            True,
        ),
        (
            "list_deployments",
            "List recent deployments with their live status.",
            _schema(
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                    "include_archived": {"type": "boolean"},
                }
            ),
            True,
        ),
        (
            "get_deployment",
            "Read one deployment, including parameters and error details.",
            _schema({"deployment_id": {"type": "string"}}, ("deployment_id",)),
            True,
        ),
        (
            "deployment_logs",
            "Read the container log tail for one deployment.",
            _schema(
                {
                    "deployment_id": {"type": "string"},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 5000},
                },
                ("deployment_id",),
            ),
            True,
        ),
        (
            "deploy_model",
            "Create a deployment on the DGX Spark and return immediately.",
            _schema(
                {
                    "model_id": {"type": "string", "description": "owner/name or local model id."},
                    "revision": {"type": "string"},
                    "display_name": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": ["huggingface", "modelscope"],
                    },
                    "local_model_path": {
                        "type": "string",
                        "description": "Absolute path on the DGX Spark; skips the download.",
                    },
                    "image": {"type": "string"},
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                    "served_model_name": {"type": "string"},
                    "max_model_len": {"type": "integer", "minimum": 1},
                    "gpu_memory_utilization": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                    },
                    "startup_timeout_seconds": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 7200,
                    },
                    "trust_remote_code": {"type": "boolean"},
                    "remote_code_acknowledged": {"type": "boolean"},
                    "extra_args": {"type": "array", "items": {"type": "string"}},
                },
                ("model_id",),
            ),
            False,
        ),
        (
            "wait_for_deployment",
            "Poll one deployment until it is running, stopped, failed or cancelled.",
            _schema(
                {
                    "deployment_id": {"type": "string"},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 10,
                        "maximum": 3600,
                    },
                    "interval_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                },
                ("deployment_id",),
            ),
            True,
        ),
        (
            "stop_deployment",
            "Stop a deployment and revoke its publication.",
            _schema({"deployment_id": {"type": "string"}}, ("deployment_id",)),
            False,
        ),
        (
            "publish_service",
            "Publish a running model to the LAN and return the Base URL and API key.",
            _schema(
                {
                    "target_type": target_type,
                    "target_id": target_id,
                },
                ("target_id",),
            ),
            False,
        ),
        (
            "publication_status",
            "Read LAN/public exposure state for one service.",
            _schema({"target_type": target_type, "target_id": target_id}, ("target_id",)),
            True,
        ),
        (
            "rotate_publication_key",
            "Rotate the API key of an enabled publication.",
            _schema({"target_type": target_type, "target_id": target_id}, ("target_id",)),
            False,
        ),
        (
            "unpublish_service",
            "Revoke LAN/public access for one service.",
            _schema({"target_type": target_type, "target_id": target_id}, ("target_id",)),
            False,
        ),
        (
            "enable_external_access",
            "Open a Cloudflare Quick Tunnel for a published service.",
            _schema({"target_type": target_type, "target_id": target_id}, ("target_id",)),
            False,
        ),
        (
            "disable_external_access",
            "Close the public tunnel of a published service.",
            _schema({"target_type": target_type, "target_id": target_id}, ("target_id",)),
            False,
        ),
    ]
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": schema,
            "annotations": {"readOnlyHint": read_only},
        }
        for name, description, schema, read_only in tools
    ]


class HarnessMCP:
    """Translate MCP tool calls into platform HTTP requests."""

    server_name = "uniform_platform"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("platform URL must be an http(s) base URL without credentials")
        self.base = base_url.strip().rstrip("/")
        self.api_key = api_key or ""
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        self.client = client or httpx.Client(
            headers=headers,
            trust_env=False,
            follow_redirects=False,
            timeout=60,
        )

    # -- HTTP helpers -------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> tuple[bool, Any]:
        try:
            response = self.client.request(
                method,
                f"{self.base}{path}",
                params={key: value for key, value in (params or {}).items() if value is not None},
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as error:
            return False, {"error": f"platform unreachable: {type(error).__name__}"}
        if response.is_redirect:
            return False, {"error": "platform redirects are not allowed"}
        if response.status_code == 204:
            return True, {"ok": True}
        try:
            body: Any = response.json()
        except ValueError:
            body = {"detail": response.text[:2000]}
        if response.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else None
            if response.status_code == 401:
                return False, {"error": "platform rejected the API key", "status": 401}
            return False, {"status": response.status_code, "error": detail or body}
        return True, body

    def _publication_path(self, arguments: dict[str, Any]) -> str:
        target_type = arguments.get("target_type") or "deployment"
        target_id = str(arguments.get("target_id") or "").strip()
        if target_type not in {"deployment", "ai_session"}:
            raise ValueError("target_type must be deployment or ai_session")
        if not target_id or "/" in target_id:
            raise ValueError("target_id is required")
        return f"/api/v1/service-publications/{target_type}/{target_id}"

    # -- Tool dispatch -------------------------------------------------
    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "platform_status": self._platform_status,
            "open_platform_ui": self._open_platform_ui,
            "list_catalog_models": self._list_catalog_models,
            "list_local_models": self._list_local_models,
            "list_deployments": self._list_deployments,
            "get_deployment": self._get_deployment,
            "deployment_logs": self._deployment_logs,
            "deploy_model": self._deploy_model,
            "wait_for_deployment": self._wait_for_deployment,
            "stop_deployment": self._stop_deployment,
            "publish_service": self._publish_service,
            "publication_status": self._publication_status,
            "rotate_publication_key": self._rotate_publication_key,
            "unpublish_service": self._unpublish_service,
            "enable_external_access": self._enable_external_access,
            "disable_external_access": self._disable_external_access,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError("unknown tool")
        return handler(arguments)

    def _result(self, ok: bool, payload: Any) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": not ok,
        }

    def _platform_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("this tool accepts no arguments")
        return self._result(*self._request("GET", "/api/v1/health"))

    def _open_platform_ui(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("this tool accepts no arguments")
        return self._result(
            True,
            {
                "url": f"{self.base}/",
                "note": "Open this address in a browser and log in with the platform API key.",
            },
        )

    def _list_catalog_models(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = {
            "query": arguments.get("query", ""),
            "source": arguments.get("source"),
            "sort": arguments.get("sort"),
            "limit": arguments.get("limit"),
            "offset": arguments.get("offset"),
        }
        return self._result(*self._request("GET", "/api/v1/catalog/models", params=params))

    def _list_local_models(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise ValueError("this tool accepts no arguments")
        return self._result(*self._request("GET", "/api/v1/local-models", timeout=120))

    def _list_deployments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params = {
            "limit": arguments.get("limit"),
            "offset": arguments.get("offset"),
            "include_archived": arguments.get("include_archived"),
        }
        return self._result(*self._request("GET", "/api/v1/deployments", params=params))

    def _deployment_id(self, arguments: dict[str, Any]) -> str:
        deployment_id = str(arguments.get("deployment_id") or "").strip()
        if not deployment_id or "/" in deployment_id:
            raise ValueError("deployment_id is required")
        return deployment_id

    def _get_deployment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        deployment_id = self._deployment_id(arguments)
        return self._result(*self._request("GET", f"/api/v1/deployments/{deployment_id}"))

    def _deployment_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        deployment_id = self._deployment_id(arguments)
        return self._result(
            *self._request(
                "GET",
                f"/api/v1/deployments/{deployment_id}/logs",
                params={"tail": arguments.get("tail")},
                timeout=120,
            )
        )

    def _deploy_model(self, arguments: dict[str, Any]) -> dict[str, Any]:
        model_id = str(arguments.get("model_id") or "").strip()
        if not model_id or any(char.isspace() for char in model_id):
            raise ValueError("model_id must be provided without whitespace")
        parameters = {
            key: arguments[key]
            for key in (
                "port",
                "source",
                "image",
                "served_model_name",
                "local_model_path",
                "max_model_len",
                "gpu_memory_utilization",
                "startup_timeout_seconds",
                "trust_remote_code",
                "remote_code_acknowledged",
                "extra_args",
            )
            if arguments.get(key) is not None
        }
        payload: dict[str, Any] = {"model_id": model_id, "parameters": parameters}
        for key in ("revision", "display_name"):
            if arguments.get(key):
                payload[key] = arguments[key]
        return self._result(
            *self._request("POST", "/api/v1/deployments", payload=payload, timeout=120)
        )

    def _wait_for_deployment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        deployment_id = self._deployment_id(arguments)
        deadline = time.monotonic() + min(float(arguments.get("timeout_seconds") or 900), 3600)
        interval = max(float(arguments.get("interval_seconds") or 5), 1.0)
        started = time.monotonic()
        while True:
            ok, payload = self._request("GET", f"/api/v1/deployments/{deployment_id}")
            if not ok:
                return self._result(False, payload)
            status = str(payload.get("status") or "")
            if status in TERMINAL_STATUSES:
                payload = dict(payload)
                payload["waited_seconds"] = round(time.monotonic() - started, 1)
                return self._result(True, payload)
            if time.monotonic() >= deadline:
                return self._result(
                    False,
                    {
                        "status": status,
                        "error": "timed out before the deployment reached a terminal state",
                        "waited_seconds": round(time.monotonic() - started, 1),
                    },
                )
            time.sleep(interval)

    def _stop_deployment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        deployment_id = self._deployment_id(arguments)
        return self._result(
            *self._request("POST", f"/api/v1/deployments/{deployment_id}/stop", timeout=120)
        )

    def _publish_service(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("POST", path, timeout=180))

    def _publication_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("GET", path))

    def _rotate_publication_key(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("POST", f"{path}/rotate-key"))

    def _unpublish_service(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("DELETE", path))

    def _enable_external_access(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("POST", f"{path}/external-access", timeout=180))

    def _disable_external_access(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = self._publication_path(arguments)
        return self._result(*self._request("DELETE", f"{path}/external-access", timeout=120))

    # -- MCP protocol ---------------------------------------------------
    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return tool_definitions()

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
        except (ValueError, TypeError):
            if method == "tools/call":
                response["result"] = {
                    "content": [{"type": "text", "text": "Invalid tool arguments"}],
                    "isError": True,
                }
            else:
                response["error"] = {"code": -32602, "message": "invalid params"}
        return response


def main() -> None:
    base_url = os.environ.get("UNIFORM_PLATFORM_URL") or os.environ.get("UNIFORM_CONTROL_URL") or ""
    api_key = (
        os.environ.get("UNIFORM_PLATFORM_API_KEY") or os.environ.get("UNIFORM_API_TOKEN") or ""
    )
    if not base_url:
        print(
            "UNIFORM_PLATFORM_URL is required, e.g. http://10.21.212.6:18180",
            file=sys.stderr,
        )
        raise SystemExit(2)
    server = HarnessMCP(base_url, api_key)
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
