from __future__ import annotations

import html
import json
import threading
import urllib.error
import urllib.parse
import urllib.request


class PublisherIconService:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener()
        self._publisher_cache: dict[str, tuple[bytes, str]] = {}
        self._publisher_misses: set[str] = set()
        self._lock = threading.Lock()

    def icon(self, publisher: str, model_id: str | None) -> tuple[bytes, str]:
        normalized = publisher.strip()[:255]
        if not normalized or any(character in normalized for character in "/\\\x00"):
            return self._fallback("?"), "image/svg+xml"
        publisher_key = normalized.casefold()
        with self._lock:
            cached = self._publisher_cache.get(publisher_key)
            publisher_missed = publisher_key in self._publisher_misses
        if cached:
            return cached
        if not publisher_missed:
            resolved = self._huggingface_avatar(normalized)
            with self._lock:
                if resolved:
                    self._publisher_cache[publisher_key] = resolved
                else:
                    self._publisher_misses.add(publisher_key)
            if resolved:
                return resolved
        if model_id and self._valid_model_id(model_id):
            resolved_model = self._modelscope_publisher_avatar(model_id)
            if resolved_model:
                with self._lock:
                    self._publisher_cache[publisher_key] = resolved_model
                return resolved_model
        return self._fallback(normalized), "image/svg+xml"

    def _huggingface_avatar(self, publisher: str) -> tuple[bytes, str] | None:
        encoded = urllib.parse.quote(publisher, safe="")
        for kind in ("organizations", "users"):
            metadata = self._fetch(
                f"https://huggingface.co/api/{kind}/{encoded}/overview",
                max_bytes=128_000,
                accepted_types={"application/json"},
            )
            if metadata is None:
                continue
            try:
                avatar_url = str(json.loads(metadata[0]).get("avatarUrl") or "")
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            parsed = urllib.parse.urlparse(avatar_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not (
                    parsed.hostname == "cdn-avatars.huggingface.co"
                    or parsed.hostname.endswith(".huggingface.co")
                )
            ):
                continue
            image = self._fetch(
                avatar_url,
                max_bytes=1_500_000,
                accepted_types={"image/jpeg", "image/png", "image/webp", "image/gif"},
            )
            if image:
                return image
        return None

    def _modelscope_publisher_avatar(self, model_id: str) -> tuple[bytes, str] | None:
        encoded_path = "/".join(
            urllib.parse.quote(segment, safe="") for segment in model_id.split("/")
        )
        metadata = self._fetch(
            f"https://www.modelscope.cn/api/v1/models/{encoded_path}",
            max_bytes=512_000,
            accepted_types={"application/json"},
        )
        if metadata is None:
            return None
        try:
            payload = json.loads(metadata[0])
            organization = payload.get("Data", {}).get("Organization", {})
            avatar_url = str(organization.get("Avatar") or "")
        except (AttributeError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        parsed = urllib.parse.urlparse(avatar_url)
        allowed_hosts = {
            "resources.modelscope.cn",
            "resouces.modelscope.cn",
            "cdn.modelscope.cn",
        }
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            return None
        return self._fetch(
            avatar_url,
            max_bytes=1_500_000,
            accepted_types={"image/png", "image/jpeg", "image/webp", "image/gif"},
        )

    def _fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        accepted_types: set[str],
    ) -> tuple[bytes, str] | None:
        request = urllib.request.Request(url, headers={"User-Agent": "Uniform-DGX-Spark/0.1"})
        try:
            with self.opener.open(request, timeout=5) as response:
                content_type = response.headers.get_content_type().lower()
                if content_type not in accepted_types:
                    return None
                data = response.read(max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            return None
        if not data or len(data) > max_bytes:
            return None
        return data, content_type

    @staticmethod
    def _valid_model_id(model_id: str) -> bool:
        parts = model_id.split("/")
        return (
            len(parts) == 2
            and all(parts)
            and len(model_id) <= 255
            and not any(character.isspace() or character == "\x00" for character in model_id)
        )

    @staticmethod
    def _fallback(publisher: str) -> bytes:
        initials = (
            "".join(part[0].upper() for part in publisher.replace("_", "-").split("-") if part)[:2]
            or "?"
        )
        safe_initials = html.escape(initials)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" '
            'viewBox="0 0 96 96"><rect width="96" height="96" rx="20" fill="#26352d"/>'
            f'<text x="48" y="59" text-anchor="middle" font-family="sans-serif" '
            f'font-size="34" font-weight="700" fill="#9bea18">{safe_initials}</text></svg>'
        ).encode()
