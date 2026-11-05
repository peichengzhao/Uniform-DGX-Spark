from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .state import DeploymentStatus


class DeploymentParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=8000, ge=1, le=65535)
    source: Literal["huggingface", "modelscope"] = "huggingface"
    image: str = Field(default="nvcr.io/nvidia/vllm:26.07-py3", min_length=1)
    served_model_name: str | None = Field(default=None, min_length=1, max_length=255)
    local_model_path: str | None = None
    max_model_len: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float = Field(default=0.9, gt=0, le=1)
    startup_timeout_seconds: int | None = Field(default=None, ge=10, le=7200)
    trust_remote_code: bool = False
    remote_code_acknowledged: bool = False
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("local_model_path")
    @classmethod
    def validate_local_model_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("local_model_path must be an absolute path")
        return normalized

    @field_validator("extra_args")
    @classmethod
    def validate_extra_args(cls, values: list[str]) -> list[str]:
        reserved = {
            "--host",
            "--port",
            "--model",
            "--served-model-name",
            "--download-dir",
            "--trust-remote-code",
        }
        for value in values:
            flag = value.strip().split(maxsplit=1)[0]
            if not flag.startswith("--"):
                raise ValueError("each extra argument must start with --")
            if flag in reserved:
                raise ValueError(f"{flag} is managed by the platform")
        return values

    def model_post_init(self, __context: Any) -> None:
        if self.trust_remote_code and not self.remote_code_acknowledged:
            raise ValueError(
                "remote_code_acknowledged must be true when trust_remote_code is enabled"
            )


class DeploymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1, max_length=255)
    revision: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    parameters: DeploymentParameters = Field(default_factory=DeploymentParameters)

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("model_id must not contain whitespace")
        return normalized


class DeploymentRead(BaseModel):
    id: str
    model_id: str
    revision: str | None
    display_name: str | None
    parameters: DeploymentParameters
    status: DeploymentStatus
    progress: float
    error_code: str | None
    error_message: str | None
    attempt: int
    version: int
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DeploymentList(BaseModel):
    items: list[DeploymentRead]
    total: int


class DeploymentEventRead(BaseModel):
    id: int
    deployment_id: str
    sequence: int
    event_type: str
    from_status: DeploymentStatus | None
    to_status: DeploymentStatus | None
    message: str
    details: dict[str, Any]
    created_at: datetime


class DeploymentEventList(BaseModel):
    items: list[DeploymentEventRead]


class HealthRead(BaseModel):
    status: str
    database: str
    recovered_tasks: int


class CatalogSourceRead(BaseModel):
    source: Literal["modelscope", "huggingface"]
    model_id: str
    revision: str | None
    downloads: int
    likes: int


class CatalogPublisherRead(BaseModel):
    id: str
    name: str
    icon_url: str


class CatalogModelRead(BaseModel):
    source: str
    model_id: str
    revision: str | None
    author: str | None
    pipeline_tag: str | None
    library_name: str | None
    tags: list[str]
    downloads: int
    likes: int
    last_modified: datetime | None
    created_at: datetime | None = None
    gated: bool
    private: bool
    compatibility: str
    compatibility_reason: str
    metadata: dict[str, Any]
    refreshed_at: datetime
    popularity_rank: int | None = None
    sources: list[CatalogSourceRead] = Field(default_factory=list)
    publisher: CatalogPublisherRead | None = None


class CatalogModelList(BaseModel):
    items: list[CatalogModelRead]
    total: int


class CatalogRefreshRead(BaseModel):
    source: str
    refreshed: int
    sources: dict[str, dict[str, object]] = Field(default_factory=dict)
    finished_at: datetime


class CatalogRemoteSearchRead(BaseModel):
    items: list[CatalogModelRead]
    total: int
    live: bool
    source_errors: dict[str, str] = Field(default_factory=dict)


class BenchmarkIOShape(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=1, le=1_048_576)
    output_tokens: int = Field(ge=1, le=131_072)


