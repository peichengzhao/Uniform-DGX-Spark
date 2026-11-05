from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import socket
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from secrets import token_urlsafe

import httpx
import psutil
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from ..ai_store import AIDeploymentResultNotFound, AISessionNotFound, AISessionStore
from ..gateway_store import GatewayStore, GatewayTokenNotFound
from ..publication_store import ServicePublication, ServicePublicationStore
from ..state import DeploymentStatus
from ..store import DeploymentNotFound, DeploymentStore
from .docker_runtime import DockerRuntime


class PublicationTargetNotFound(LookupError):
    pass


class PublicationTargetUnavailable(RuntimeError):
    pass


class PublicationAlreadyEnabled(RuntimeError):
    pass


class PublicationNotEnabled(RuntimeError):
    pass


class PublicationGatewayUnavailable(RuntimeError):
    pass


class PublicationRequestTooLarge(ValueError):
    pass


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    target_type: str
    target_id: str
    label: str
    model_key: str
    port: int
    container_name: str
    running: bool


@dataclass(frozen=True, slots=True)
class PublicationStatus:
    target: PublicationTarget
    publication: ServicePublication | None
    base_url: str | None
    gateway_running: bool


@dataclass(frozen=True, slots=True)
class GatewayRequestIdentity:
    token_id: str | None
    consumer_id: str | None
    legacy_key_hash: str | None = None


@dataclass(slots=True)
class GatewayAdmissionLease:
    key: str
    token_event: list[float | int]
    released: bool = False


class GatewayAdmissionController:
    """Per-key request/token windows plus live concurrency protection."""

    def __init__(
        self,
        *,
        requests_per_minute: int,
        tokens_per_minute: int,
        key_concurrency: int,
        total_concurrency: int,
    ) -> None:
        self.requests_per_minute = max(int(requests_per_minute), 0)
        self.tokens_per_minute = max(int(tokens_per_minute), 0)
        self.key_concurrency = max(int(key_concurrency), 0)
        self.total_concurrency = max(int(total_concurrency), 0)
        self._requests: dict[str, deque[float]] = {}
        self._tokens: dict[str, deque[list[float | int]]] = {}
        self._active_by_key: dict[str, int] = {}
        self._active_total = 0
        self._lock = threading.Lock()

    def acquire(
        self, key: str, projected_tokens: int
    ) -> tuple[GatewayAdmissionLease | None, int]:
        now = time.monotonic()
        projected_tokens = max(int(projected_tokens), 0)
        with self._lock:
            requests = self._requests.setdefault(key, deque())
            self._prune_times(requests, now)
            if (
                self.requests_per_minute > 0
                and len(requests) >= self.requests_per_minute
            ):
                retry_after = max(1, math.ceil(60 - (now - requests[0])))
                return None, retry_after

            token_events = self._tokens.setdefault(key, deque())
            self._prune_token_events(token_events, now)
            used_tokens = sum(int(event[1]) for event in token_events)
            if (
                self.tokens_per_minute > 0
                and used_tokens + projected_tokens > self.tokens_per_minute
            ):
                return None, 60

            key_active = self._active_by_key.get(key, 0)
            if self.key_concurrency > 0 and key_active >= self.key_concurrency:
                return None, 1
            if (
                self.total_concurrency > 0
                and self._active_total >= self.total_concurrency
            ):
                return None, 1

            requests.append(now)
            event: list[float | int] = [now, projected_tokens]
            token_events.append(event)
            self._active_by_key[key] = key_active + 1
            self._active_total += 1
            return GatewayAdmissionLease(key, event), 0

    def release(
        self, lease: GatewayAdmissionLease | None, actual_tokens: int | None = None
    ) -> None:
        if lease is None or lease.released:
            return
        with self._lock:
            if lease.released:
                return
            lease.released = True
            if actual_tokens is not None:
                lease.token_event[1] = max(int(actual_tokens), 0)
            active = self._active_by_key.get(lease.key, 0)
            if active <= 1:
                self._active_by_key.pop(lease.key, None)
            else:
                self._active_by_key[lease.key] = active - 1
            self._active_total = max(self._active_total - 1, 0)

    @staticmethod
    def _prune_times(events: deque[float], now: float) -> None:
        while events and now - events[0] >= 60:
            events.popleft()

    @classmethod
    def _prune_token_events(
        cls, events: deque[list[float | int]], now: float
    ) -> None:
        while events and now - float(events[0][0]) >= 60:
            events.popleft()


