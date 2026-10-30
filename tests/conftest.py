from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from uniform_dgxspark.config import Settings
from uniform_dgxspark.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path, worker_enabled=False))
    with TestClient(app) as test_client:
        # Unit tests must not adopt labeled containers from the developer's real Docker.
        test_client.app.state.runtime.discover_ai_services = lambda: []
        yield test_client
