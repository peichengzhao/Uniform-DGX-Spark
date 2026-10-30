from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse


class ReferenceError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "svg"}:
            self._ignored_depth += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "pre", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class ReferenceDocument:
    url: str
    content_type: str
    text: str


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        SafeReferenceFetcher._validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeReferenceFetcher:
    def __init__(self, *, max_bytes: int = 1_000_000, timeout: int = 15) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _SafeRedirectHandler()
        )

    def fetch(self, url: str) -> ReferenceDocument:
        self._validate_public_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Uniform-DGX-Spark/0.2 reference-fetcher",
                "Accept": "text/html,text/plain,application/json,text/markdown",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                final_url = response.geturl()
                self._validate_public_url(final_url)
                content_type = response.headers.get_content_type()
                if content_type not in {
                    "text/html",
                    "text/plain",
                    "text/markdown",
                    "application/json",
                }:
                    raise ReferenceError(f"unsupported reference content type: {content_type}")
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > self.max_bytes:
                    raise ReferenceError("reference exceeds size limit")
                body = response.read(self.max_bytes + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ReferenceError(f"failed to fetch reference: {error}") from error
        if len(body) > self.max_bytes:
            raise ReferenceError("reference exceeds size limit")
        charset = response.headers.get_content_charset() or "utf-8"
        decoded = body.decode(charset, errors="replace")
        if content_type == "text/html":
            parser = _TextExtractor()
            parser.feed(decoded)
            decoded = parser.text()
        return ReferenceDocument(url=final_url, content_type=content_type, text=decoded)

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ReferenceError("reference URL must use http or https")
        try:
            default_port = 443 if parsed.scheme == "https" else 80
            addresses = socket.getaddrinfo(
                parsed.hostname, parsed.port or default_port, type=socket.SOCK_STREAM
            )
        except socket.gaierror as error:
            raise ReferenceError(f"reference host cannot be resolved: {parsed.hostname}") from error
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ReferenceError("private, loopback, and link-local references are blocked")
