"""Opt-in bot DMs, one-time OAuth recipient binding and persistent resource alerts."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx

from ..database import Database
from ..feishu_models import (
    METRIC_LABELS,
    FeishuBindingChecks,
    FeishuBindingDiagnostic,
    FeishuConfigUpdate,
)
from .cluster_monitoring import ClusterMonitoringService
from .monitoring import MetricsService

TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
AUTHORIZE_URL = "https://accounts.feishu.cn/oauth/v3/authorize"
USER_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
LOGGER = logging.getLogger(__name__)


def _pkce_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode()
        .rstrip("=")
    )


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: int | None = None,
        http_status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.http_status = http_status
        self.request_id = request_id
        self.binding_attempt_id: str | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class FeishuNotifications:
    def __init__(
        self,
        database: Database,
        data_dir: Path,
        metrics: MetricsService,
        cluster: ClusterMonitoringService,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.database, self.metrics, self.cluster = database, metrics, cluster
        self.path = data_dir / "feishu-private" / "config.json"
        self.diagnostic_path = self.path.with_name("binding-diagnostic.json")
        self.last_binding: FeishuBindingDiagnostic | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(
            transport=transport, timeout=10, follow_redirects=False, trust_env=False
        )
        self._oauth: dict[str, dict[str, Any]] = {}
        self._token, self._token_until = "", 0.0
        self.last_check_at: float | None = None
        self.last_error: str | None = None
        self._config = FeishuConfigUpdate().model_dump(mode="json", exclude={"app_secret"})
        self._config.update(app_secret="", generation=0)
        if self.path.exists():
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise FeishuError("飞书配置文件权限必须为仅当前用户可读写，且不能是符号链接")
            stored = json.loads(self.path.read_text())
            parsed = FeishuConfigUpdate.model_validate(
                {k: v for k, v in stored.items() if k != "generation"}
            )
            self._config = parsed.model_dump(mode="json", exclude={"app_secret"})
            self._config.update(
                app_secret=parsed.app_secret.get_secret_value() if parsed.app_secret else "",
                generation=int(stored.get("generation", 0)),
            )
        if self.diagnostic_path.exists():
            try:
                if (
                    self.diagnostic_path.is_symlink()
                    or self.diagnostic_path.stat().st_mode & 0o077
                    or self.diagnostic_path.parent.is_symlink()
                    or self.diagnostic_path.parent.stat().st_mode & 0o077
                ):
                    raise ValueError("Unsafe diagnostic permissions")
                self.last_binding = FeishuBindingDiagnostic.model_validate_json(
                    self.diagnostic_path.read_text()
                )
            except (OSError, ValueError):
                LOGGER.warning("Feishu binding diagnostic unavailable; ignoring saved diagnostic")

    def _save(self, config: dict[str, Any]) -> None:
        self._write_private(self.path, config)

    @staticmethod
    def _write_private(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
            raise FeishuError("飞书私有配置目录权限必须为 700")
        descriptor, name = tempfile.mkstemp(prefix="private-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _record_binding(self, diagnostic: FeishuBindingDiagnostic) -> None:
        self.last_binding = diagnostic
        # The model has a closed schema; response bodies and credential values never enter it.
        LOGGER.info("Feishu binding diagnostic %s", diagnostic.model_dump_json())
        try:
            self._write_private(self.diagnostic_path, diagnostic.model_dump(mode="json"))
        except (OSError, FeishuError):
            LOGGER.warning("Feishu binding diagnostic persistence unavailable")

    def _binding(self) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM feishu_recipient WHERE id=1").fetchone()
        if row and row["app_id"] == self._config["app_id"]:
            return dict(row)
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            config = {k: v for k, v in self._config.items() if k != "app_secret"}
            binding = self._binding()
            return {
                "config": config,
                "secret_configured": bool(self._config["app_secret"]),
                "configured": self._configured(),
                "recipient": (
                    {k: binding[k] for k in ("name", "bound_at", "verified_at")}
                    if binding
                    else None
                ),
                "last_check_at": self.last_check_at,
                "last_error": self.last_error,
                "worker_running": bool(self._thread and self._thread.is_alive()),
                "authorization_mode": "official_oauth_page",
                "authorization_flow": "v3_pkce_s256",
                "last_binding": self.last_binding.model_dump(mode="json")
                if self.last_binding
                else None,
            }

    def _configured(self) -> bool:
        return all(self._config[k] for k in ("app_id", "app_secret", "redirect_uri"))

    def configure(self, update: FeishuConfigUpdate) -> dict[str, Any]:
        with self._lock:
            config = update.model_dump(mode="json", exclude={"app_secret"})
            config["app_secret"] = (
                update.app_secret.get_secret_value()
                if update.app_secret and update.app_secret.get_secret_value()
                else self._config["app_secret"]
            )
            if len(config["app_secret"]) > 4096:
                raise FeishuError("App Secret 长度超出限制")
            if config["app_id"] != self._config["app_id"] and not update.app_secret:
                config["app_secret"] = ""
            identity_changed = any(
                config[k] != self._config[k] for k in ("app_id", "app_secret", "redirect_uri")
            )
            binding = None if identity_changed else self._binding()
            if config["enabled"] and (
                not all(config[k] for k in ("app_id", "app_secret", "redirect_uri"))
                or not binding
                or not binding["verified_at"]
            ):
                raise FeishuError("请先配置应用、扫码绑定，并成功发送测试消息，再开启告警")
            config["generation"] = self._config["generation"] + 1
            self._save(config)
            self._config = config
            self._oauth.clear()
            if identity_changed:
                self._token, self._token_until = "", 0
            with self.database.connect() as connection:
                if identity_changed:
                    connection.execute("DELETE FROM feishu_recipient")
                connection.execute("DELETE FROM system_alert_state")
                connection.execute(
                    "UPDATE system_alert_events SET delivery='cancelled',error=? "
                    "WHERE delivery='pending'",
                    ("配置已变更，旧通知已取消",),
                )
                connection.commit()
            self.last_error = None
            return self.status()

    def begin_binding(self, browser_nonce: str) -> str:
        with self._lock:
            if not self._configured():
                raise FeishuError("请先创建飞书自建应用并保存 App ID、App Secret 和回调地址")
            if self._config["enabled"]:
                raise FeishuError("重新绑定前请先关闭告警")
            now = time.time()
            self._oauth = {k: v for k, v in self._oauth.items() if v["expires"] > now}
            if len(self._oauth) >= 32:
                raise FeishuError("授权请求过多，请稍后重试")
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
            challenge = _pkce_challenge(verifier)
            pending = {
                "expires": now + 300,
                "nonce_hash": hashlib.sha256(browser_nonce.encode()).hexdigest(),
                "verifier": verifier,
                "challenge": challenge,
                "app_id": self._config["app_id"],
                "redirect_uri": self._config["redirect_uri"],
                "generation": self._config["generation"],
                "attempt_id": str(uuid4()),
            }
            url = (
                AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "client_id": self._config["app_id"],
                        "response_type": "code",
                        "redirect_uri": self._config["redirect_uri"],
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "prompt": "consent",
                    }
                )
            )
            parameters = parse_qs(urlsplit(url).query)
            valid = all(
                parameters.get(key) == [value]
                for key, value in {
                    "client_id": pending["app_id"],
                    "redirect_uri": pending["redirect_uri"],
                    "state": state,
                    "response_type": "code",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }.items()
            )
            diagnostic = FeishuBindingDiagnostic(
                attempt_id=pending["attempt_id"],
                stage="authorization",
                result="pending",
                checked_at=now,
                checks=FeishuBindingChecks(authorization_parameters_valid=valid),
            )
            self._record_binding(diagnostic)
            if not valid:
                raise FeishuError("授权参数构造失败，未发起飞书授权")
            self._oauth[state] = pending
            return url

    def complete_binding(
        self,
        state: str,
        code: str,
        browser_nonce: str,
        *,
        authorization_error: str = "",
    ) -> None:
        with self._lock:
            pending = self._oauth.get(state)
            if not pending:
                raise FeishuError("授权已过期或不属于当前浏览器，请重新扫码绑定")
            nonce_valid = secrets.compare_digest(
                pending["nonce_hash"], hashlib.sha256(browser_nonce.encode()).hexdigest()
            )
            expired = pending["expires"] < time.time()
            identity_matches = (
                pending["generation"] == self._config["generation"]
                and pending["app_id"] == self._config["app_id"]
                and pending["redirect_uri"] == self._config["redirect_uri"]
            )
            verifier_valid = isinstance(pending["verifier"], str) and bool(
                re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", pending["verifier"])
            )
            pkce_matches = verifier_valid and secrets.compare_digest(
                _pkce_challenge(pending["verifier"]), pending["challenge"]
            )
            checks = FeishuBindingChecks(
                authorization_parameters_valid=True,
                state_valid=not expired and identity_matches,
                browser_nonce_valid=nonce_valid,
                pkce_verifier_valid=verifier_valid,
                pkce_challenge_matches=bool(pkce_matches),
                redirect_uri_matches=identity_matches,
            )
            diagnostic = FeishuBindingDiagnostic(
                attempt_id=pending["attempt_id"],
                stage="callback",
                result="pending",
                checked_at=time.time(),
                checks=checks,
            )
            if expired or not identity_matches or not nonce_valid:
                reason = (
                    "expired"
                    if expired
                    else "configuration_changed"
                    if not identity_matches
                    else "browser_mismatch"
                )
                self._record_binding(
                    FeishuBindingDiagnostic.model_validate(
                        {
                            **diagnostic.model_dump(),
                            "result": "failed",
                            "reason": reason,
                        }
                    )
                )
                raise FeishuError("授权已过期或不属于当前浏览器，请重新扫码绑定")
            del self._oauth[state]
            if not pkce_matches or not code or len(code) > 2048 or authorization_error:
                reason = (
                    "local_pkce_mismatch"
                    if not pkce_matches
                    else "user_denied"
                    if authorization_error
                    else "invalid_code"
                )
                self._record_binding(
                    FeishuBindingDiagnostic.model_validate(
                        {
                            **diagnostic.model_dump(),
                            "result": "failed",
                            "reason": reason,
                        }
                    )
                )
                if not pkce_matches:
                    raise FeishuError("本地授权参数校验失败，未向飞书发送凭证请求，请重新绑定")
                raise FeishuError("未收到有效授权码，可能已拒绝授权，请重新绑定")
            diagnostic = FeishuBindingDiagnostic.model_validate(
                {
                    **diagnostic.model_dump(),
                    "stage": "user_token",
                }
            )
            self._record_binding(diagnostic)
            try:
                token = self._request(
                    "POST",
                    TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": pending["app_id"],
                        "client_secret": self._config["app_secret"],
                        "code": code,
                        "redirect_uri": pending["redirect_uri"],
                        "code_verifier": pending["verifier"],
                    },
                ).get("access_token")
                if not isinstance(token, str) or not token:
                    raise FeishuError("飞书未返回有效授权凭证")
                diagnostic = FeishuBindingDiagnostic.model_validate(
                    {
                        **diagnostic.model_dump(),
                        "stage": "user_info",
                    }
                )
                self._record_binding(diagnostic)
                info = self._request("GET", USER_URL, headers={"Authorization": f"Bearer {token}"})
                user = info.get("data", {})
                if not isinstance(user, dict):
                    raise FeishuError("飞书用户信息格式错误")
                open_id = user.get("open_id")
                if (
                    not isinstance(open_id, str)
                    or not open_id.startswith("ou")
                    or len(open_id) > 256
                ):
                    raise FeishuError("飞书未返回可用的收件人标识")
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO feishu_recipient "
                        "(id,app_id,open_id,name,bound_at,verified_at) VALUES(1,?,?,?,?,NULL)",
                        (
                            self._config["app_id"],
                            open_id,
                            str(user.get("name") or "飞书用户")[:128],
                            time.time(),
                        ),
                    )
                    connection.execute("DELETE FROM system_alert_state")
                    connection.execute(
                        "UPDATE system_alert_events SET delivery='cancelled',error=? "
                        "WHERE delivery='pending'",
                        ("收件人已变更，旧通知已取消",),
                    )
                    connection.commit()
            except FeishuError as failure:
                self._record_binding(
                    FeishuBindingDiagnostic.model_validate(
                        {
                            **diagnostic.model_dump(),
                            "checked_at": time.time(),
                            "result": "failed",
                            "reason": "provider_failed",
                            "error_code": failure.code,
                            "http_status": failure.http_status,
                            "request_id": failure.request_id,
                        }
                    )
                )
                failure.binding_attempt_id = pending["attempt_id"]
                raise
            self._oauth.clear()
            self._record_binding(
                FeishuBindingDiagnostic(
                    attempt_id=pending["attempt_id"],
                    stage="complete",
                    result="success",
                    checked_at=time.time(),
                    checks=checks,
                )
            )

    def unbind(self) -> None:
        with self._lock:
            config = {
                **self._config,
                "enabled": False,
                "generation": self._config["generation"] + 1,
            }
            self._save(config)
            self._config = config
            self._oauth.clear()
            with self.database.connect() as connection:
                connection.execute("DELETE FROM feishu_recipient")
                connection.execute("DELETE FROM system_alert_state")
                connection.execute(
                    "UPDATE system_alert_events SET delivery='cancelled',error=? "
                    "WHERE delivery='pending'",
                    ("收件人已解绑",),
                )
                connection.commit()

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise FeishuError("飞书网络请求失败或超时", retryable=True) from error
        try:
            data = response.json()
        except ValueError as error:
            raise FeishuError(
                "飞书返回了无效响应", retryable=response.status_code >= 500
            ) from error
        if not isinstance(data, dict):
            raise FeishuError("飞书响应格式错误")
        code = data.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise FeishuError("飞书响应缺少有效错误码", retryable=response.status_code >= 500)
        if response.is_success and code == 0:
            return data
        messages = {
            20002: "应用凭证不正确，请检查 App ID 和 App Secret",
            20009: "飞书应用尚未安装或发布",
            20010: "当前用户不在飞书应用的可用范围内",
            20069: "飞书应用未启用，请发布应用并确认管理员已启用",
            20003: "授权码无效或已使用，请重新扫码",
            20004: "授权码已过期，请重新扫码",
            20049: (
                "飞书拒绝 PKCE 安全校验（20049）。请关闭旧授权窗口，"
                "从系统监控重新发起绑定；诊断已保留，不会绕过安全校验"
            ),
            20071: "飞书回调地址不匹配，请检查应用安全设置",
            230002: "机器人未启用或未发布",
            230013: "无发送权限，请检查机器人能力和应用可用范围",
            230027: "缺少机器人发消息权限，请开通 im:message:send_as_bot",
            230035: "飞书禁止发送消息，请检查接收人、机器人屏蔽和应用权限",
            230053: "接收人已停止接收机器人消息",
        }
        token_expired = code in {99991661, 99991663, 99991664, 99991668}
        if token_expired:
            self._token_until = 0
        safe_code = str(code) if isinstance(code, int) else "unknown"
        request_id = response.headers.get("x-tt-logid", "")
        if not re.fullmatch(r"[0-9]{14}[0-9A-Fa-f]{16,64}", request_id):
            request_id = None
        raise FeishuError(
            messages.get(code, f"飞书调用失败（HTTP {response.status_code}，错误码 {safe_code}）"),
            retryable=response.status_code in {408, 429}
            or response.status_code >= 500
            or token_expired,
            code=code if 0 <= code <= 2_147_483_647 else None,
            http_status=response.status_code if 100 <= response.status_code <= 599 else None,
            request_id=request_id,
        )

    def _bot_token(self) -> str:
        if time.time() >= self._token_until:
            data = self._request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config["app_id"],
                    "client_secret": self._config["app_secret"],
                    "scope": "im:message:send_as_bot",
                },
            )
            token, expiry = data.get("access_token"), _number(data.get("expires_in"))
            if not isinstance(token, str) or not token or not expiry or expiry <= 0:
                raise FeishuError("飞书未返回有效机器人凭证")
            self._token, self._token_until = token, time.time() + max(1, expiry - 60)
        return self._token

    def _send(self, open_id: str, text: str, event_id: str) -> None:
        self._request(
            "POST",
            MESSAGE_URL,
            params={"receive_id_type": "open_id"},
            headers={"Authorization": f"Bearer {self._bot_token()}"},
            json={
                "receive_id": open_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": event_id,
            },
        )

    def _event(
        self,
        connection: Any,
        *,
        kind: str,
        node: str,
        label: str,
        metric: str,
        value: float | None,
        text: str,
        now: float,
    ) -> str:
        event_id = str(uuid4())
        connection.execute(
            "INSERT INTO system_alert_events "
            "(id,kind,node_id,node_label,metric,value,text,created_at,generation,"
            "delivery,attempts,next_attempt) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?)",
            (
                event_id,
                kind,
                node,
                label,
                metric,
                value,
                text,
                now,
                self._config["generation"],
                now,
            ),
        )
        return event_id

    def test_message(self) -> dict[str, Any]:
        with self._lock:
            binding = self._binding()
            if not self._configured() or not binding:
                raise FeishuError("请先保存应用配置并扫码绑定接收人")
            now = time.time()
            with self.database.connect() as connection:
                recent = connection.execute(
                    "SELECT MAX(created_at) FROM system_alert_events WHERE kind='test'"
                ).fetchone()[0]
                if recent is not None and now - recent < 30:
                    raise FeishuError("测试消息请至少间隔 30 秒")
                event_id = self._event(
                    connection,
                    kind="test",
                    node="local",
                    label="本机",
                    metric="test",
                    value=None,
                    text="[Uniform 系统监控] 飞书机器人私聊测试。收到此消息表示通知通道可用；"
                    "只有你在平台开启告警后，系统才会自动发送资源告警。",
                    now=now,
                )
                connection.commit()
            self._deliver(event_id, binding["open_id"], now, retry=False)
            with self.database.connect() as connection:
                connection.execute("UPDATE feishu_recipient SET verified_at=? WHERE id=1", (now,))
                connection.commit()
            return self.status()

    def events(self, limit: int = 100) -> dict[str, Any]:
        with self.database.connect() as connection:
            items = connection.execute(
                "SELECT id,kind,node_id,node_label,metric,value,created_at,delivery,attempts,error "
                "FROM system_alert_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return {"items": [dict(row) for row in items]}

    def _deliver(self, event_id: str, open_id: str, now: float, *, retry: bool = True) -> None:
        with self.database.connect() as connection:
            event = connection.execute(
                "SELECT * FROM system_alert_events WHERE id=?", (event_id,)
            ).fetchone()
        if not event or event["delivery"] != "pending":
            return
        attempt = event["attempts"] + 1
        try:
            self._send(open_id, event["text"], event_id)
            delivery, error = "sent", None
            self.last_error = None
        except FeishuError as failure:
            error = str(failure)
            delivery = "pending" if retry and failure.retryable and attempt < 3 else "failed"
            self.last_error = error
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE system_alert_events SET delivery=?,attempts=?,error=?,next_attempt=? "
                "WHERE id=?",
                (delivery, attempt, error, now + 30 * 2 ** (attempt - 1), event_id),
            )
            connection.commit()
        if not retry and error:
            raise FeishuError(error)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="uniform-feishu-alerts", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=35)
        if not self._thread or not self._thread.is_alive():
            self._client.close()

    def _loop(self) -> None:
        while not self._stop.wait(5):
            try:
                self.tick()
            except Exception:  # Service boundary: do not expose response bodies or credentials.
                self.last_error = "告警检查失败，请检查监控采样及本地存储"

    def tick(
        self,
        *,
        now: float | None = None,
        local: dict[str, Any] | None = None,
        remote: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            now = time.time() if now is None else now
            self.last_check_at = now
            binding = self._binding()
            if (
                not self._config["enabled"]
                or not self._configured()
                or not binding
                or not binding["verified_at"]
            ):
                return
            local = self.metrics.snapshot() if local is None else local
            remote = self.cluster.snapshot()["items"] if remote is None else remote
            sources = [("local", "本机", local, _number(local.get("timestamp")), False)]
            if self._config["include_remote_nodes"]:
                for node in remote:
                    if node["id"] != "local":
                        sources.append(
                            (
                                node["id"],
                                node.get("label") or node["id"],
                                node,
                                _number(node.get("observed_at")),
                                True,
                            )
                        )
            seen = set()
            with self.database.connect() as connection:
                for node_id, label, sample, observed, is_remote in sources:
                    for rule in self._config["rules"]:
                        if not rule["enabled"] or (rule["metric"] == "offline" and not is_remote):
                            continue
                        metric = rule["metric"]
                        seen.add((node_id, metric))
                        self._evaluate(
                            connection, node_id, label, sample, observed, is_remote, rule, now
                        )
                rows = connection.execute(
                    "SELECT node_id,metric FROM system_alert_state"
                ).fetchall()
                for row in rows:
                    if (row["node_id"], row["metric"]) not in seen:
                        connection.execute(
                            "DELETE FROM system_alert_state WHERE node_id=? AND metric=?",
                            (row["node_id"], row["metric"]),
                        )
                        connection.execute(
                            "UPDATE system_alert_events SET delivery='cancelled',error=? "
                            "WHERE delivery='pending' AND node_id=? AND metric=?",
                            ("节点或规则已移除", row["node_id"], row["metric"]),
                        )
                connection.commit()
                pending = connection.execute(
                    "SELECT id FROM system_alert_events WHERE delivery='pending' AND kind!='test' "
                    "AND next_attempt<=? AND generation=? ORDER BY created_at LIMIT 5",
                    (now, self._config["generation"]),
                ).fetchall()
            for row in pending:
                if self._stop.is_set():
                    break
                self._deliver(row["id"], binding["open_id"], now)

    def _evaluate(
        self,
        connection: Any,
        node_id: str,
        label: str,
        sample: dict[str, Any],
        observed: float | None,
        is_remote: bool,
        rule: dict[str, Any],
        now: float,
    ) -> None:
        metric = rule["metric"]
        row = connection.execute(
            "SELECT state_json FROM system_alert_state WHERE node_id=? AND metric=?",
            (node_id, metric),
        ).fetchone()
        state = (
            json.loads(row[0])
            if row
            else {
                "active": False,
                "pending_at": None,
                "last_alert_at": None,
                "last_observed": None,
            }
        )
        fresh = observed is not None and 0 <= now - observed <= 30
        value = None
        if metric == "offline":
            # The cluster service records an observation time for failed probes too.
            value = 0.0 if sample.get("online") else 1.0
        elif not is_remote or sample.get("online"):
            paths = {
                "gpu_temperature": ("gpu", "temperature_c"),
                "memory": ("memory" if is_remote else "unified_memory", "percent"),
                "disk": ("disk", "percent"),
                "cpu": ("cpu", "utilization_percent" if is_remote else "percent"),
                "gpu": ("gpu", "utilization_percent"),
                "swap": ("swap", "percent"),
            }
            group, field = paths[metric]
            if group != "gpu" or sample.get("gpu", {}).get("available"):
                value = _number(sample.get(group, {}).get(field))
        if not fresh or value is None:
            state["pending_at"] = None
            self._state(connection, node_id, metric, state)
            return  # Unknown or stale data never acts as zero or causes a false recovery.
        if state["last_observed"] is not None and observed <= state["last_observed"]:
            return
        if state["last_observed"] is not None and observed - state["last_observed"] > 30:
            state["pending_at"] = None
        state["last_observed"] = observed
        above = value >= (1 if metric == "offline" else rule["threshold"])
        recovered = value <= (0 if metric == "offline" else rule["recovery"])
        if state["active"]:
            if recovered:
                state.update(active=False, pending_at=None)
                connection.execute(
                    "UPDATE system_alert_events SET delivery='cancelled',error=? "
                    "WHERE delivery='pending' AND node_id=? AND metric=? "
                    "AND kind IN ('alert','reminder')",
                    ("指标已恢复，旧告警不再发送", node_id, metric),
                )
                if self._config["notify_recovery"]:
                    self._alert(connection, "recovery", node_id, label, metric, value, rule, now)
            elif above and now - state["last_alert_at"] >= rule["cooldown_seconds"]:
                self._alert(connection, "reminder", node_id, label, metric, value, rule, now)
                state["last_alert_at"] = now
        elif above:
            if state["pending_at"] is None:
                state["pending_at"] = observed
            if observed - state["pending_at"] >= rule["duration_seconds"]:
                state.update(active=True, last_alert_at=now, pending_at=None)
                self._alert(connection, "alert", node_id, label, metric, value, rule, now)
        else:
            state["pending_at"] = None
        self._state(connection, node_id, metric, state)

    @staticmethod
    def _state(connection: Any, node_id: str, metric: str, state: dict[str, Any]) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO system_alert_state VALUES(?,?,?)",
            (node_id, metric, json.dumps(state)),
        )

    def _alert(
        self,
        connection: Any,
        kind: str,
        node: str,
        label: str,
        metric: str,
        value: float,
        rule: dict[str, Any],
        now: float,
    ) -> None:
        label = str(label)[:128].replace("<", "＜").replace(">", "＞")
        unit = "°C" if metric == "gpu_temperature" else "%"
        detail = ("离线" if value else "恢复在线") if metric == "offline" else f"{value:g}{unit}"
        threshold = (
            "节点探测失败"
            if metric == "offline"
            else f"告警 ≥ {rule['threshold']:g}{unit}；恢复 ≤ {rule['recovery']:g}{unit}"
        )
        title = {"alert": "资源告警", "reminder": "告警持续", "recovery": "告警恢复"}[kind]
        stamp = datetime.fromtimestamp(now, UTC).astimezone().isoformat(timespec="seconds")
        text = (
            f"[Uniform 系统监控] {title}\n节点：{label}\n指标：{METRIC_LABELS[metric]}"
            f"\n当前：{detail}\n条件：{threshold}"
            f"\n持续确认：{rule['duration_seconds']} 秒\n时间：{stamp}"
        )
        self._event(
            connection,
            kind=kind,
            node=node,
            label=label,
            metric=metric,
            value=value,
            text=text,
            now=now,
        )
