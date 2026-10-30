from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import browser as browser
from test_dataset_integration import integration_site as integration_site
from test_feishu_notifications import MockFeishu, sample

from uniform_dgxspark.feishu_models import CALLBACK_PATH
from uniform_dgxspark.services.feishu_notifications import TOKEN_URL


@pytest.mark.parametrize("mobile", [False, True])
def test_feishu_configuration_official_binding_test_opt_in_alert_and_unbind(
    browser: Browser,
    integration_site: tuple[TestClient, str, str],
    tmp_path: Path,
    mobile: bool,
) -> None:
    client, url, _ = integration_site
    mock = MockFeishu()
    service = client.app.state.feishu_notifications
    service._client.close()
    service._client = httpx.Client(transport=httpx.MockTransport(mock))
    if mobile:
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
    open_site(browser, url)
    browser.click('[data-view="system"]')
    browser.wait("document.querySelectorAll('[data-feishu-rule]').length===7")
    assert browser.evaluate("document.querySelector('#feishu-bind').disabled")
    assert browser.evaluate("document.querySelector('#feishu-form [name=enabled]').disabled")
    browser.click("#feishu-setup-guide summary")
    assert "自建应用" in browser.evaluate(
        "document.querySelector('#feishu-setup-guide').textContent"
    )
    browser.fill("#feishu-form [name=app_id]", "cli_test")
    browser.fill("#feishu-form [name=app_secret]", "private-secret-do-not-echo")
    browser.fill("#feishu-form [name=redirect_uri]", url + CALLBACK_PATH)
    browser.click("#feishu-save")
    browser.wait("!document.querySelector('#feishu-bind').disabled")
    assert browser.evaluate("document.querySelector('#feishu-form [name=app_secret]').value") == ""
    assert not mock.messages()
    browser.evaluate(
        "window.open=()=>window.__feishuPopup={location:{href:''},close(){}};window.confirm=()=>true;"
    )
    browser.click("#feishu-bind")
    browser.wait("window.__feishuPopup?.location.href.startsWith('https://accounts.feishu.cn/')")
    authorization_url = browser.evaluate("window.__feishuPopup.location.href")
    assert urlsplit(authorization_url).path == "/oauth/v3/authorize"
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    response = browser.evaluate(
        f"fetch('{url}{CALLBACK_PATH}?state={state}&code=mock-code').then(r=>r.status)"
    )
    assert response == 200
    browser.wait("document.querySelector('#feishu-recipient').textContent.includes('测试接收人')")
    assert browser.evaluate("window.__feishuInjected||false") is False
    assert not mock.messages()
    browser.click("#feishu-test")
    browser.wait("!document.querySelector('#feishu-form [name=enabled]').disabled")
    assert len(mock.messages()) == 1
    browser.click("#feishu-form [name=enabled]")
    browser.click("#feishu-save")
    browser.wait("document.querySelector('#feishu-status').textContent==='告警已开启'")
    # A dirty rule is not replaced by the periodic status refresh.
    browser.fill('[data-feishu-rule="memory"] [data-rule-threshold]', "92")
    browser.evaluate("new Promise(resolve=>setTimeout(resolve,3300))")
    assert (
        browser.evaluate(
            "document.querySelector('[data-feishu-rule=memory] [data-rule-threshold]').value"
        )
        == "92"
    )
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    browser.evaluate("document.querySelector('#feishu-heading').scrollIntoView({block:'start'})")
    browser.screenshot(tmp_path / ("feishu-mobile.png" if mobile else "feishu-desktop.png"))
    browser.click("#feishu-save")
    browser.wait("!document.querySelector('#feishu-test').disabled")
    # The alert engine runs without using the page or starting a model task.
    import time

    stamp = time.time()
    for seconds in range(0, 61, 10):
        service.tick(now=stamp + seconds, local=sample(stamp + seconds), remote=[])
    assert any(item["kind"] == "alert" for item in service.events()["items"])
    assert len(mock.messages()) == 2
    browser.click("#feishu-unbind")
    browser.wait("document.querySelector('#feishu-recipient').textContent.includes('尚未绑定')")
    assert service.status()["config"]["enabled"] is False
    assert len(service.events()["items"]) == 2
    assert client.app.state.accuracy_store.list()[1] == 0


