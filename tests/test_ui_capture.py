from __future__ import annotations

import subprocess
from pathlib import Path

from uniform_dgxspark.schemas import UIPageCaptureCreate
from uniform_dgxspark.services.ui_capture import UIPageCaptureService


def capture_request() -> UIPageCaptureCreate:
    return UIPageCaptureCreate(
        html=(
            '<html><head><base href="https://untrusted.invalid/"></head>'
            '<body><script>alert("no")</script><main '
            'data-uniform-capture-scroll-top="420">current UI</main></body></html>'
        ),
        viewport_width=1280,
        viewport_height=720,
        device_pixel_ratio=1.5,
        scroll_x=12,
        scroll_y=345,
    )


def test_capture_document_is_script_isolated_and_scrolls_to_viewport() -> None:
    document = UIPageCaptureService._capture_document(
        capture_request(), base_url="http://127.0.0.1:18180"
    )

    assert 'base href="http://127.0.0.1:18180/"' in document
    assert "untrusted.invalid" not in document
    assert 'alert("no")' not in document
    assert "left:-12px" in document
    assert "top:-345px" in document
    assert "uniformCaptureScrollTop" in document
    assert "script-src 'nonce-" in document
    assert "current UI" in document


def test_capture_invokes_chromium_and_returns_png(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        output_argument = next(part for part in command if part.startswith("--screenshot="))
        output = Path(output_argument.split("=", 1)[1])
        output.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("uniform_dgxspark.services.ui_capture.subprocess.run", fake_run)
    service = UIPageCaptureService(browser_binary="/usr/bin/chromium", work_dir=tmp_path)

    image = service.capture(capture_request(), base_url="http://127.0.0.1:18180")

    assert image == b"\x89PNG\r\n\x1a\nrendered"
