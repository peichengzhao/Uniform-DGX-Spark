from __future__ import annotations

import html
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..schemas import UIPageCaptureCreate


class UIPageCaptureUnavailable(RuntimeError):
    pass


class UIPageCaptureError(RuntimeError):
    pass


class UIPageCaptureService:
    """Render a browser DOM snapshot with Chromium and return real page pixels."""

    _script_pattern = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
    _base_pattern = re.compile(r"<base\b[^>]*>", re.IGNORECASE)

    def __init__(
        self,
        browser_binary: str | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.browser_binary = browser_binary or self._find_browser()
        self.work_dir = (work_dir or Path.cwd()).resolve()
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _find_browser() -> str | None:
        for candidate in ("chromium", "chromium-browser", "google-chrome"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    def capture(self, payload: UIPageCaptureCreate, *, base_url: str) -> bytes:
        if not self.browser_binary:
            raise UIPageCaptureUnavailable("本机未安装 Chromium，已无法执行原生页面截图")

        document = self._capture_document(payload, base_url=base_url)
        # Snap Chromium has a private /tmp mount. Keep transient files below the
        # application data directory so both processes see the same paths.
        with tempfile.TemporaryDirectory(prefix="ui-capture-", dir=self.work_dir) as temporary:
            directory = Path(temporary)
            source = directory / "snapshot.html"
            output = directory / "screenshot.png"
            source.write_text(document, encoding="utf-8")
            command = [
                self.browser_binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--hide-scrollbars",
                "--metrics-recording-only",
                "--no-first-run",
                f"--window-size={payload.viewport_width},{payload.viewport_height}",
                f"--force-device-scale-factor={payload.device_pixel_ratio}",
                "--virtual-time-budget=1500",
                f"--screenshot={output}",
                source.as_uri(),
            ]
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise UIPageCaptureError(f"Chromium 截图启动失败：{error}") from error
            if result.returncode != 0 or not output.is_file():
                details = (result.stderr or result.stdout or "未知错误").strip()[-1_000:]
                raise UIPageCaptureError(f"Chromium 截图失败：{details}")
            image = output.read_bytes()
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise UIPageCaptureError("Chromium 返回了无效的截图文件")
            if len(image) > 16_000_000:
                raise UIPageCaptureError("页面截图超过 16 MB，请缩小浏览器窗口后重试")
            return image

    @classmethod
    def _capture_document(cls, payload: UIPageCaptureCreate, *, base_url: str) -> str:
        snapshot = cls._script_pattern.sub("", payload.html)
        snapshot = cls._base_pattern.sub("", snapshot)
        nonce = secrets.token_urlsafe(24)
        safe_base = html.escape(base_url.rstrip("/") + "/", quote=True)
        scroll_script = (
            "addEventListener('load',()=>requestAnimationFrame(()=>requestAnimationFrame(()=>{"
            "document.querySelectorAll('[data-uniform-capture-scroll-top]').forEach(el=>{"
            "el.scrollTop=Number(el.dataset.uniformCaptureScrollTop)||0;"
            "el.scrollLeft=Number(el.dataset.uniformCaptureScrollLeft)||0;});"
            "})));"
        )
        capture_head = (
            f'<base href="{safe_base}">'
            '<meta http-equiv="Content-Security-Policy" content="'
            "default-src 'none'; "
            f"style-src 'unsafe-inline' {safe_base}; "
            f"img-src data: blob: {safe_base}; font-src data: {safe_base}; "
            f"media-src data: blob: {safe_base}; script-src 'nonce-{nonce}'; "
            "connect-src 'none'; frame-src 'none'; object-src 'none'; form-action 'none'"
            '">'
            "<style>*{animation-play-state:paused!important;caret-color:transparent!important}"
            "html{scroll-behavior:auto!important;overflow:hidden!important}"
            f"body{{position:relative!important;left:-{payload.scroll_x}px!important;"
            f"top:-{payload.scroll_y}px!important}}</style>"
            f'<script nonce="{nonce}">{scroll_script}</script>'
        )
        head_match = re.search(r"<head\b[^>]*>", snapshot, re.IGNORECASE)
        if head_match:
            offset = head_match.end()
            snapshot = snapshot[:offset] + capture_head + snapshot[offset:]
        else:
            snapshot = f"<html><head>{capture_head}</head><body>{snapshot}</body></html>"
        return "<!doctype html>" + snapshot
