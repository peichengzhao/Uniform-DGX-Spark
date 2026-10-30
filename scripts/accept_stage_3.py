from pathlib import Path
import tempfile

from uniform_dgxspark.services.downloads import HuggingFaceDownloadManager


with tempfile.TemporaryDirectory(prefix="uniform-dgxspark-stage3-", dir="/tmp") as directory:
    manager = HuggingFaceDownloadManager(Path(directory), max_workers=4)
    first = manager.download("yujiepan/qwen2.5-tiny-random", "0354728bb14944308c3134cf48d29302030a72c0")
    second = manager.download("yujiepan/qwen2.5-tiny-random", "0354728bb14944308c3134cf48d29302030a72c0")
    assert first.cached is False
    assert second.cached is True
    assert (first.path / ".uniform-download-complete.json").is_file()
    assert (first.path / "config.json").is_file()
    assert any(first.path.glob("*.safetensors"))
    print(f"downloaded_bytes={first.bytes_on_disk} revision={first.revision}")

