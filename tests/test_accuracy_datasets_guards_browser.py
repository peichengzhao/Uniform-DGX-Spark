from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import accuracy_site as accuracy_site
from test_accuracy_browser import browser as browser


@pytest.mark.parametrize("mode", ["busy", "disabled"])
def test_dataset_actions_respect_active_tasks_and_disabled_feature(
    browser: Browser, accuracy_site: tuple[TestClient, str], mode: str
) -> None:
    client, url = accuracy_site
    if mode == "busy":
        replacement = {
            "items": [
                {
                    "id": "test-only-running",
                    "status": "running",
                    "request": {},
                    "created_at": "2026-10-27T00:00:00Z",
                    "model_name": "test-model",
                    "target_label": "隔离测试目标",
                }
            ],
            "creation_enabled": True,
        }
        response = f"new Response(JSON.stringify({json.dumps(replacement)}),{{status:200}})"
    else:
        response = "new Response(JSON.stringify({detail:'disabled'}),{status:403})"
    browser.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "const originalFetch=window.fetch;window.fetch=(url,...args)=>"
            "String(url).startsWith('/api/v1/accuracy/runs?')?"
            f"Promise.resolve({response}):originalFetch(url,...args);"
        },
    )
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelectorAll('.accuracy-catalog-card').length===12")
    browser.wait("document.querySelector('#open-accuracy-config').disabled")
    browser.fill("#accuracy-dataset-status", "integrated")
    assert browser.evaluate(
        "[...document.querySelectorAll('#accuracy-dataset-list "
        '[data-dataset-action="ai"],#accuracy-dataset-list '
        '[data-dataset-action="manual"]\')].every(button=>button.disabled)'
    )
    browser.click('[data-dataset-action="detail"]')
    assert browser.evaluate("document.querySelector('#accuracy-dataset-dialog').open")
    assert client.app.state.accuracy_store.list()[1] == 0


def test_catalog_load_failure_retains_manual_entry_and_can_retry(
    browser: Browser, accuracy_site: tuple[TestClient, str]
) -> None:
    client, url = accuracy_site
    browser.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "let catalogFailed=false;const originalFetch=window.fetch;"
            "window.fetch=(url,...args)=>{"
            "if(String(url).startsWith('/static/accuracy-datasets.json')&&!catalogFailed){"
            "catalogFailed=true;return Promise.resolve(new Response('{}',{status:503}));}"
            "return originalFetch(url,...args);};"
        },
    )
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelector('[data-dataset-retry]')")
    browser.wait("!document.querySelector('#open-accuracy-config').disabled")
    target = browser.evaluate(
        "document.querySelector('#accuracy-target option[value]:not([value=\"\"])').value"
    )
    browser.fill("#accuracy-target", target)
    browser.click("#open-accuracy-config")
    browser.wait("document.querySelector('#accuracy-config-dialog').open")
    browser.click("#accuracy-config-dialog [data-accuracy-config-close]")
    browser.click("[data-dataset-retry]")
    browser.wait("document.querySelectorAll('.accuracy-catalog-card').length===12")
    assert client.app.state.accuracy_store.list()[1] == 0