class ServicePublicationManager:
    _ALLOWED_ENDPOINTS = {
        ("GET", "v1/models"),
        ("POST", "v1/chat/completions"),
        ("POST", "v1/completions"),
        ("POST", "v1/embeddings"),
        ("POST", "v1/responses"),
    }
    _MAX_REQUEST_BYTES = 16 * 1024 * 1024
    _MAX_USAGE_CAPTURE_BYTES = 2 * 1024 * 1024
    _RESPONSE_HEADERS = {
        "cache-control",
        "content-encoding",
        "content-type",
        "x-request-id",
    }

    def __init__(
        self,
        store: ServicePublicationStore,
        deployment_store: DeploymentStore,
        ai_store: AISessionStore,
        runtime: DockerRuntime,
        gateway_store: GatewayStore | None = None,
        *,
        listen_host: str = "0.0.0.0",
        listen_port: int = 18181,
        advertise_host: str | None = None,
        request_timeout_seconds: int = 1800,
        gateway_requests_per_minute: int = 60,
        gateway_tokens_per_minute: int = 120_000,
        gateway_key_concurrency: int = 4,
        gateway_total_concurrency: int = 16,
        gateway_quota_reservation_tokens: int = 1024,
    ) -> None:
        self.store = store
        self.deployment_store = deployment_store
        self.ai_store = ai_store
        self.runtime = runtime
        self.gateway_store = gateway_store
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.advertise_host = advertise_host
        self.request_timeout_seconds = request_timeout_seconds
        self.gateway_quota_reservation_tokens = max(
            int(gateway_quota_reservation_tokens), 1
        )
        self._admission = GatewayAdmissionController(
            requests_per_minute=gateway_requests_per_minute,
            tokens_per_minute=gateway_tokens_per_minute,
            key_concurrency=gateway_key_concurrency,
            total_concurrency=gateway_total_concurrency,
        )
        self.gateway_app = self._build_gateway_app()
        self._gateway_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._gateway_server: uvicorn.Server | None = None
        self._gateway_thread: threading.Thread | None = None
        self.gateway_error: str | None = None

    def list_statuses(self) -> list[PublicationStatus]:
        statuses: list[PublicationStatus] = []
        for publication in self.store.list_enabled():
            try:
                target = self._resolve_target(
                    publication.target_type, publication.target_id, require_running=False
                )
            except PublicationTargetNotFound:
                continue
            statuses.append(
                PublicationStatus(
                    target=target,
                    publication=publication,
                    base_url=self._base_url(publication.publication_id),
                    gateway_running=self.gateway_running,
                )
            )
        return statuses

    @property
    def gateway_running(self) -> bool:
        with self._gateway_lock:
            return bool(
                self._gateway_server
                and self._gateway_server.started
                and self._gateway_thread
                and self._gateway_thread.is_alive()
            )

    def start_for_existing_publications(self) -> None:
        if self.store.count_enabled():
            try:
                self.ensure_gateway_started()
            except PublicationGatewayUnavailable as error:
                self.gateway_error = str(error)

    def ensure_gateway_started(self) -> None:
        with self._gateway_lock:
            if self.gateway_running:
                return
            if self._gateway_thread is not None and self._gateway_thread.is_alive():
                raise PublicationGatewayUnavailable(
                    self.gateway_error or "publication gateway is still shutting down"
                )
            self.gateway_error = None
            config = uvicorn.Config(
                self.gateway_app,
                host=self.listen_host,
                port=self.listen_port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(config)
            thread = threading.Thread(
                target=server.run,
                name="uniform-model-publication-gateway",
                daemon=True,
            )
            self._gateway_server = server
            self._gateway_thread = thread
            thread.start()
            deadline = time.monotonic() + 5
            while thread.is_alive() and not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            if not server.started:
                server.should_exit = True
                thread.join(timeout=1)
                self.gateway_error = f"could not listen on {self.listen_host}:{self.listen_port}"
                if not thread.is_alive():
                    self._gateway_server = None
                    self._gateway_thread = None
                raise PublicationGatewayUnavailable(self.gateway_error)

    def stop_gateway_if_idle(self) -> None:
        if self.store.count_enabled() == 0:
            self.stop_gateway()

    def stop_gateway(self) -> None:
        with self._gateway_lock:
            server = self._gateway_server
            thread = self._gateway_thread
            if server is None or thread is None:
                return
            server.should_exit = True
            thread.join(timeout=5)
            if thread.is_alive():
                server.force_exit = True
                thread.join(timeout=1)
            if thread.is_alive():
                self.gateway_error = "publication gateway did not stop cleanly"
                return
            self._gateway_server = None
            self._gateway_thread = None

    def status(self, target_type: str, target_id: str) -> PublicationStatus:
        target = self._resolve_target(target_type, target_id, require_running=False)
        publication = self.store.get_for_target(target_type, target_id)
        enabled = publication is not None and publication.enabled
        return PublicationStatus(
            target=target,
            publication=publication,
            base_url=self._base_url(publication.publication_id) if enabled else None,
            gateway_running=self.gateway_running,
        )

    def publish(self, target_type: str, target_id: str) -> tuple[PublicationStatus, str]:
        with self._operation_lock:
            target = self._resolve_target(target_type, target_id, require_running=True)
            if not self.runtime.health(target.port, timeout=3.0):
                raise PublicationTargetUnavailable("model service is not running or healthy")
            if target.target_type == "ai_session" and not self._port_is_loopback_only(
                target.container_name, target.port
            ):
                raise PublicationTargetUnavailable(
                    "model service raw API is exposed beyond loopback; redeploy it on "
                    "127.0.0.1 before enabling authenticated LAN access"
                )
            existing = self.store.get_for_target(target_type, target_id)
            if existing is not None and existing.enabled:
                raise PublicationAlreadyEnabled("model service is already published")
            self.ensure_gateway_started()
            api_key = self._new_api_key()
            try:
                publication = self.store.enable(
                    target_type,
                    target_id,
                    api_key_hash=self._hash_api_key(api_key),
                    key_hint=self._key_hint(api_key),
                    model_key=target.model_key,
                )
            except Exception:
                self.stop_gateway_if_idle()
                raise
            self._record_event_best_effort(target, "service_published", publication.publication_id)
            return self.status(target_type, target_id), api_key

    def rotate_key(self, target_type: str, target_id: str) -> tuple[PublicationStatus, str]:
        with self._operation_lock:
            target = self._resolve_target(target_type, target_id, require_running=False)
            existing = self.store.get_for_target(target_type, target_id)
            if existing is None or not existing.enabled:
                raise PublicationNotEnabled("model service is not published")
            self.ensure_gateway_started()
            api_key = self._new_api_key()
            publication = self.store.enable(
                target_type,
                target_id,
                api_key_hash=self._hash_api_key(api_key),
                key_hint=self._key_hint(api_key),
                model_key=target.model_key,
            )
            self._record_event_best_effort(
                target, "publication_key_rotated", publication.publication_id
            )
            return self.status(target_type, target_id), api_key

    def revoke(self, target_type: str, target_id: str) -> bool:
        with self._operation_lock:
            publication = self.store.get_for_target(target_type, target_id)
            revoked = self.store.disable(target_type, target_id)
            try:
                target = self._resolve_target(target_type, target_id, require_running=False)
            except PublicationTargetNotFound:
                target = None
            if revoked and publication is not None and target is not None:
                self._record_event_best_effort(
                    target, "service_publication_revoked", publication.publication_id
                )
            try:
                self.stop_gateway_if_idle()
            except Exception:  # noqa: BLE001 - revocation must remain authoritative
                LOGGER.exception("could not stop idle publication gateway")
            return revoked

    def verify_key(self, publication: ServicePublication, supplied_key: str) -> bool:
        if not supplied_key or len(supplied_key) > 512:
            return False
        supplied_hash = self._hash_api_key(supplied_key)
        return hmac.compare_digest(publication.api_key_hash, supplied_hash)

    def _resolve_target(
        self,
        target_type: str,
        target_id: str,
        *,
        require_running: bool,
    ) -> PublicationTarget:
        try:
            if target_type == "deployment":
                deployment = self.deployment_store.get(target_id)
                running = deployment.status == DeploymentStatus.RUNNING and self.runtime.is_running(
                    deployment.id
                )
                target = PublicationTarget(
                    target_type=target_type,
                    target_id=target_id,
                    label=deployment.display_name or deployment.model_id,
                    model_key=deployment.model_id,
                    port=deployment.parameters.port,
                    container_name=self.runtime.container_name(deployment.id),
                    running=running,
                )
            elif target_type == "ai_session":
                session = self.ai_store.get(target_id)
                result = self.ai_store.get_result(target_id)
                running = result.status == "verified" and self.runtime.is_named_container_running(
                    result.container_name
                )
                target = PublicationTarget(
                    target_type=target_type,
                    target_id=target_id,
                    label=session.display_name,
                    model_key=session.target_model_id or f"ai_session:{target_id}",
                    port=result.port,
                    container_name=result.container_name,
                    running=running,
                )
            else:
                raise PublicationTargetNotFound("unsupported publication target")
        except (DeploymentNotFound, AISessionNotFound, AIDeploymentResultNotFound) as error:
            raise PublicationTargetNotFound("model service not found") from error
        if require_running and not target.running:
            raise PublicationTargetUnavailable("model service is not running")
        return target

    def _record_event(
        self, target: PublicationTarget, event_type: str, publication_id: str
    ) -> None:
        details = {"publication_id": publication_id, "listen_port": self.listen_port}
        if target.target_type == "deployment":
            self.deployment_store.record_event(
                target.target_id,
                event_type,
                "Model service publication settings changed",
                details=details,
            )
        else:
            self.ai_store.add_audit(
                target.target_id,
                event_type,
                "Model service publication settings changed",
                details,
            )

    def _record_event_best_effort(
        self, target: PublicationTarget, event_type: str, publication_id: str
    ) -> None:
        try:
            self._record_event(target, event_type, publication_id)
        except Exception:  # noqa: BLE001 - publication state must not be rolled back by audit
            LOGGER.exception(
                "could not record publication audit event %s for %s/%s",
                event_type,
                target.target_type,
                target.target_id,
            )

    def _port_is_loopback_only(self, container_name: str, port: int) -> bool:
        bindings = self.runtime.container_host_bindings(container_name, port)
        if bindings:
            return all(self._is_loopback_address(address) for address in bindings)
        try:
            listeners = {
                connection.laddr.ip
                for connection in psutil.net_connections(kind="tcp")
                if connection.status == psutil.CONN_LISTEN
                and connection.laddr
                and connection.laddr.port == port
            }
        except (OSError, psutil.Error):
            return False
        return bool(listeners) and all(self._is_loopback_address(address) for address in listeners)

    @staticmethod
    def _is_loopback_address(address: str) -> bool:
        try:
            return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
        except ValueError:
            return address.casefold() == "localhost"

    def _build_gateway_app(self) -> FastAPI:
        gateway = FastAPI(
            title="Uniform model publication gateway",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @gateway.api_route(
            "/{publication_id}/{upstream_path:path}",
            methods=["GET", "POST"],
            include_in_schema=False,
            response_model=None,
        )
        async def proxy_model_request(
            publication_id: str,
            upstream_path: str,
            request: Request,
        ) -> Response:
            publication = await asyncio.to_thread(self.store.get_enabled_by_id, publication_id)
            supplied_key = self._request_api_key(request)
            identity, failure = await asyncio.to_thread(
                self._authenticate_request, publication, supplied_key
            )
            if identity is None:
                failure_status, failure_message = failure or (401, "invalid API key")
                return JSONResponse(
                    status_code=failure_status,
                    content={"error": {"message": failure_message}},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            normalized_path = upstream_path.strip("/")
            if (request.method, normalized_path) not in self._ALLOWED_ENDPOINTS:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"message": "endpoint is not published"}},
                )
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit():
                if int(content_length) > self._MAX_REQUEST_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"error": {"message": "request body is too large"}},
                    )
            try:
                async with asyncio.timeout(30):
                    body = await self._read_bounded_body(request)
            except PublicationRequestTooLarge:
                return JSONResponse(
                    status_code=413,
                    content={"error": {"message": "request body is too large"}},
                )
            except TimeoutError:
                return JSONResponse(
                    status_code=408,
                    content={"error": {"message": "request body timed out"}},
                )
            except ClientDisconnect:
                return JSONResponse(
                    status_code=400,
                    content={"error": {"message": "request body was interrupted"}},
                )
            prepared_body = self._prepare_request_body(normalized_path, body)
            model = self._request_model(prepared_body)
            started_at = time.monotonic()
            if not await asyncio.to_thread(
                self._publication_is_current, publication, identity
            ):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"message": "API key or model access was revoked"}},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            projected_tokens, explicit_token_budget = self._request_token_budget(
                prepared_body
            )
            admission_key = (
                identity.token_id
                or identity.legacy_key_hash
                or self._hash_api_key(supplied_key)
            )
            lease, retry_after = self._admission.acquire(
                admission_key, projected_tokens
            )
            if lease is None:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"message": "gateway rate limit exceeded"}},
                    headers={"Retry-After": str(max(retry_after, 1))},
                )
            reservation = 0
            if request.method == "POST" and identity.token_id and self.gateway_store:
                try:
                    reservation = await asyncio.to_thread(
                        self.gateway_store.reserve_tokens,
                        identity.token_id,
                        projected_tokens,
                        strict=explicit_token_budget,
                    )
                except Exception:
                    self._admission.release(lease, 0)
                    raise
                if reservation is None:
                    self._admission.release(lease, 0)
                    return JSONResponse(
                        status_code=429,
                        content={"error": {"message": "API key token quota is exhausted"}},
                        headers={"Retry-After": "60"},
                    )
            try:
                target = await asyncio.to_thread(
                    self._resolve_target,
                    publication.target_type,
                    publication.target_id,
                    require_running=True,
                )
                upstream, client = await self._open_upstream(
                    target.port,
                    normalized_path,
                    request.url.query,
                    request.method,
                    prepared_body,
                    request.headers,
                )
            except (PublicationTargetNotFound, PublicationTargetUnavailable):
                self._admission.release(lease, 0)
                await asyncio.to_thread(
                    self._record_usage_best_effort,
                    publication,
                    identity,
                    normalized_path,
                    request.method,
                    model,
                    503,
                    None,
                    None,
                    None,
                    "unavailable",
                    started_at,
                    None,
                    reservation,
                    False,
                )
                return JSONResponse(
                    status_code=503,
                    content={"error": {"message": "model service is unavailable"}},
                )
            except httpx.HTTPError:
                self._admission.release(lease, 0)
                await asyncio.to_thread(
                    self._record_usage_best_effort,
                    publication,
                    identity,
                    normalized_path,
                    request.method,
                    model,
                    502,
                    None,
                    None,
                    None,
                    "unavailable",
                    started_at,
                    None,
                    reservation,
                    False,
                )
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": "model service request failed"}},
                )
            except Exception:
                self._admission.release(lease, 0)
                if (
                    self.gateway_store is not None
                    and identity.token_id
                    and reservation
                ):
                    await asyncio.to_thread(
                        self.gateway_store.release_reserved_tokens,
                        identity.token_id,
                        reservation,
                    )
                raise
            headers = {
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in self._RESPONSE_HEADERS
            }
            return StreamingResponse(
                self._iter_upstream(
                    upstream,
                    client,
                    publication,
                    identity,
                    model,
                    normalized_path,
                    request.method,
                    started_at,
                    lease,
                    reservation,
                ),
                status_code=upstream.status_code,
                headers=headers,
            )

        return gateway

    @classmethod
    async def _read_bounded_body(cls, request: Request) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > cls._MAX_REQUEST_BYTES:
                raise PublicationRequestTooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    async def _open_upstream(
        self,
        port: int,
        path: str,
        query: str,
        method: str,
        body: bytes,
        inbound_headers: Headers,
    ) -> tuple[httpx.Response, httpx.AsyncClient]:
        get_header = inbound_headers.get
        headers = {
            "Accept": get_header("accept", "application/json"),
            # Usage capture reads the raw response stream. Disable upstream
            # compression so JSON/SSE accounting is not hidden by gzip bytes.
            "Accept-Encoding": "identity",
        }
        content_type = get_header("content-type")
        if content_type:
            headers["Content-Type"] = content_type
        target_url = f"http://127.0.0.1:{port}/{path}"
        if query:
            target_url = f"{target_url}?{query}"
        timeout = httpx.Timeout(
            connect=5.0,
            read=float(self.request_timeout_seconds),
            write=30.0,
            pool=5.0,
        )
        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
        )
        upstream_request = client.build_request(
            method,
            target_url,
            content=body if method == "POST" else None,
            headers=headers,
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except Exception:
            await client.aclose()
            raise
        return response, client

    async def _iter_upstream(
        self,
        upstream: httpx.Response,
        client: httpx.AsyncClient,
        publication: ServicePublication,
        identity: GatewayRequestIdentity,
        model: str | None,
        endpoint: str,
        method: str,
        started_at: float,
        admission_lease: GatewayAdmissionLease,
        reserved_tokens: int,
    ) -> AsyncIterator[bytes]:
        last_validation = 0.0
        captured = bytearray()
        try:
            async for chunk in upstream.aiter_raw():
                captured.extend(chunk)
                if len(captured) > self._MAX_USAGE_CAPTURE_BYTES:
                    del captured[: len(captured) - self._MAX_USAGE_CAPTURE_BYTES]
                now = time.monotonic()
                if now - last_validation >= 0.25:
                    current = await asyncio.to_thread(
                        self._publication_is_current, publication, identity
                    )
                    if not current:
                        break
                    last_validation = now
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            prompt_tokens, completion_tokens, total_tokens = self._extract_usage(bytes(captured))
            charge_reserved = (
                upstream.status_code < 400 and total_tokens is None and reserved_tokens > 0
            )
            actual_tokens = (
                total_tokens
                if total_tokens is not None
                else (reserved_tokens if charge_reserved else 0)
            )
            recorded_total = (
                total_tokens
                if total_tokens is not None
                else (reserved_tokens if charge_reserved else None)
            )
            self._admission.release(admission_lease, actual_tokens)
            await asyncio.to_thread(
                self._record_usage_best_effort,
                publication,
                identity,
                endpoint,
                method,
                model,
                upstream.status_code,
                prompt_tokens,
                completion_tokens,
                recorded_total,
                (
                    "upstream"
                    if total_tokens is not None
                    else ("estimated" if charge_reserved else "unavailable")
                ),
                started_at,
                upstream.headers.get("x-request-id"),
                reserved_tokens,
                charge_reserved,
            )

    def _authenticate_request(
        self,
        publication: ServicePublication | None,
        supplied_key: str,
    ) -> tuple[GatewayRequestIdentity | None, tuple[int, str] | None]:
        if publication is None:
            return None, (401, "invalid API key")
        if self.gateway_store is not None:
            token = self.gateway_store.find_token_by_key(supplied_key)
            if token is not None:
                if not token.enabled or not token.consumer_enabled:
                    return None, (401, "API key is disabled")
                if token.expires_in_past():
                    return None, (401, "API key has expired")
                if token.quota_exhausted():
                    return None, (429, "API key token quota is exhausted")
                if not self.gateway_store.can_access_publication(
                    token.token_id,
                    publication.publication_id,
                    publication.model_key,
                ):
                    return None, (403, "API key is not allowed to access this model")
                return GatewayRequestIdentity(token.token_id, token.consumer_id), None
        if self.verify_key(publication, supplied_key):
            return (
                GatewayRequestIdentity(None, None, legacy_key_hash=publication.api_key_hash),
                None,
            )
        return None, (401, "invalid API key")

    def _publication_is_current(
        self,
        publication: ServicePublication,
        identity: GatewayRequestIdentity,
    ) -> bool:
        current = self.store.get_enabled_by_id(publication.publication_id)
        if not (
            current
            and current.target_type == publication.target_type
            and current.target_id == publication.target_id
        ):
            return False
        if identity.token_id is not None:
            if self.gateway_store is None:
                return False
            try:
                token = self.gateway_store.get_token(identity.token_id)
            except GatewayTokenNotFound:
                return False
            return token.is_usable() and self.gateway_store.can_access_publication(
                token.token_id,
                publication.publication_id,
                publication.model_key,
            )
        return bool(
            identity.legacy_key_hash
            and hmac.compare_digest(current.api_key_hash, identity.legacy_key_hash)
        )

    @staticmethod
    def _request_model(body: bytes) -> str | None:
        if not body:
            return None
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return None
        model = payload.get("model") if isinstance(payload, dict) else None
        return model if isinstance(model, str) and model else None

    @classmethod
    def _prepare_request_body(cls, path: str, body: bytes) -> bytes:
        if not body or path not in {"v1/chat/completions", "v1/completions"}:
            return body
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return body
        if not isinstance(payload, dict) or payload.get("stream") is not True:
            return body
        options = payload.get("stream_options")
        if not isinstance(options, dict):
            options = {}
        if options.get("include_usage") is not True:
            options["include_usage"] = True
            payload["stream_options"] = options
            return json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return body

    def _request_token_budget(self, body: bytes) -> tuple[int, bool]:
        default = self.gateway_quota_reservation_tokens
        if not body:
            return 0, False
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return default, False
        if not isinstance(payload, dict):
            return default, False
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return max(value, 1), True
        return default, False

    @classmethod
    def _extract_usage(cls, payload: bytes) -> tuple[int | None, int | None, int | None]:
        if not payload:
            return None, None, None
        text = payload.decode("utf-8", errors="replace")
        candidates = [text]
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                data = stripped[5:].strip()
                if data and data != "[DONE]":
                    candidates.append(data)
        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
            except ValueError:
                continue
            usage = parsed.get("usage") if isinstance(parsed, dict) else None
            if not isinstance(usage, dict):
                continue
            prompt = cls._optional_token_count(usage.get("prompt_tokens"))
            if prompt is None:
                prompt = cls._optional_token_count(usage.get("input_tokens"))
            completion = cls._optional_token_count(usage.get("completion_tokens"))
            if completion is None:
                completion = cls._optional_token_count(usage.get("output_tokens"))
            total = cls._optional_token_count(usage.get("total_tokens"))
            if total is None and prompt is not None and completion is not None:
                total = prompt + completion
            if prompt is not None or completion is not None or total is not None:
                return prompt, completion, total
        return None, None, None

    @staticmethod
    def _optional_token_count(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _record_usage_best_effort(
        self,
        publication: ServicePublication,
        identity: GatewayRequestIdentity,
        endpoint: str,
        method: str,
        model: str | None,
        status_code: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        usage_source: str,
        started_at: float,
        request_id: str | None,
        reserved_tokens: int = 0,
        charge_reserved: bool = False,
    ) -> None:
        if self.gateway_store is None:
            return
        try:
            target = self._resolve_target(
                publication.target_type, publication.target_id, require_running=False
            )
            self.gateway_store.record_usage(
                token_id=identity.token_id,
                consumer_id=identity.consumer_id,
                publication_id=publication.publication_id,
                publication_label=target.label,
                target_type=target.target_type,
                target_id=target.target_id,
                model=model,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                usage_source=usage_source,
                latency_ms=int((time.monotonic() - started_at) * 1000),
                request_id=request_id,
                reserved_tokens=reserved_tokens,
                charge_reserved=charge_reserved,
            )
        except Exception:  # noqa: BLE001 - usage accounting must not break the proxy
            LOGGER.exception("could not record gateway usage for %s", publication.publication_id)
            if identity.token_id and reserved_tokens:
                try:
                    self.gateway_store.release_reserved_tokens(
                        identity.token_id, reserved_tokens
                    )
                except Exception:  # noqa: BLE001 - one last best effort release
                    LOGGER.exception(
                        "could not release reserved quota for %s",
                        identity.token_id,
                    )

    @staticmethod
    def _request_api_key(request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return request.headers.get("x-api-key", "").strip()

    @staticmethod
    def _new_api_key() -> str:
        return f"udgs_{token_urlsafe(32)}"

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _key_hint(api_key: str) -> str:
        return f"{api_key[:10]}…{api_key[-4:]}"

    def _base_url(self, publication_id: str) -> str:
        host = self.advertise_host
        if not host:
            host = (
                self._discover_lan_host()
                if self.listen_host in {"0.0.0.0", "::"}
                else self.listen_host
            )
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.listen_port}/{publication_id}/v1"

    @staticmethod
    def _discover_lan_host() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                address = str(probe.getsockname()[0])
                if address and not address.startswith("127."):
                    return address
        except OSError:
            pass
        try:
            address = socket.gethostbyname(socket.gethostname())
            if address and not address.startswith("127."):
                return address
        except OSError:
            pass
        return socket.gethostname()
