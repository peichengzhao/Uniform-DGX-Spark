from __future__ import annotations

import json

import httpx
import pytest

from uniform_dgxspark import harness_mcp
from uniform_dgxspark.harness_mcp import HarnessMCP, tool_definitions


@pytest.fixture(autouse=True)
def reset_console_state(monkeypatch):
    monkeypatch.setattr(harness_mcp, "CONSOLE_OPENED", False)
    monkeypatch.delenv("UNIFORM_AUTO_OPEN_UI", raising=False)
    monkeypatch.delenv("UNIFORM_UI_URL", raising=False)


def record_console(monkeypatch) -> list:
    opened: list = []
    monkeypatch.setattr(
        harness_mcp, "open_console", lambda url, reason: (opened.append((url, reason)), True)[1]
    )
    return opened


def tool_call(name: str) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}}


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
    with pytest.raises(ValueError):
        HarnessMCP("http://host:18180", "key", fallback_url="ftp://fallback")


def test_fallback_url_is_used_when_primary_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary.internal":
            raise httpx.ConnectError("primary offline", request=request)
        assert request.url.host == "uniform.example.com"
        return httpx.Response(200, json={"status": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    mcp = HarnessMCP(
        "http://primary.internal:18180",
        "platform-secret",
        fallback_url="https://uniform.example.com",
        client=client,
    )

    status = body(mcp.call_tool("platform_status", {}))
    ui = body(mcp.call_tool("open_platform_ui", {}))

    assert status == {"status": "ok", "platform_url": "https://uniform.example.com"}
    assert ui["url"] == "https://uniform.example.com/"


def test_unknown_tool_is_rejected() -> None:
    mcp = build(lambda request: httpx.Response(200, json={}))
    response = mcp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nope"}}
    )
    assert response["result"]["isError"] is True


def test_console_opens_on_the_first_tool_call_only(monkeypatch) -> None:
    opened = record_console(monkeypatch)
    mcp = HarnessMCP("http://10.0.0.5:18180", "platform-secret")

    harness_mcp.open_console_for_request(mcp, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert opened == []

    harness_mcp.open_console_for_request(mcp, tool_call("platform_status"))
    harness_mcp.open_console_for_request(mcp, tool_call("list_deployments"))
    assert opened == [("http://10.0.0.5:18180/", "first tool call: platform_status")]


def test_open_platform_ui_reopens_the_console_every_time(monkeypatch) -> None:
    opened = record_console(monkeypatch)
    mcp = HarnessMCP("http://10.0.0.5:18180", "platform-secret")

    harness_mcp.open_console_for_request(mcp, tool_call("platform_status"))
    harness_mcp.open_console_for_request(mcp, tool_call("open_platform_ui"))
    harness_mcp.open_console_for_request(mcp, tool_call("open_platform_ui"))

    assert [reason for _, reason in opened] == [
        "first tool call: platform_status",
        "open_platform_ui",
        "open_platform_ui",
    ]


def test_auto_open_can_be_disabled_without_losing_the_explicit_tool(monkeypatch) -> None:
    opened = record_console(monkeypatch)
    monkeypatch.setenv("UNIFORM_AUTO_OPEN_UI", "0")
    mcp = HarnessMCP("http://10.0.0.5:18180", "platform-secret")

    harness_mcp.open_console_for_request(mcp, tool_call("platform_status"))
    harness_mcp.open_console_for_request(mcp, tool_call("deploy_model"))
    assert opened == []

    harness_mcp.open_console_for_request(mcp, tool_call("open_platform_ui"))
    assert opened == [("http://10.0.0.5:18180/", "open_platform_ui")]


def test_console_url_can_target_another_entry_point(monkeypatch) -> None:
    opened = record_console(monkeypatch)
    monkeypatch.setenv("UNIFORM_UI_URL", "https://uniform.example.com/ui")
    mcp = HarnessMCP("http://10.0.0.5:18180", "platform-secret")

    harness_mcp.open_console_for_request(mcp, tool_call("platform_status"))

    assert opened == [("https://uniform.example.com/ui", "first tool call: platform_status")]


def test_console_hook_ignores_malformed_requests(monkeypatch) -> None:
    opened = record_console(monkeypatch)
    mcp = HarnessMCP("http://10.0.0.5:18180", "platform-secret")

    for request in (None, {}, "not-a-dict", {"method": "tools/call"}, tool_call("")):
        harness_mcp.open_console_for_request(mcp, request)

    assert opened == []


def test_console_launch_failures_are_reported_but_never_raised(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("no desktop session")

    monkeypatch.setattr(harness_mcp.webbrowser, "open", explode)
    assert harness_mcp.open_console("http://10.0.0.5:18180/", "test") is False

    monkeypatch.setattr(harness_mcp.webbrowser, "open", lambda url, new=0: False)
    assert harness_mcp.open_console("http://10.0.0.5:18180/", "test") is False

    monkeypatch.setattr(harness_mcp.webbrowser, "open", lambda url, new=0: True)
    assert harness_mcp.open_console("http://10.0.0.5:18180/", "test") is True
