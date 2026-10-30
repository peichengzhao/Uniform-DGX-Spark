from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import DeploymentParameters


def test_api_key_protects_api_but_not_web_ui(tmp_path: Path) -> None:
    app = create_app(
        Settings(data_dir=tmp_path, worker_enabled=False, api_token="secret-token")
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/v1/health").status_code == 401
        assert (
            client.get(
                "/api/v1/health", headers={"X-API-Key": "secret-token"}
            ).status_code
            == 200
        )

        class StubIcons:
            def icon(self, publisher: str, model_id: str | None) -> tuple[bytes, str]:
                del publisher, model_id
                return b"<svg/>", "image/svg+xml"

        app.state.publisher_icons = StubIcons()
        icon = client.get("/api/v1/catalog/publishers/Qwen/icon")
        assert icon.status_code == 200
        assert icon.headers["content-type"] == "image/svg+xml"


def test_remote_code_requires_explicit_acknowledgement() -> None:
    with pytest.raises(ValidationError, match="remote_code_acknowledged"):
        DeploymentParameters(trust_remote_code=True)


def test_platform_owned_vllm_flags_are_rejected() -> None:
    with pytest.raises(ValidationError, match="managed by the platform"):
        DeploymentParameters(extra_args=["--port 9999"])