@pytest.mark.parametrize("mobile", [False, True])
def test_binding_failure_exposes_only_safe_diagnostic_and_requires_new_authorization(
    browser: Browser,
    integration_site: tuple[TestClient, str, str],
    mobile: bool,
) -> None:
    client, url, _ = integration_site
    service = client.app.state.feishu_notifications
    requests = []

    def reject(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == TOKEN_URL
        return httpx.Response(
            400,
            json={
                "code": 20049,
                "error_description": "private-secret-do-not-echo private-authorization-code",
            },
        )

    service._client.close()
    service._client = httpx.Client(transport=httpx.MockTransport(reject))
    if mobile:
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 390,
                "height": 844,
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
    open_site(browser, url)
    browser.click('[data-view="system"]')
    browser.wait("document.querySelectorAll('[data-feishu-rule]').length===7")
    browser.fill("#feishu-form [name=app_id]", "cli_test")
    browser.fill("#feishu-form [name=app_secret]", "private-secret-do-not-echo")
    browser.fill("#feishu-form [name=redirect_uri]", url + CALLBACK_PATH)
    browser.click("#feishu-save")
    browser.wait("!document.querySelector('#feishu-bind').disabled")
    browser.evaluate("window.open=()=>window.__feishuPopup={location:{href:''},close(){}}")
    browser.click("#feishu-bind")
    browser.wait("window.__feishuPopup?.location.href.includes('/oauth/v3/authorize?')")
    authorization_url = browser.evaluate("window.__feishuPopup.location.href")
    state = parse_qs(urlsplit(authorization_url).query)["state"][0]
    result = browser.evaluate(
        f"fetch('{url}{CALLBACK_PATH}?state={state}&code=private-authorization-code')"
        ".then(async r=>({status:r.status,text:await r.text()}))"
    )
    assert result["status"] == 400
    assert "Uniform 平台回调页" in result["text"] and "20049" in result["text"]
    assert "诊断编号" in result["text"]
    browser.wait("document.querySelector('#feishu-binding-summary').textContent.includes('20049')")
    browser.click("#feishu-binding-summary")
    details = browser.evaluate("document.querySelector('#feishu-binding-details').textContent")
    assert "本地 PKCE 前后匹配：通过" in details
    assert "不记录密钥" in details
    for sensitive in ("private-secret-do-not-echo", "private-authorization-code", state):
        assert sensitive not in details + result["text"]
    assert browser.evaluate("document.querySelector('#feishu-form [name=enabled]').disabled")
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    assert len(requests) == 1
    # Failed codes are not automatically retried; the user must start a fresh authorization.
    browser.click("#feishu-bind")
    browser.wait("!document.querySelector('#feishu-binding-summary').textContent.includes('20049')")
    fresh = browser.evaluate("window.__feishuPopup.location.href")
    assert fresh != authorization_url
    assert len(requests) == 1 and service.status()["last_binding"]["result"] == "pending"


def test_feishu_form_rejects_invalid_threshold_without_exposing_secret(
    browser: Browser,
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, url, _ = integration_site
    open_site(browser, url)
    browser.click('[data-view="system"]')
    browser.wait("document.querySelectorAll('[data-feishu-rule]').length===7")
    browser.fill("#feishu-form [name=app_secret]", "private-secret-do-not-echo")
    browser.fill('[data-feishu-rule="memory"] [data-rule-recovery]', "95")
    browser.click("#feishu-save")
    browser.wait("document.querySelector('#feishu-error').textContent.length>0")
    assert "private-secret" not in browser.evaluate(
        "document.querySelector('#feishu-error').textContent"
    )
    assert not client.app.state.feishu_notifications.path.exists()
