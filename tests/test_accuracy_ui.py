from __future__ import annotations

import re

from fastapi.testclient import TestClient


def test_accuracy_workspace_exposes_complete_user_flow(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200
    html = index.text

    assert 'data-view="accuracy">精度评测</button>' in html
    for element_id in (
        "view-accuracy",
        "accuracy-target",
        "accuracy-run-list",
        "accuracy-config-dialog",
        "accuracy-form",
        "accuracy-preset",
        "accuracy-detail-dialog",
        "accuracy-detail-content",
    ):
        assert f'id="{element_id}"' in html

    assert set(re.findall(r'value="([a-z]+-objective-v1)"', html)) == {
        "quick-objective-v1",
        "balanced-objective-v1",
        "formal-objective-v1",
        "custom-objective-v1",
    }
    assert 'name="dataset_gsm8k"' in html
    assert 'name="dataset_arc"' in html
    assert 'name="dataset_ceval"' in html
    assert "GSM8K" in html and "ARC" in html and "C-Eval" in html


def test_accuracy_ui_uses_authenticated_api_and_safe_artifact_download(
    client: TestClient,
) -> None:
    response = client.get("/static/app.js")
    assert response.status_code == 200
    script = response.text

    for fragment in (
        'api("/api/v1/accuracy/runs?limit=100")',
        'api("/api/v1/accuracy/runs",{method:"POST"',
        '/cancel`,{method:"POST"}',
        "/logs?tail=2000",
        "/artifacts`",
        "apiBlob(accuracyArtifactUrl(id,path))",
        "accuracyActiveStatuses",
        "renderAccuracyRuns",
        "accuracyMetricTable",
        "openAccuracyDetail",
    ):
        assert fragment in script

    assert "run_directory" not in script
    assert 'class="secondary accuracy-service"' in script
    assert 'sessionStorage.getItem("uniform_api_token")' in script

    stylesheet = client.get("/static/app.css")
    assert stylesheet.status_code == 200
    assert ".accuracy-run-card" in stylesheet.text
    assert ".accuracy-metric-table" in stylesheet.text
    assert ".accuracy-detail-dialog" in stylesheet.text


def test_accuracy_static_assets_share_cache_busting_version(client: TestClient) -> None:
    html = client.get("/").text
    css_version = re.search(r'app\.css\?v=([^"\s]+)', html)
    js_version = re.search(r'app\.js\?v=([^"\s]+)', html)

    assert css_version is not None
    assert js_version is not None
    assert css_version.group(1) == js_version.group(1) == "20261103-gateway-copy"
