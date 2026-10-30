from __future__ import annotations

import json

import httpx
import pytest

from uniform_dgxspark.harness_mcp import HarnessMCP, tool_definitions


def build(handler) -> HarnessMCP:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-API-Key": "platform-secret"},
    )
    return HarnessMCP("http://10.0.0.5:18180", "platform-secret", client=client)


def body(response) -> dict:
    return json.loads(response["content"][0]["text"])


def test_tool_list_covers_deploy_and_publish_without_leaking_credentials() -> None:
    tools = tool_definitions()
    names = {tool["name"] for tool in tools}
    assert {
        "platform_status",
        "open_platform_ui",
        "list_catalog_models",
        "deploy_model",
        "wait_for_deployment",
        "publish_service",
        "enable_external_access",
        "unpublish_service",
    } <= names
    read_only = {tool["name"] for tool in tools if tool["annotations"]["readOnlyHint"]}
    assert "deploy_model" not in read_only
    assert "publish_service" not in read_only
    assert "platform-secret" not in json.dumps(tools)


def test_deploy_model_sends_parameters_and_omits_empty_fields() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "d-1", "status": "queued"})

    mcp = build(handler)
    result = mcp.call_tool(
        "deploy_model",
        {
            "model_id": "Qwen/Qwen3.8-27B",
            "port": 8000,
            "local_model_path": "/models/Qwen",
            "gpu_memory_utilization": 0.8,
            "display_name": "Qwen demo",
        },
    )
    assert result["isError"] is False
    assert captured["url"] == "http://10.0.0.5:18180/api/v1/deployments"
    assert captured["payload"]["model_id"] == "Qwen/Qwen3.8-27B"
    assert captured["payload"]["display_name"] == "Qwen demo"
    assert captured["payload"]["parameters"] == {
        "port": 8000,
        "local_model_path": "/models/Qwen",
        "gpu_memory_utilization": 0.8,
    }


def test_publish_service_returns_key_and_targets_deployment_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/service-publications/deployment/d-1"
        return httpx.Response(
            201,
            json={"base_url": "http://10.21.212.6:18181/p/v1", "api_key": "sk-service"},
        )

    result = build(handler).call_tool("publish_service", {"target_id": "d-1"})
    assert body(result)["api_key"] == "sk-service"


def test_platform_errors_are_reported_without_leaking_the_platform_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "service is not running"})

    result = build(handler).call_tool(
        "publish_service", {"target_id": "d-1", "target_type": "deployment"}
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "service is not running" in text
    assert "platform-secret" not in text


def test_rejected_api_key_is_explained() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid api key"})

    result = build(handler).call_tool("platform_status", {})
    assert result["isError"] is True
    assert "API key" in result["content"][0]["text"]


def test_wait_for_deployment_returns_terminal_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "d-1", "status": "running"})

    result = build(handler).call_tool(
        "wait_for_deployment", {"deployment_id": "d-1", "interval_seconds": 1}
    )
    payload = body(result)
    assert payload["status"] == "running"
    assert "waited_seconds" in payload


def test_platform_url_validation_rejects_credentials_and_query() -> None:
    for url in ("ftp://host", "http://user:pass@host", "http://host/?token=1", "host:18180"):
        with pytest.raises(ValueError):
            HarnessMCP(url, "key")


def test_unknown_tool_is_rejected() -> None:
    mcp = build(lambda request: httpx.Response(200, json={}))
    response = mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert response["result"]["isError"] is True
