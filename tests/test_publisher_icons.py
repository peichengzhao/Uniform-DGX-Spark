from __future__ import annotations

import json
import urllib.error
from email.message import Message

from uniform_dgxspark.services.publisher_icons import PublisherIconService


class FakeResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self.body[:amount]


class FakeOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def open(self, request: object, timeout: int) -> FakeResponse:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        self.urls.append(url)
        if "/api/organizations/" in url:
            return FakeResponse(
                json.dumps(
                    {"avatarUrl": "https://cdn-avatars.huggingface.co/qwen.png"}
                ).encode(),
                "application/json",
            )
        return FakeResponse(b"png-image", "image/png")


def test_publisher_icon_prefers_organization_avatar_and_caches_it() -> None:
    service = PublisherIconService()
    opener = FakeOpener()
    service.opener = opener  # type: ignore[assignment]

    first = service.icon("Qwen", "Qwen/Qwen3-8B")
    second = service.icon("qwen", "Qwen/Qwen3-8B")

    assert first == (b"png-image", "image/png")
    assert second == first
    assert len(opener.urls) == 2


def test_invalid_publisher_uses_safe_svg_fallback() -> None:
    content, media_type = PublisherIconService().icon("bad/name", None)

    assert media_type == "image/svg+xml"
    assert b"<svg" in content


class ModelScopeOpener:
    def open(self, request: object, timeout: int) -> FakeResponse:
        del timeout
        url = request.full_url  # type: ignore[attr-defined]
        if "huggingface.co" in url:
            raise urllib.error.URLError("offline")
        if "/api/v1/models/" in url:
            return FakeResponse(
                json.dumps(
                    {
                        "Data": {
                            "Organization": {
                                "Avatar": (
                                    "https://resources.modelscope.cn/avatar/qwen.jpg"
                                )
                            }
                        }
                    }
                ).encode(),
                "application/json",
            )
        return FakeResponse(b"modelscope-avatar", "image/jpeg")


def test_publisher_icon_uses_modelscope_organization_avatar_as_fallback() -> None:
    service = PublisherIconService()
    service.opener = ModelScopeOpener()  # type: ignore[assignment]

    content, media_type = service.icon("Qwen", "Qwen/Qwen3-8B")

    assert content == b"modelscope-avatar"
    assert media_type == "image/jpeg"
