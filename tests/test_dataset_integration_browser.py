from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_accuracy_browser import Browser, open_site
from test_accuracy_browser import browser as browser
from test_dataset_integration import integration_site as integration_site


def open_integration_card(browser: Browser, url: str, name: str) -> None:
    open_site(browser, url)
    browser.click('[data-view="accuracy"]')
    browser.wait("document.querySelectorAll('.accuracy-catalog-card').length===12")
    target = browser.evaluate(
        "document.querySelector('#accuracy-target option[value]:not([value=\"\"])').value"
    )
    browser.fill("#accuracy-target", target)
    browser.fill("#accuracy-dataset-search", name)
    selector = f'[data-dataset="{name}"][data-dataset-action="integrate"]'
    browser.wait(f"!document.querySelector('{selector}').disabled")
    browser.click(selector)
    browser.wait("document.querySelector('#dataset-integration-dialog').open")


@pytest.mark.parametrize("mobile", [False, True])
def test_one_click_onboarding_inline_codex_dialog_confirm_activate_and_evaluate(
    browser: Browser,
    integration_site: tuple[TestClient, str, str],
    tmp_path: Path,
    mobile: bool,
) -> None:
    client, url, _ = integration_site
    if mobile:
        browser.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": False},
        )
    open_integration_card(browser, url, "mmlu")
    assert client.app.state.ai_store.list()[1] == 0
    assert client.app.state.accuracy_store.list()[1] == 0
    assert "2 条" in browser.evaluate(
        "document.querySelector('#dataset-integration-note').textContent"
    )
    browser.click("#create-dataset-integration")
    browser.wait("!document.querySelector('#dataset-integration-dialog').open")
    browser.wait("document.querySelector('.confirm-dataset-integration')", timeout=20)
    browser.wait("!document.querySelector('#dataset-integration-terminal-panel').hidden")
    browser.wait("!document.querySelector('#dataset-integration-chat-view').hidden")
    assert browser.evaluate(
        "document.querySelector('#show-dataset-integration-chat').classList.contains('active')"
    )
    browser.wait("!document.querySelector('#dataset-integration-chat-input').disabled")
    browser.wait(
        "document.querySelector('#dataset-integration-chat-messages')."
        "textContent.includes('请检查 mmlu')"
    )
    assert browser.evaluate("document.querySelector('#view-accuracy').classList.contains('active')")
    assert browser.evaluate("window.__integrationInjected||false") is False
    assert client.app.state.accuracy_store.list()[1] == 0
    session = client.app.state.ai_store.list(task_type="dataset_integration")[0][0].id
    browser.fill("#dataset-integration-chat-input", "解释一下当前接入方案")
    browser.click("#send-dataset-integration-chat-message")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        transcript, _ = client.app.state.ai_terminal.transcript(session)
        if "收到补充需求".encode() in transcript:
            break
        time.sleep(0.05)
    assert "收到补充需求".encode() in transcript
    browser.click("#show-dataset-integration-terminal")
    browser.wait("!document.querySelector('#dataset-integration-terminal').hidden")
    browser.wait("!document.querySelector('#send-dataset-integration-message').disabled")
    assert browser.evaluate(
        "document.querySelector('#show-dataset-integration-terminal').classList.contains('active')"
    )
    browser.click("#show-dataset-integration-chat")
    browser.wait("!document.querySelector('#dataset-integration-chat-view').hidden")
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    browser.screenshot(tmp_path / ("onboarding-mobile.png" if mobile else "onboarding-desktop.png"))
    browser.click(".confirm-dataset-integration")
    browser.wait(
        'document.querySelector(\'[data-dataset="mmlu"][data-dataset-action="manual"]\')',
        timeout=20,
    )
    assert "已验证接入" in browser.evaluate(
        "document.querySelector('#dataset-integration-list').textContent"
    )
    assert client.app.state.accuracy_store.list()[1] == 1
    browser.click('[data-dataset="mmlu"][data-dataset-action="manual"]')
    browser.wait("document.querySelector('#accuracy-config-dialog').open")
    assert browser.evaluate(
        "document.querySelector('[data-extra-accuracy=\"mmlu\"] [data-extra-enabled]').checked"
    )
    assert not browser.evaluate("document.querySelector('[name=dataset_gsm8k]').checked")
    assert (
        browser.evaluate(
            "document.querySelector('[data-extra-accuracy=\"mmlu\"] [data-extra-limit]').value"
        )
        == "10"
    )
    assert browser.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    browser.click("#accuracy-config-dialog [data-accuracy-config-close]")
    browser.click('[data-dataset="mmlu"][data-dataset-action="ai"]')
    browser.wait("document.querySelector('#ai-accuracy-dialog').open")
    assert "mmlu" in browser.evaluate(
        "document.querySelector('#ai-accuracy-form [name=intent]').value"
    )
    assert client.app.state.ai_store.list()[1] == 1


def test_complex_dataset_uses_inline_dialog_without_fake_activation(
    browser: Browser,
    integration_site: tuple[TestClient, str, str],
) -> None:
    client, url, _ = integration_site
    open_integration_card(browser, url, "live_code_bench")
    assert "尚缺专用适配" in browser.evaluate(
        "document.querySelector('#dataset-integration-note').textContent"
    )
    browser.click("#create-dataset-integration")
    browser.wait(
        "document.querySelector('#dataset-integration-list').textContent.includes('专用适配')"
    )
    browser.wait("!document.querySelector('#dataset-integration-terminal-panel').hidden")
    assert not browser.evaluate("document.querySelector('.confirm-dataset-integration')")
    assert not browser.evaluate(
        'document.querySelector(\'[data-dataset="live_code_bench"][data-dataset-action="manual"]\')'
    )
    assert client.app.state.accuracy_store.list()[1] == 0
