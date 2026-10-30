from pathlib import Path

from uniform_dgxspark.services.modelscope_downloads import ModelScopeDownloadManager


class FakeModelScopeApi:
    def __init__(self) -> None:
        self.calls = 0

    def download_repo(self, *args: object, **kwargs: object) -> Path:
        self.calls += 1
        target = Path(str(kwargs["local_dir"]))
        (target / "config.json").write_text("{}")
        (target / "model.safetensors").write_bytes(b"weights")
        return target


def test_modelscope_download_and_cache_marker(tmp_path: Path) -> None:
    api = FakeModelScopeApi()
    manager = ModelScopeDownloadManager(tmp_path, api=api)  # type: ignore[arg-type]

    first = manager.download("Qwen/Test", None)
    second = manager.download("Qwen/Test", "master")

    assert first.cached is False
    assert second.cached is True
    assert first.revision == "master"
    assert api.calls == 1
