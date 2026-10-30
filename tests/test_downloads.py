from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from uniform_dgxspark.services.downloads import HuggingFaceDownloadManager


class FakeApi:
    def model_info(self, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            sha="abc123",
            siblings=[SimpleNamespace(size=10), SimpleNamespace(size=20)],
        )


def test_download_writes_completion_marker_and_reuses_cache(tmp_path: Path) -> None:
    calls = 0

    def fake_download(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        target = Path(str(kwargs["local_dir"]))
        (target / "config.json").write_text("{}")
        (target / "model.safetensors").write_bytes(b"weights")
        return str(target)

    progress: list[float] = []
    manager = HuggingFaceDownloadManager(
        tmp_path,
        api=FakeApi(),  # type: ignore[arg-type]
        downloader=fake_download,
    )

    first = manager.download(
        "org/model", None, progress_callback=lambda value, current, total: progress.append(value)
    )
    second = manager.download("org/model", "abc123")

    assert first.revision == "abc123"
    assert first.cached is False
    assert second.cached is True
    assert first.path == second.path
    assert calls == 1
    assert progress[-1] == 1
    assert (first.path / ".uniform-download-complete.json").is_file()


def test_partial_directory_is_resumed_not_treated_as_complete(tmp_path: Path) -> None:
    calls = 0

    def fake_download(**kwargs: object) -> str:
        nonlocal calls
        calls += 1
        target = Path(str(kwargs["local_dir"]))
        assert (target / "partial.incomplete").is_file()
        (target / "config.json").write_text("{}")
        (target / "model.safetensors").write_bytes(b"weights")
        return str(target)

    manager = HuggingFaceDownloadManager(
        tmp_path,
        api=FakeApi(),  # type: ignore[arg-type]
        downloader=fake_download,
    )
    target = manager._target_path("org/model", "abc123")
    target.mkdir(parents=True)
    (target / "partial.incomplete").write_text("partial")

    result = manager.download("org/model", "abc123")

    assert result.cached is False
    assert calls == 1
