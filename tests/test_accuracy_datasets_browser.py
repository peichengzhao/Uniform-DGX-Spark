from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import accuracy_site as accuracy_site
from test_accuracy_browser import browser as browser


def open_catalog(browser: Browser, url: str) -> None:
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelectorAll('.accuracy-catalog-card').length===12")
    browser.wait("!document.querySelector('[data-dataset-action=\"manual\"]').disabled")
    target = browser.evaluate(
        "document.querySelector('#accuracy-target option[value]:not([value=\"\"])').value"
    )
    browser.fill("#accuracy-target", target)


@pytest.mark.parametrize("mobile", [False, True])
def test_dataset_cards_search_filter_detail_and_safe_configuration(
    browser: Browser, accuracy_site: tuple[TestClient, str], mobile: bool
) -> None:
    client, url = accuracy_site
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
    open_catalog(browser, url)
    assert "275 项" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-source').textContent"
    )
    browser.click("#accuracy-dataset-next")
    assert (
        browser.evaluate("document.querySelector('#accuracy-dataset-page').textContent") == "2 / 23"
    )
    browser.fill("#accuracy-dataset-search", "mmlu")
    assert "MMLU" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-list').textContent"
    )
    browser.fill("#accuracy-dataset-search", "")
    browser.fill("#accuracy-dataset-category", "vlm")
    assert "77 项匹配" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-count').textContent"
    )
    browser.fill("#accuracy-dataset-category", "all")
    browser.fill("#accuracy-dataset-tag", "Math")
    browser.fill("#accuracy-dataset-status", "integrated")
    assert browser.evaluate("document.querySelectorAll('.accuracy-catalog-card').length") == 1
    browser.fill("#accuracy-dataset-tag", "all")
    assert browser.evaluate("document.querySelectorAll('.accuracy-catalog-card').length") == 3
    browser.click('[data-dataset="ceval"][data-dataset-action="detail"]')
    browser.wait("document.querySelector('#accuracy-dataset-dialog').open")
    assert "52" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-detail').textContent"
    )
    assert "computer_network" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-detail').textContent"
    )
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    browser.click('#accuracy-dataset-dialog [data-dataset-action="manual"]')
    browser.wait("document.querySelector('#accuracy-config-dialog').open")
    assert browser.evaluate("document.querySelector('[name=dataset_ceval]').checked")
    assert not browser.evaluate("document.querySelector('[name=dataset_gsm8k]').checked")
    assert not browser.evaluate("document.querySelector('[name=dataset_arc]').checked")
    assert (
        browser.evaluate("document.querySelector('[name=ceval_subsets]').value")
        == "computer_network"
    )
    assert browser.evaluate("document.querySelector('[name=ceval_limit]').value") == "10"
    browser.click("#accuracy-config-dialog [data-accuracy-config-close]")
    browser.click('[data-dataset="arc"][data-dataset-action="ai"]')
    browser.wait("document.querySelector('#ai-accuracy-dialog').open")
    intent = browser.evaluate("document.querySelector('#ai-accuracy-form [name=intent]').value")
    assert "ARC-Challenge" in intent and "10 条" in intent
    assert (
        browser.evaluate("document.querySelector('#ai-accuracy-form [name=sample_budget]').value")
        == "12"
    )
    browser.click("#ai-accuracy-dialog [data-ai-accuracy-close]")
    browser.fill("#accuracy-dataset-status", "pending")
    browser.fill("#accuracy-dataset-search", "live_code_bench")
    assert (
        browser.evaluate(
            "document.querySelectorAll('#accuracy-dataset-list "
            '[data-dataset-action="ai"]\').length'
        )
        == 0
    )
    browser.click('[data-dataset-action="detail"]')
    browser.wait("document.querySelector('#accuracy-dataset-dialog').open")
    assert "不能在当前平台执行" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-detail').textContent"
    )
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    assert client.app.state.accuracy_store.list()[1] == 0
    assert client.app.state.ai_store.list()[1] == 0


def test_dataset_catalog_untrusted_metadata_is_not_html(
    browser: Browser, accuracy_site: tuple[TestClient, str]
) -> None:
    client, url = accuracy_site
    payload = client.get("/static/accuracy-datasets.json").json()
    item = payload["items"][0]
    item["pretty_name"] = "<img src=x onerror=window.__catalogInjected=true>"
    item["description"] = "<script>window.__catalogInjected=true</script>"
    item["summary"] = "<img src=x onerror=window.__catalogInjected=true>"
    browser.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "const originalFetch=window.fetch;window.fetch=(url,...args)=>"
            "String(url).startsWith('/static/accuracy-datasets.json')?"
            f"Promise.resolve(new Response(JSON.stringify({json.dumps(payload)}),{{status:200}})):"
            "originalFetch(url,...args);"
        },
    )
    open_catalog(browser, url)
    browser.fill("#accuracy-dataset-search", item["name"])
    browser.click('[data-dataset-action="detail"]')
    assert "<script>" in browser.evaluate(
        "document.querySelector('#accuracy-dataset-detail').textContent"
    )
    assert not browser.evaluate("document.querySelector('#accuracy-dataset-detail script')")
    assert browser.evaluate("window.__catalogInjected||false") is False


def test_catalog_remains_browsable_but_actions_disable_in_read_only_mode(
    browser: Browser, accuracy_site: tuple[TestClient, str]
) -> None:
    _client, url = accuracy_site
    browser.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "const originalFetch=window.fetch;window.fetch=async(url,...args)=>{"
            "const response=await originalFetch(url,...args);"
            "if(String(url).startsWith('/api/v1/accuracy/runs?')){"
            "const data=await response.clone().json();data.creation_enabled=false;"
            "return new Response(JSON.stringify(data),{status:200});}return response;};"
        },
    )
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelectorAll('.accuracy-catalog-card').length===12")
    browser.wait("document.querySelector('#accuracy-unavailable').textContent.includes('只读')")
    browser.fill("#accuracy-dataset-status", "integrated")
    assert browser.evaluate(
        '[...document.querySelectorAll(\'[data-dataset-action="ai"],[data-dataset-action="manual"]\')].every(b=>b.disabled)'
    )
    browser.click('[data-dataset-action="detail"]')
    assert browser.evaluate("document.querySelector('#accuracy-dataset-dialog').open")