class BenchmarkExecutionPlan(BaseModel):
    """Optional user constraints for an AI-driven benchmark.

    Empty collections, ``None`` values, and ``reference`` enum values deliberately mean that the
    benchmark agent should inherit the selected reference implementation's defaults.
    """

    model_config = ConfigDict(extra="forbid")

    io_shapes: list[BenchmarkIOShape] = Field(default_factory=list, max_length=64)
    concurrencies: list[int] = Field(default_factory=list, max_length=64)
    requests_per_case: int | None = Field(default=None, ge=1, le=1_000_000)
    runs_per_case: int | None = Field(default=None, ge=1, le=10_000)
    warmup_requests: int | None = Field(default=None, ge=0, le=100_000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86_400)
    endpoint: Literal["reference", "chat_completions", "completions"] = "reference"
    dataset: Literal["reference", "random", "real_text", "specified"] = "reference"
    dataset_path: str | None = Field(default=None, max_length=4096)
    require_exact_output: bool | None = None
    prefix_cache: Literal["reference", "current", "enabled", "disabled", "compare"] = "reference"
    metrics: list[
        Literal[
            "ttft",
            "tpot",
            "itl",
            "prefill_throughput",
            "decode_throughput",
            "request_throughput",
            "token_throughput",
            "latency_percentiles",
        ]
    ] = Field(default_factory=list, max_length=8)
    resource_metrics: list[
        Literal["unified_memory", "gpu", "network_rdma", "speculative_acceptance"]
    ] = Field(default_factory=list, max_length=4)
    ab_test_enabled: bool | None = None
    ab_test_dimensions: str | None = Field(default=None, max_length=4000)

    @field_validator("concurrencies")
    @classmethod
    def validate_concurrencies(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 100_000 for value in values):
            raise ValueError("concurrency values must be between 1 and 100000")
        if len(set(values)) != len(values):
            raise ValueError("concurrency values must be unique")
        return values

    @field_validator("metrics", "resource_metrics")
    @classmethod
    def validate_unique_choices(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("metric choices must be unique")
        return values

    @field_validator("dataset_path", "ab_test_dimensions")
    @classmethod
    def normalize_benchmark_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    def model_post_init(self, __context: Any) -> None:
        if self.dataset == "specified" and not self.dataset_path:
            raise ValueError("specified dataset requires dataset_path")
        if self.dataset != "specified" and self.dataset_path:
            raise ValueError("dataset_path is only valid for a specified dataset")
        if self.ab_test_enabled is True and not self.ab_test_dimensions:
            raise ValueError("enabled A/B testing requires deployment parameter dimensions")
        if self.ab_test_enabled is not True and self.ab_test_dimensions:
            raise ValueError("A/B test dimensions require A/B testing to be enabled")


class AIBenchmarkResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    machine: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=512)
    kv_cache_dtype: str | None = Field(default=None, max_length=64)
    engine: str = Field(min_length=1, max_length=128)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    requests: int = Field(ge=1)
    successful_requests: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    total_input_tokens: int = Field(ge=0)
    total_generated_tokens: int = Field(ge=0)
    request_throughput: float | None = Field(
        default=None, ge=0, description="Successful requests divided by case wall-clock duration"
    )
    decode_token_throughput: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Independently measured decode-phase token throughput; null when only whole-case "
            "output throughput is available"
        ),
    )
    output_token_throughput: float | None = Field(
        default=None,
        ge=0,
        description="Successfully generated output tokens divided by case wall-clock duration",
    )
    total_token_throughput: float | None = Field(
        default=None,
        ge=0,
        description="Successful input plus output tokens divided by case wall-clock duration",
    )
    mean_ttft_ms: float | None = Field(default=None, ge=0)
    median_ttft_ms: float | None = Field(default=None, ge=0)
    p99_ttft_ms: float | None = Field(default=None, ge=0)
    mean_tpot_ms: float | None = Field(default=None, ge=0)
    median_tpot_ms: float | None = Field(default=None, ge=0)
    p95_tpot_ms: float | None = Field(default=None, ge=0)
    p99_tpot_ms: float | None = Field(default=None, ge=0)
    mean_itl_ms: float | None = Field(default=None, ge=0)
    prefill_duration_seconds: float | None = Field(
        default=None,
        ge=0,
        description="Delta of the engine's cumulative per-request Prefill phase timer",
    )
    prefill_observation_count: int | None = Field(default=None, ge=0)
    prefill_computed_tokens: int | None = Field(
        default=None,
        ge=0,
        description="New KV tokens actually computed during Prefill, excluding cache hits",
    )
    mean_prefill_ms: float | None = Field(default=None, ge=0)
    median_prefill_ms: float | None = Field(
        default=None, ge=0, description="P50 estimated from native histogram bucket deltas"
    )
    p95_prefill_ms: float | None = Field(
        default=None, ge=0, description="P95 estimated from native histogram bucket deltas"
    )
    p99_prefill_ms: float | None = Field(
        default=None, ge=0, description="P99 estimated from native histogram bucket deltas"
    )
    prefill_token_throughput: float | None = Field(
        default=None,
        ge=0,
        description="Computed Prefill/KV tokens divided by native Prefill duration",
    )
    prefill_measurement_source: str | None = Field(default=None, max_length=512)
    latency_p50_ms: float | None = Field(default=None, ge=0)
    latency_p95_ms: float | None = Field(default=None, ge=0)
    latency_p99_ms: float | None = Field(default=None, ge=0)
    unified_memory_peak_bytes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Peak used DGX Spark unified memory during the measured case; for multi-node runs, "
            "the synchronized sum across sampled nodes"
        ),
    )
    gpu_utilization_peak_percent: float | None = Field(default=None, ge=0, le=100)
    gpu_utilization_peak_by_device_percent: list[float] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "Peak GPU utilization ordered by benchmark node/device; GPU 0 is the local/head "
            "device and GPU 1 is the TP worker/second device"
        ),
    )
    network_rx_peak_bps: float | None = Field(default=None, ge=0)
    network_tx_peak_bps: float | None = Field(default=None, ge=0)
    rdma_rx_peak_bps: float | None = Field(default=None, ge=0)
    rdma_tx_peak_bps: float | None = Field(default=None, ge=0)
    speculative_acceptance_rate: float | None = Field(default=None, ge=0, le=1)

    @field_validator("gpu_utilization_peak_by_device_percent")
    @classmethod
    def validate_gpu_utilization_peaks(cls, values: list[float]) -> list[float]:
        if any(value < 0 or value > 100 for value in values):
            raise ValueError("per-device GPU utilization peaks must be between 0 and 100")
        return values

    def model_post_init(self, __context: Any) -> None:
        if self.successful_requests > self.requests:
            raise ValueError("successful_requests cannot exceed requests")
        if self.duration_seconds > 0:
            self.request_throughput = self.successful_requests / self.duration_seconds
            self.output_token_throughput = self.total_generated_tokens / self.duration_seconds
            self.total_token_throughput = (
                self.total_input_tokens + self.total_generated_tokens
            ) / self.duration_seconds
        if self.prefill_duration_seconds and self.prefill_duration_seconds > 0:
            if self.prefill_observation_count:
                self.mean_prefill_ms = (
                    self.prefill_duration_seconds / self.prefill_observation_count * 1000
                )
            if self.prefill_computed_tokens is not None:
                self.prefill_token_throughput = (
                    self.prefill_computed_tokens / self.prefill_duration_seconds
                )


class AIBenchmarkResultDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[AIBenchmarkResultRecord] = Field(min_length=1, max_length=1000)
    notes: list[str] = Field(default_factory=list, max_length=100)


class AISessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["codex", "claude", "deepseek", "dsh"]
    task_type: Literal[
        "deployment",
        "diagnosis",
        "benchmark_execution",
        "benchmark_analysis",
        "accuracy_evaluation",
        "dataset_integration",
        "service_stop",
        "general",
    ] = "general"
    client_request_id: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    display_name: str | None = Field(default=None, max_length=120)
    ai_model: str | None = Field(default=None, max_length=128)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    execution_mode: Literal["autonomous", "interactive", "full_access"] = "autonomous"
    full_access_acknowledged: bool = False
    deployment_id: str | None = None
    initial_prompt: str = Field(default="", max_length=12_000)
    reference_urls: list[str] = Field(default_factory=list, max_length=5)
    web_search: bool = False
    target_model_id: str | None = Field(default=None, max_length=255)
    target_source: Literal["huggingface", "modelscope"] | None = None
    target_revision: str | None = Field(default=None, max_length=255)
    target_local_path: str | None = Field(default=None, max_length=4096)
    deployment_topology: Literal["auto", "single_node", "multi_node"] = "auto"
    benchmark_target_type: Literal["deployment", "ai_session"] | None = None
    benchmark_target_id: str | None = Field(default=None, min_length=1, max_length=255)
    benchmark_plan: BenchmarkExecutionPlan | None = None
    accuracy_target_type: Literal["deployment", "ai_session"] | None = None
    accuracy_target_id: str | None = Field(default=None, min_length=1, max_length=255)
    accuracy_sample_budget: int = Field(default=100, ge=1, le=10_000)
    accuracy_dataset_name: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$"
    )

    @field_validator("display_name", "ai_model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("ai_model")
    @classmethod
    def validate_ai_model(cls, value: str | None) -> str | None:
        if value and any(character.isspace() for character in value):
            raise ValueError("ai_model must not contain whitespace")
        return value

    @field_validator("target_model_id")
    @classmethod
    def validate_target_model_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("target_model_id must not contain whitespace")
        return normalized

    @field_validator("reference_urls")
    @classmethod
    def validate_reference_urls(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            parsed = urlparse(value.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("reference URLs must use http or https")
            normalized.append(value.strip())
        return normalized

    def model_post_init(self, __context: Any) -> None:
        if self.task_type == "general" and self.target_model_id:
            self.task_type = "deployment"
        elif self.task_type == "general" and self.deployment_id:
            self.task_type = "diagnosis"
        has_remote_target = bool(self.target_model_id and self.target_source)
        has_local_target = bool(self.target_model_id and self.target_local_path)
        if self.target_source and not self.target_model_id:
            raise ValueError("target_source requires a target model")
        if self.target_local_path and not self.target_model_id:
            raise ValueError("target_local_path requires a target model")
        if self.target_model_id and not (self.target_source or self.target_local_path):
            raise ValueError("target model requires a remote source or local path")
        if has_remote_target and has_local_target:
            raise ValueError("remote and local model targets are mutually exclusive")
        if self.target_revision and not self.target_model_id:
            raise ValueError("target_revision requires a target model")
        if self.deployment_id and self.target_model_id:
            raise ValueError("deployment_id and target_model_id are mutually exclusive")
        if self.provider not in {"codex", "dsh"} and self.reasoning_effort:
            raise ValueError("reasoning_effort is only supported for Codex and DSH sessions")
        if self.execution_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access_acknowledged must be true for full_access mode")
        has_benchmark_target = bool(self.benchmark_target_type and self.benchmark_target_id)
        if self.task_type == "benchmark_execution" and not has_benchmark_target:
            raise ValueError("benchmark execution requires a target service")
        if bool(self.benchmark_target_type) != bool(self.benchmark_target_id):
            raise ValueError("benchmark target type and ID must be provided together")
        if self.benchmark_plan is not None and self.task_type != "benchmark_execution":
            raise ValueError("benchmark_plan is only valid for benchmark execution")
        if self.task_type == "accuracy_evaluation":
            if not self.accuracy_target_type or not self.accuracy_target_id:
                raise ValueError("AI accuracy evaluation requires a selected target")
            if self.provider not in {"codex", "dsh"} or self.execution_mode == "full_access":
                raise ValueError("AI accuracy evaluation requires scoped Codex or DSH execution")
            if (
                self.target_model_id
                or self.deployment_id
                or self.benchmark_target_id
                or self.reference_urls
                or self.web_search
            ):
                raise ValueError("AI accuracy cannot request deployment or external references")
        elif self.task_type == "dataset_integration":
            if not self.accuracy_dataset_name:
                raise ValueError("dataset integration requires a selected dataset")
            if bool(self.accuracy_target_type) != bool(self.accuracy_target_id):
                raise ValueError("integration target type and ID must be provided together")
            if self.provider not in {"codex", "dsh"} or self.execution_mode != "interactive":
                raise ValueError(
                    "dataset integration requires scoped interactive Codex or DSH execution"
                )
            if (
                self.target_model_id
                or self.deployment_id
                or self.benchmark_target_id
                or self.reference_urls
                or self.web_search
            ):
                raise ValueError("dataset integration cannot request deployment or external tools")
        elif self.accuracy_target_type or self.accuracy_target_id:
            raise ValueError("accuracy target is only valid for AI accuracy evaluation")
        if self.accuracy_dataset_name and self.task_type != "dataset_integration":
            raise ValueError("dataset binding is only valid for integration")


class AISessionRead(BaseModel):
    id: str
    deployment_id: str | None
    provider: Literal["codex", "claude", "deepseek", "dsh"]
    task_type: Literal[
        "deployment",
        "diagnosis",
        "benchmark_execution",
        "benchmark_analysis",
        "accuracy_evaluation",
        "dataset_integration",
        "service_stop",
        "general",
    ]
    display_name: str
    target_model_id: str | None
    related_session_id: str | None = None
    related_service_name: str | None = None
    ai_model: str | None
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None
    execution_mode: Literal["autonomous", "interactive", "full_access"]
    status: Literal["starting", "running", "exited", "failed", "stopped", "interrupted"]
    initial_prompt: str
    reference_urls: list[str]
    workspace_path: str
    pid: int | None
    exit_code: int | None
    error_message: str | None
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AISessionList(BaseModel):
    items: list[AISessionRead]
    total: int


class AISessionResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(default="继续执行上次任务", min_length=1, max_length=500)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be empty")
        return normalized


class AIConversationMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    text: str
    created_at: datetime | None = None
    phase: str | None = None


class AIConversationRead(BaseModel):
    session_id: str
    provider: Literal["codex", "claude", "deepseek", "dsh"]
    status: Literal["starting", "running", "exited", "failed", "stopped", "interrupted"]
    source: Literal["codex_rollout", "claude_history", "dsh_native", "none"]
    available: bool
    messages: list[AIConversationMessage]
    notice: str | None = None


class AIConversationMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=12_000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be empty")
        return normalized


AI_PROVIDER_LABELS = {
    "codex": "Codex",
    "claude": "Claude Code",
    "deepseek": "DeepSeek Harness",
    "dsh": "DSH 原生会话",
}


class AIProviderRead(BaseModel):
    provider: Literal["codex", "claude", "deepseek", "dsh"]
    available: bool
    executable: str | None
    version: str | None


class AIAutoResumeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_retries: int = Field(default=5, ge=1, le=50)
    base_delay_seconds: int = Field(default=15, ge=1, le=300)
    prompt: str = Field(default="继续执行上次任务", min_length=1, max_length=500)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be empty")
        return normalized


class AIAutoResumeRead(BaseModel):
    session_id: str
    enabled: bool
    max_retries: int
    retry_count: int
    base_delay_seconds: int
    prompt: str
    state: Literal["disabled", "watching", "waiting", "sending", "exhausted", "ended"]
    next_retry_at: datetime | None = None
    last_reason: str | None = None


class AIAuditEventRead(BaseModel):
    id: int
    session_id: str
    event_type: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class AIAuditEventList(BaseModel):
    items: list[AIAuditEventRead]


class AIHealthCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(default="GET", min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=2048)
    status_code: int | None = Field(default=None, ge=100, le=599)
    passed: bool
    details: str = Field(default="", max_length=8000)


class AIInferenceTestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: str = Field(default="POST", min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=2048)
    status_code: int | None = Field(default=None, ge=100, le=599)
    passed: bool
    request_summary: str = Field(default="", max_length=8000)
    response_summary: str = Field(default="", max_length=8000)


class AIDeploymentResultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str = Field(min_length=1, max_length=255)
    container_name: str = Field(min_length=1, max_length=255)
    container_id: str | None = Field(default=None, max_length=255)
    image: str = Field(min_length=1, max_length=1024)
    port: int = Field(ge=1, le=65535)
    service_url: str = Field(min_length=1, max_length=2048)
    launch_command: str = Field(min_length=1, max_length=20_000)
    health_check: AIHealthCheckResult
    inference_test: AIInferenceTestResult


class AIDeploymentResultRead(AIDeploymentResultCreate):
    session_id: str
    status: Literal["verified", "failed"]
    created_at: datetime
    updated_at: datetime


class AIDeploymentTemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: int = Field(default=1, ge=1)
    id: str = Field(min_length=1, max_length=255)
    source_session_id: str | None = Field(default=None, max_length=255)
    source_deployment_id: str | None = Field(default=None, max_length=255)
    model_id: str = Field(min_length=1, max_length=255)
    target_source: Literal["huggingface", "modelscope", "local", "unknown"]
    target_revision: str | None = Field(default=None, max_length=255)
    target_local_path: str | None = Field(default=None, max_length=4096)
    deployment_topology: Literal["single_node", "multi_node"]
    engine: str = Field(min_length=1, max_length=255)
    image: str = Field(min_length=1, max_length=1024)
    port: int = Field(ge=1, le=65535)
    container_name: str = Field(min_length=1, max_length=255)
    inference_endpoint: Literal["chat_completions", "completions"] = "completions"
    reuse_mode: Literal["managed", "direct_script", "ai_session"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    reference_urls: list[str] = Field(default_factory=list, max_length=5)
    launch_command_template: str = Field(min_length=1, max_length=20_000)
    script_path: str = Field(min_length=1, max_length=4096)
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stop_script_path: str | None = Field(default=None, max_length=4096)
    stop_script_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    container_id: str | None = Field(default=None, max_length=255)
    artifact_verification_status: Literal[
        "legacy_unverified", "pending", "verifying", "verified", "failed", "not_required"
    ] = "legacy_unverified"
    artifact_verification_error: str | None = Field(default=None, max_length=8000)
    artifact_verified_at: datetime | None = None
    requires_secret_values: bool = False


class AIDeploymentTemplateRead(AIDeploymentTemplateCreate):
    created_at: datetime
    updated_at: datetime


class AIDeploymentReuseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    model_id: str = Field(min_length=1, max_length=255)
    target_source: Literal["huggingface", "modelscope", "local"]
    target_revision: str | None = Field(default=None, max_length=255)
    target_local_path: str | None = Field(default=None, max_length=4096)
    deployment_topology: Literal["single_node", "multi_node"] = "single_node"
    provider: Literal["codex", "claude", "deepseek", "dsh"] = "codex"
    display_name: str | None = Field(default=None, max_length=120)
    ai_model: str | None = Field(default=None, max_length=128)
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    execution_mode: Literal["autonomous", "interactive", "full_access"] = "autonomous"
    full_access_acknowledged: bool = False

    @field_validator("model_id")
    @classmethod
    def normalize_reuse_model_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("model_id must not contain whitespace")
        return normalized

    def model_post_init(self, __context: Any) -> None:
        if self.target_source == "local":
            if not self.target_local_path or not self.target_local_path.startswith("/"):
                raise ValueError("local reuse requires an absolute target_local_path")
        elif self.target_local_path:
            raise ValueError("target_local_path is only valid for a local model")
        if self.provider not in {"codex", "dsh"} and self.reasoning_effort:
            raise ValueError("reasoning_effort is only supported for Codex and DSH sessions")
        if self.execution_mode == "full_access" and not self.full_access_acknowledged:
            raise ValueError("full_access_acknowledged must be true for full_access mode")


class AIDeploymentReuseRead(BaseModel):
    reuse_method: Literal[
        "running_service",
        "managed_deployment",
        "direct_script",
        "source_ai_session",
        "new_ai_session",
    ]
    fallback_to_ai: bool
    message: str
    template: AIDeploymentTemplateRead | None = None
    deployment: DeploymentRead | None = None
    session: AISessionRead | None = None


class AIDeployedServiceRead(BaseModel):
    session_id: str
    linked_deployment_id: str | None
    display_name: str
    target_model_id: str | None
    engine: str
    container_name: str
    image: str
    port: int
    service_url: str
    launch_command: str
    result_status: Literal["verified", "failed"]
    runtime_status: Literal["running", "unhealthy", "stopped", "verification_failed"]
    ai_session_status: Literal["starting", "running", "exited", "failed", "stopped", "interrupted"]
    health_reachable: bool
    container_running: bool
    stop_status: Literal[
        "idle", "direct_script", "original_session", "fallback_session", "completed", "failed"
    ] = "idle"
    stop_message: str | None = None
    stop_session_id: str | None = None
    archived_at: datetime | None = None
    updated_at: datetime


class AIDeployedServiceList(BaseModel):
    items: list[AIDeployedServiceRead]
    total: int


class ServicePublicationRead(BaseModel):
    target_type: Literal["deployment", "ai_session"]
    target_id: str
    model_id: str
    display_name: str
    publication_id: str | None
    enabled: bool
    base_url: str | None
    key_hint: str | None
    gateway_running: bool
    gateway_error: str | None
    service_running: bool
    external_access_enabled: bool = False
    external_url: str | None = None
    external_access_state: Literal["stopped", "starting", "running", "error"] = "stopped"
    external_access_error: str | None = None
    external_access_mode: Literal["quick", "named"] = "quick"
    external_health_checked_at: str | None = None
    external_last_healthy_at: str | None = None
    external_consecutive_health_failures: int = 0
    external_restart_count: int = 0
    external_next_retry_at: str | None = None
    created_at: datetime | None
    updated_at: datetime | None


class ServicePublicationSecret(ServicePublicationRead):
    api_key: str


class GatewayConsumerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


class GatewayTokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    publication_ids: list[str] = Field(min_length=1, max_length=100)
    token_limit: int | None = Field(default=None, ge=1, le=10**15)
    expires_at: datetime | None = None


class GatewayTokenRead(BaseModel):
    token_id: str
    consumer_id: str
    name: str
    key_hint: str
    enabled: bool
    token_limit: int | None
    used_tokens: int
    reserved_tokens: int = 0
    remaining_tokens: int | None
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    publication_ids: list[str]
    model_ids: list[str]


class GatewayTokenSecret(GatewayTokenRead):
    api_key: str


class GatewayConsumerRead(BaseModel):
    consumer_id: str
    name: str
    note: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    tokens: list[GatewayTokenRead] = Field(default_factory=list)


class GatewayConsumerList(BaseModel):
    items: list[GatewayConsumerRead]
    total: int


class GatewayUsageRead(BaseModel):
    id: str
    token_id: str | None
    consumer_id: str | None
    consumer_name: str
    token_name: str
    publication_id: str | None
    publication_label: str | None
    target_type: str | None
    target_id: str | None
    model: str | None
    endpoint: str
    method: str
    status_code: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    usage_source: str
    latency_ms: int
    request_id: str | None
    created_at: datetime


class GatewayUsageSummary(BaseModel):
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    unknown_usage: int


class GatewayUsagePage(BaseModel):
    items: list[GatewayUsageRead]
    total: int
    summary: GatewayUsageSummary


class BenchmarkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["deployment", "ai_session"]
    target_id: str = Field(min_length=1, max_length=255)
    endpoint: Literal["chat_completions", "completions"] = "chat_completions"
    input_tokens: int = Field(default=512, ge=1, le=131_072)
    output_tokens: int = Field(default=128, ge=1, le=8192)
    concurrency: int = Field(default=1, ge=1, le=64)
    total_requests: int = Field(default=10, ge=1, le=1000)
    warmup_requests: int = Field(default=1, ge=0, le=10)
    timeout_seconds: int = Field(default=300, ge=5, le=600)


class BenchmarkMetrics(BaseModel):
    duration_seconds: float
    success_rate: float
    request_throughput: float
    output_token_throughput: float
    total_token_throughput: float
    prompt_tokens: int
    completion_tokens: int
    average_prompt_tokens: float
    average_completion_tokens: float
    latency_average_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    errors: list[str] = Field(default_factory=list)


class BenchmarkRead(BaseModel):
    id: str
    target_type: Literal["deployment", "ai_session"]
    target_id: str
    target_label: str
    model_name: str
    service_url: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    parameters: BenchmarkCreate
    progress: float
    completed_requests: int
    successful_requests: int
    failed_requests: int
    metrics: BenchmarkMetrics | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class BenchmarkList(BaseModel):
    items: list[BenchmarkRead]
    total: int


AccuracyRunStatus = Literal[
    "queued",
    "preparing",
    "running",
    "parsing",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "interrupted",
]


class AccuracyDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    subset_list: list[str] = Field(min_length=1, max_length=128)
    limit: int = Field(ge=1, le=10_000)
    few_shot_num: int = Field(default=0, ge=0, le=16)

    @field_validator("subset_list")
    @classmethod
    def validate_subset_list(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(
            not value
            or len(value) > 128
            or not all(character.isalnum() or character in "_-" for character in value)
            for value in normalized
        ):
            raise ValueError(
                "subset names must contain only letters, numbers, underscores, or hyphens"
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("subset names must be unique")
        return normalized


class AccuracyGenerationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    top_p: float = Field(default=1, gt=0, le=1)


class AccuracyRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["deployment", "ai_session"]
    target_id: str = Field(min_length=1, max_length=255)
    preset: Literal[
        "quick-objective-v1",
        "balanced-objective-v1",
        "formal-objective-v1",
        "custom-objective-v1",
    ] = "quick-objective-v1"
    datasets: list[AccuracyDatasetRequest] = Field(min_length=1, max_length=8)
    eval_batch_size: int = Field(default=1, ge=1, le=8)
    timeout_seconds: int = Field(default=300, ge=5, le=600)
    seed: int = Field(default=42, ge=-(2**31), le=(2**31) - 1)
    generation_config: AccuracyGenerationConfig = Field(default_factory=AccuracyGenerationConfig)
    use_cache: bool = False
    cache_source_run_id: str | None = Field(default=None, min_length=1, max_length=255)

    def model_post_init(self, __context: Any) -> None:
        if self.use_cache != (self.cache_source_run_id is not None):
            raise ValueError("use_cache requires an explicit cache_source_run_id, and vice versa")

    @field_validator("target_id")
    @classmethod
    def validate_target_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("target_id must not be blank")
        return normalized

    @field_validator("datasets")
    @classmethod
    def validate_datasets(
        cls, values: list[AccuracyDatasetRequest]
    ) -> list[AccuracyDatasetRequest]:
        names = [value.name for value in values]
        if len(names) != len(set(names)):
            raise ValueError("dataset names must be unique")
        return values


class AccuracyResolvedDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_-]*$")
    dataset_id: str = Field(min_length=1, max_length=512)
    dataset_hub: Literal["modelscope", "huggingface"]
    dataset_revision: str | None = Field(default=None, max_length=255)
    content_hash: str | None = Field(default=None, max_length=255)
    evaluation_version: str = Field(min_length=1, max_length=64)
    subset_list: list[str] = Field(min_length=1, max_length=128)
    limit: int = Field(ge=1, le=10_000)
    few_shot_num: int = Field(ge=0, le=16)
    prompt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AccuracyResolvedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    datasets: list[AccuracyResolvedDataset] = Field(min_length=1, max_length=8)
    generation_config: AccuracyGenerationConfig
    eval_batch_size: int = Field(ge=1, le=8)
    timeout_seconds: int = Field(ge=5, le=600)
    seed: int = Field(ge=-(2**31), le=(2**31) - 1)
    deployment_snapshot: dict[str, Any]


class AccuracyRunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    phase: AccuracyRunStatus = "queued"
    processed_samples: int = Field(default=0, ge=0)
    total_samples: int = Field(default=0, ge=0)
    percent: float = Field(default=0, ge=0, le=100)
    current_dataset: str | None = Field(default=None, max_length=128)
    message: str | None = Field(default=None, max_length=1000)

    def model_post_init(self, __context: Any) -> None:
        if self.processed_samples > self.total_samples:
            raise ValueError("processed_samples cannot exceed total_samples")


class AccuracyRunError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=4000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class AccuracyMetricRead(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    dataset_name: str = Field(min_length=1, max_length=128)
    dataset_pretty_name: str = Field(min_length=1, max_length=255)
    evaluation_version: str = Field(min_length=1, max_length=64)
    metric_key: str = Field(min_length=1, max_length=255)
    metric_display_name: str = Field(min_length=1, max_length=255)
    metric_kind: Literal["quality", "diagnostic"]
    higher_is_better: bool
    category_path: list[str] = Field(default_factory=list, max_length=16)
    subset_name: str = Field(min_length=1, max_length=128)
    sample_count: int = Field(ge=0)
    score: float
    is_primary: bool = False
    execution_requested: int = Field(ge=0)
    execution_succeeded: int = Field(ge=0)
    execution_errored: int = Field(ge=0)

    def model_post_init(self, __context: Any) -> None:
        if self.execution_succeeded + self.execution_errored > self.execution_requested:
            raise ValueError("execution counts cannot exceed execution_requested")


class AccuracyRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_count: int = Field(ge=1)
    metric_count: int = Field(ge=1)
    primary_metric_count: int = Field(ge=0)
    execution_requested: int = Field(ge=0)
    execution_succeeded: int = Field(ge=0)
    execution_errored: int = Field(ge=0)
    incomplete: bool = False

    def model_post_init(self, __context: Any) -> None:
        if self.primary_metric_count > self.metric_count:
            raise ValueError("primary_metric_count cannot exceed metric_count")
        if self.execution_succeeded + self.execution_errored > self.execution_requested:
            raise ValueError("execution counts cannot exceed execution_requested")


class AccuracyRunRead(BaseModel):
    id: str
    target_type: Literal["deployment", "ai_session"]
    target_id: str
    target_label: str
    model_name: str
    service_url: str
    status: AccuracyRunStatus
    request: AccuracyRunCreate
    resolved_config: AccuracyResolvedConfig
    progress: AccuracyRunProgress
    summary: AccuracyRunSummary | None
    metrics: list[AccuracyMetricRead] = Field(default_factory=list)
    evalscope_version: str
    evalscope_commit: str | None
    run_directory: str
    pid: int | None
    error: AccuracyRunError | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    evaluation_fingerprint: str | None = None
    cache_fingerprint: str | None = None
    cached_samples: int = Field(default=0, ge=0)


class AccuracyCompareCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(min_length=2, max_length=4)

    @field_validator("run_ids")
    @classmethod
    def unique_runs(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("comparison requires distinct run IDs")
        return values


class AccuracyComparisonRead(BaseModel):
    evaluation_fingerprint: str
    items: list[AccuracyRunRead]


class AccuracyRunList(BaseModel):
    items: list[AccuracyRunRead]
    total: int
    creation_enabled: bool = True


class AccuracyLogRead(BaseModel):
    logs: str
    size_bytes: int = Field(ge=0)
    truncated: bool


class AccuracyArtifactRead(BaseModel):
    path: str
    size_bytes: int = Field(ge=0)
    media_type: Literal["application/json"] = "application/json"


class AccuracyArtifactList(BaseModel):
    items: list[AccuracyArtifactRead]
    total: int


class ArenaRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["deployment", "ai_session"]
    target_id: str = Field(min_length=1, max_length=255)
    report_model_name: str | None = Field(default=None, max_length=255)
    served_model_name: str | None = Field(default=None, max_length=255)
    tokenizer: str = Field(min_length=1, max_length=4096)
    pp: int = Field(default=2048, ge=1, le=131_072)
    tg: int = Field(default=128, ge=1, le=8192)
    depth: int = Field(default=16_384, ge=0, le=131_072)
    concurrency: int = Field(default=1, ge=1, le=64)
    runs: int = Field(default=7, ge=1, le=50)
    enable_prefix_caching: bool = True
    exact_tg: bool = False
    sglang_compatibility: bool = False
    api_key_env: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    leaderboard_benchmark_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @field_validator("report_model_name", "served_model_name", "api_key_env")
    @classmethod
    def normalize_arena_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("tokenizer")
    @classmethod
    def normalize_tokenizer(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tokenizer must not be empty")
        return normalized


class ArenaMetricRead(BaseModel):
    mean: float
    std: float
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


class ArenaResultSummary(BaseModel):
    prefill_throughput: ArenaMetricRead | None = None
    decode_throughput: ArenaMetricRead | None = None
    peak_decode_throughput: ArenaMetricRead | None = None
    ttfr_ms: ArenaMetricRead | None = None
    estimated_prefill_ms: ArenaMetricRead | None = None
    e2e_ttft_ms: ArenaMetricRead | None = None
    tpot_ms: ArenaMetricRead | None = None


class ArenaSpeculativeMetrics(BaseModel):
    supported: bool = False
    accepted_tokens: int | None = None
    draft_tokens: int | None = None
    drafts: int | None = None
    acceptance_rate_percent: float | None = None
    per_position_acceptance_percent: dict[str, float] = Field(default_factory=dict)
    source: str | None = None


class ArenaResourceNodeSummary(BaseModel):
    node_id: str
    label: str
    samples: int = 0
    peak_cpu_percent: float | None = None
    peak_gpu_utilization_percent: float | None = None
    peak_unified_memory_used_bytes: int | None = None
    peak_unified_memory_percent: float | None = None
    peak_rx_bps: float | None = None
    peak_tx_bps: float | None = None


class ArenaResourceSummary(BaseModel):
    sample_count: int = 0
    interval_seconds: float = 1.0
    nodes: list[ArenaResourceNodeSummary] = Field(default_factory=list)
    note: str = (
        "Resource samples are observational; cached SSH samples and monitoring overhead can "
        "affect precision."
    )


class ArenaSpeculativeConfig(BaseModel):
    enabled: bool | None = None
    method: str | None = None
    draft_model: str | None = None
    num_speculative_tokens: int | None = None
    draft_tensor_parallel_size: int | None = None
    draft_sample_method: str | None = None
    rejection_sample_method: str | None = None
    evidence: str | None = None
    raw: dict[str, Any] | str | None = None


class ArenaRuntimeRecipe(BaseModel):
    container: str | None = None
    command: str | None = None
    model_revision: str | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    pre_exec: list[str] = Field(default_factory=list)
    speculative: ArenaSpeculativeConfig = Field(default_factory=ArenaSpeculativeConfig)


class ArenaRuntimeProfile(BaseModel):
    engine: str | None = None
    image: str | None = None
    container_name: str | None = None
    launch_command: str | None = None
    configuration_source: str | None = None
    cluster_size: int | None = None
    tensor_parallel: int | None = None
    max_model_len: int | None = None
    gpu_memory_utilization: float | None = None
    quantization: str | None = None
    prefix_caching: bool | None = None
    speculative: ArenaSpeculativeConfig = Field(default_factory=ArenaSpeculativeConfig)


class ArenaLeaderboardReference(BaseModel):
    benchmark_id: str
    model_name: str
    model_path: str | None = None
    runtime: str
    cluster_size: int
    source_url: str
    summary: ArenaResultSummary
    baseline_pp: int | None = None
    baseline_tg: int | None = None
    baseline_depth: int | None = None
    baseline_concurrency: int | None = None
    parameter_match: bool = True
    comparison_notes: list[str] = Field(default_factory=list)
    recipe: ArenaRuntimeRecipe | None = None
    local_runtime: ArenaRuntimeProfile | None = None
    prefill_gap_percent: float | None = None
    decode_gap_percent: float | None = None
    ttfr_gap_percent: float | None = None


class ArenaProgressRead(BaseModel):
    total_requests: int = 0
    started_requests: int = 0
    completed_requests: int = 0
    failed_requests: int = 0
    last_event: str | None = None
    last_label: str | None = None


class ArenaRunRead(BaseModel):
    id: str
    target_type: Literal["deployment", "ai_session"]
    target_id: str
    target_label: str
    service_url: str
    served_model_name: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    parameters: ArenaRunCreate
    progress: ArenaProgressRead
    result_summary: ArenaResultSummary | None
    speculative_metrics: ArenaSpeculativeMetrics | None = None
    resource_summary: ArenaResourceSummary | None = None
    leaderboard_reference: ArenaLeaderboardReference | None
    command_preview: list[str]
    result_available: bool
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class ArenaRunList(BaseModel):
    items: list[ArenaRunRead]
    total: int


class ArenaLeaderboardEntry(BaseModel):
    id: str
    model_name: str
    runtime: str
    cluster_size: int
    source_url: str


class ArenaLeaderboardPreset(BaseModel):
    pp: int
    tg: int
    depth: int
    concurrency: int
    prefill_test_name: str
    decode_test_name: str
    summary: ArenaResultSummary


class ArenaLeaderboardDetail(BaseModel):
    id: str
    model_name: str
    model_path: str | None = None
    runtime: str
    cluster_size: int
    source_url: str
    presets: list[ArenaLeaderboardPreset]
    recipe: ArenaRuntimeRecipe | None = None


class ArenaLeaderboardList(BaseModel):
    generated_at: datetime | None = None
    items: list[ArenaLeaderboardEntry]
    total: int


class LocalImageRead(BaseModel):
    image_id: str
    repository: str
    tag: str
    reference: str
    digest: str | None
    size: str
    created_at: str
    created_since: str
    containers: int | None
    vllm_candidate: bool


class LocalImageList(BaseModel):
    items: list[LocalImageRead]
    total: int


class LocalModelRootCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)


class LocalModelRootRead(BaseModel):
    path: str
    created_at: datetime


class LocalModelRead(BaseModel):
    name: str
    path: str
    root_path: str
    architecture: str | None
    model_type: str | None
    quantization: str | None
    weight_format: str | None
    weight_files: int
    usable_weight_files: int
    weight_bytes: int
    expected_weight_bytes: int
    downloaded_weight_bytes: int
    incomplete_weight_files: int
    total_files: int
    total_bytes: int
    modified_at: datetime | None
    has_config: bool
    has_tokenizer: bool
    missing_weight_files: int
    status: Literal["ready", "incomplete", "unrecognized"]
    status_reason: str | None


class LocalModelInventory(BaseModel):
    roots: list[LocalModelRootRead]
    items: list[LocalModelRead]
    total: int
    total_bytes: int
    errors: dict[str, str] = Field(default_factory=dict)


class SSHNodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or normalized.startswith("-")
            or any(character.isspace() for character in normalized)
            or "/" in normalized
            or "@" in normalized
        ):
            raise ValueError("host must be one hostname or IP address")
        return normalized

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        normalized = value.strip()
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        if not normalized or normalized.startswith("-") or not set(normalized) <= allowed:
            raise ValueError("username contains unsupported characters")
        return normalized


class SSHNodeRead(SSHNodeCreate):
    id: str
    created_at: datetime
    updated_at: datetime


class SSHNodeList(BaseModel):
    items: list[SSHNodeRead]
    total: int


class AIDeploymentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = Field(default="nvcr.io/nvidia/vllm:26.07-py3", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    served_model_name: str | None = Field(default=None, min_length=1, max_length=255)
    max_model_len: int | None = Field(default=None, ge=1)
    gpu_memory_utilization: float = Field(default=0.9, gt=0, le=1)
    startup_timeout_seconds: int = Field(default=1200, ge=10, le=7200)
    trust_remote_code: bool = False
    remote_code_acknowledged: bool = False
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("-") or any(character.isspace() for character in normalized):
            raise ValueError("image must be one Docker image reference")
        return normalized

    @field_validator("extra_args")
    @classmethod
    def validate_extra_args(cls, values: list[str]) -> list[str]:
        reserved = {
            "--host",
            "--port",
            "--model",
            "--served-model-name",
            "--download-dir",
            "--trust-remote-code",
        }
        for value in values:
            flag = value.strip().split(maxsplit=1)[0]
            if not flag.startswith("--"):
                raise ValueError("each extra argument must start with --")
            if flag in reserved:
                raise ValueError(f"{flag} is managed by the platform")
        return values

    def model_post_init(self, __context: Any) -> None:
        if self.trust_remote_code and not self.remote_code_acknowledged:
            raise ValueError(
                "remote_code_acknowledged must be true when trust_remote_code is enabled"
            )


class UIEditRect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float = Field(ge=0)
    height: float = Field(ge=0)


class UIEditTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ui_id: str | None = Field(default=None, max_length=200)
    tag_name: str = Field(min_length=1, max_length=40, pattern=r"^[a-zA-Z][a-zA-Z0-9-]*$")
    element_id: str | None = Field(default=None, max_length=200)
    classes: list[str] = Field(default_factory=list, max_length=30)
    text: str = Field(default="", max_length=4_000)
    html: str = Field(default="", max_length=12_000)
    dom_path: str = Field(min_length=1, max_length=2_000)
    view_id: str | None = Field(default=None, max_length=200)
    page_url: str = Field(default="", max_length=2_000)
    computed_styles: dict[str, str] = Field(default_factory=dict, max_length=30)
    rect: UIEditRect

    @field_validator("classes")
    @classmethod
    def validate_classes(cls, values: list[str]) -> list[str]:
        if sum(len(value) for value in values) > 2_000:
            raise ValueError("classes are too large")
        return [value[:200] for value in values]

    @field_validator("computed_styles")
    @classmethod
    def validate_computed_styles(cls, values: dict[str, str]) -> dict[str, str]:
        return {str(key)[:100]: str(value)[:500] for key, value in values.items()}


class UIEditScreenshotContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["page_segment", "scroll_region"]
    label: str = Field(min_length=1, max_length=200)
    image_data_url: str = Field(min_length=100, max_length=12_000_000)
    image_width: int = Field(ge=1, le=16_384)
    image_height: int = Field(ge=1, le=16_384)
    offset_y: int = Field(default=0, ge=0, le=1_000_000)
    dom_path: str | None = Field(default=None, max_length=2_000)

    @field_validator("image_data_url")
    @classmethod
    def validate_image_data_url(cls, value: str) -> str:
        if not value.startswith(
            (
                "data:image/png;base64,",
                "data:image/jpeg;base64,",
                "data:image/webp;base64,",
            )
        ):
            raise ValueError("screenshot must be a base64 PNG, JPEG, or WebP data URL")
        return value


class UIEditScreenshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_image_data_url: str = Field(min_length=100, max_length=12_000_000)
    annotated_image_data_url: str = Field(min_length=100, max_length=12_000_000)
    detail_image_data_url: str | None = Field(default=None, min_length=100, max_length=8_000_000)
    selection: UIEditRect
    image_width: int = Field(ge=1, le=16_384)
    image_height: int = Field(ge=1, le=16_384)
    viewport_width: int = Field(ge=1, le=16_384)
    viewport_height: int = Field(ge=1, le=16_384)
    device_pixel_ratio: float = Field(gt=0, le=10)
    dom_paths: list[str] = Field(default_factory=list, max_length=30)
    context_images: list[UIEditScreenshotContext] = Field(default_factory=list, max_length=12)

    @field_validator("full_image_data_url", "annotated_image_data_url", "detail_image_data_url")
    @classmethod
    def validate_image_data_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = (
            "data:image/png;base64,",
            "data:image/jpeg;base64,",
            "data:image/webp;base64,",
        )
        if not value.startswith(allowed):
            raise ValueError("screenshot must be a base64 PNG, JPEG, or WebP data URL")
        return value

    @field_validator("dom_paths")
    @classmethod
    def normalize_dom_paths(cls, values: list[str]) -> list[str]:
        return [value[:1_000] for value in values]


class UIPageCaptureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str = Field(min_length=100, max_length=12_000_000)
    viewport_width: int = Field(ge=320, le=8_192)
    viewport_height: int = Field(ge=240, le=8_192)
    device_pixel_ratio: float = Field(gt=0, le=2)
    scroll_x: int = Field(default=0, ge=0, le=1_000_000)
    scroll_y: int = Field(default=0, ge=0, le=1_000_000)


class UIEditCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["codex"] = "codex"
    instruction: str = Field(min_length=1, max_length=4_000)
    scope: Literal["element", "component", "page", "global"] = "element"
    target: UIEditTarget
    screenshot: UIEditScreenshot | None = None

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("instruction must not be empty")
        return normalized


class UIEditMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4_000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be empty")
        return normalized


class UIEditRevertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("rollback reason must not be empty")
        return normalized


class UIEditRead(BaseModel):
    id: str
    provider: Literal["codex"]
    status: Literal[
        "queued",
        "running",
        "repairing",
        "validating",
        "applied",
        "applied_with_warnings",
        "failed",
        "stopped",
        "reverted",
    ]
    instruction: str
    scope: Literal["element", "component", "page", "global"]
    target: UIEditTarget
    changed_files: list[str]
    summary: str | None
    error_message: str | None
    warnings: list[str]
    codex_session_id: str | None
    commit_sha: str | None
    revert_commit_sha: str | None
    rollback_reason: str | None
    created_at: datetime
    updated_at: datetime


class UIEditList(BaseModel):
    items: list[UIEditRead]
    total: int


class UIEditLogRead(BaseModel):
    logs: str
    size_bytes: int
    truncated: bool


class UIEditConversationEvent(BaseModel):
    sequence: int
    kind: Literal["user", "assistant", "command", "output", "status", "warning", "result"]
    content: str
    title: str | None = None
    created_at: datetime


class UIEditConversationRead(BaseModel):
    events: list[UIEditConversationEvent]
    truncated: bool


class UIEditorStatus(BaseModel):
    enabled: bool
    provider_available: bool
    active_job_id: str | None
    last_applied_job_id: str | None
