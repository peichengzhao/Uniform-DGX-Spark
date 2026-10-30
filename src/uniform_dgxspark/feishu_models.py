from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

CALLBACK_PATH = "/integrations/feishu/callback"
MetricName = Literal["gpu_temperature", "memory", "disk", "cpu", "gpu", "swap", "offline"]
METRIC_LABELS = {
    "gpu_temperature": "GPU 温度",
    "memory": "统一内存占用",
    "disk": "磁盘占用",
    "cpu": "CPU 使用率",
    "gpu": "GPU 使用率",
    "swap": "Swap 使用率",
    "offline": "节点离线",
}


class FeishuBindingChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    authorization_parameters_valid: bool | None = None
    state_valid: bool | None = None
    browser_nonce_valid: bool | None = None
    pkce_verifier_valid: bool | None = None
    pkce_challenge_matches: bool | None = None
    redirect_uri_matches: bool | None = None


class FeishuBindingDiagnostic(BaseModel):
    """An explicit allowlist: never store callback URLs, credentials or provider bodies."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    attempt_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    flow: Literal["v3_pkce_s256"] = "v3_pkce_s256"
    stage: Literal["authorization", "callback", "user_token", "user_info", "complete"]
    result: Literal["pending", "failed", "success"]
    checked_at: float
    reason: (
        Literal[
            "expired",
            "browser_mismatch",
            "local_pkce_mismatch",
            "configuration_changed",
            "invalid_code",
            "user_denied",
            "provider_failed",
            "invalid_provider_response",
            "storage_failed",
        ]
        | None
    ) = None
    error_code: int | None = Field(default=None, ge=0, le=2_147_483_647)
    http_status: int | None = Field(default=None, ge=100, le=599)
    request_id: str | None = Field(default=None, pattern=r"^[0-9]{14}[0-9A-Fa-f]{16,64}$")
    checks: FeishuBindingChecks = Field(default_factory=FeishuBindingChecks)


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    metric: MetricName
    enabled: bool = True
    threshold: float = Field(default=90, ge=0, le=150)
    recovery: float = Field(default=85, ge=0, le=150)
    duration_seconds: int = Field(default=60, ge=10, le=86400)
    cooldown_seconds: int = Field(default=600, ge=60, le=86400)

    @model_validator(mode="after")
    def check_thresholds(self) -> AlertRule:
        if self.metric != "offline":
            if self.recovery >= self.threshold:
                raise ValueError("恢复阈值必须小于告警阈值")
            if self.metric != "gpu_temperature" and self.threshold > 100:
                raise ValueError("资源占比阈值不能超过 100%")
        return self


def default_rules() -> list[AlertRule]:
    return [
        AlertRule(metric="gpu_temperature", threshold=85, recovery=80),
        AlertRule(metric="memory"),
        AlertRule(metric="disk", duration_seconds=120),
        AlertRule(metric="offline"),
        AlertRule(metric="cpu", enabled=False, threshold=95, duration_seconds=120),
        AlertRule(metric="gpu", enabled=False, threshold=99, recovery=90, duration_seconds=180),
        AlertRule(metric="swap", enabled=False, threshold=50, recovery=30),
    ]


class FeishuConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    app_id: str = Field(default="", max_length=128, pattern=r"^(cli_[A-Za-z0-9_-]+)?$")
    app_secret: SecretStr | None = None
    redirect_uri: str = Field(default="", max_length=2048)
    enabled: bool = False
    notify_recovery: bool = True
    include_remote_nodes: bool = True
    rules: list[AlertRule] = Field(default_factory=default_rules, min_length=1, max_length=7)

    @model_validator(mode="after")
    def check_configuration(self) -> FeishuConfigUpdate:
        if len({rule.metric for rule in self.rules}) != len(self.rules):
            raise ValueError("同一指标只能配置一条规则")
        if self.redirect_uri:
            uri = urlsplit(self.redirect_uri)
            _ = uri.port
            if (
                not uri.hostname
                or uri.username
                or uri.password
                or uri.query
                or uri.fragment
                or uri.path != CALLBACK_PATH
                or not (
                    uri.scheme == "https"
                    or (uri.scheme == "http" and uri.hostname in {"127.0.0.1", "localhost", "::1"})
                )
            ):
                raise ValueError("回调地址必须是 HTTPS 或本机 HTTP，并使用固定的飞书回调路径")
        return self
