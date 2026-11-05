from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_name: str = "uniform.db"
    worker_enabled: bool = True
    worker_poll_seconds: float = 0.5
    default_startup_timeout_seconds: int = 600
    docker_binary: str = "docker"
    hf_token: str | None = None
    modelscope_token: str | None = None
    api_token: str | None = None
    download_workers: int = 4
    metrics_interval_seconds: float = 2.0
    ssh_monitor_interval_seconds: float = 5.0
    codex_binary: str = "codex"
    claude_binary: str = "claude"
    deepseek_binary: str = "deepseek"
    deepseek_args: str = ""
    ai_sessions_enabled: bool = True
    ai_full_access_enabled: bool = False
    ui_editing_enabled: bool = True
    ai_reference_max_bytes: int = 1_000_000
    benchmark_reference_dir: Path = Path(__file__).with_name("benchmark_reference")
    benchmark_history_csv: Path | None = field(
        default_factory=lambda: Path.home() / "Downloads" / "模型性能测试 - Sheet2.csv"
    )
    catalog_refresh_interval_seconds: int = 3600
    catalog_refresh_limit: int = 50
    catalog_auto_refresh_enabled: bool = True
    control_url: str = "http://127.0.0.1:18180"
    publication_host: str = "0.0.0.0"
    publication_port: int = 18181
    publication_advertise_host: str | None = None
    publication_request_timeout_seconds: int = 1800
    publication_control_hosts: tuple[str, ...] = ()
    gateway_requests_per_minute: int = 60
    gateway_tokens_per_minute: int = 120_000
    gateway_key_concurrency: int = 4
    gateway_total_concurrency: int = 16
    gateway_quota_reservation_tokens: int = 1024
    external_access_enabled: bool = True
    cloudflared_binary: str = "cloudflared"
    external_access_start_timeout_seconds: float = 30.0
    external_tunnel_token: str | None = None
    external_public_host: str | None = None
    external_access_health_interval_seconds: float = 15.0
    external_access_health_timeout_seconds: float = 8.0
    external_access_health_failure_threshold: int = 2
    startup_watchdog_seconds: float = 90.0
    arena_uvx_binary: str = "uvx"
    arena_uv_cache_dir: Path = field(default_factory=lambda: Path.home() / ".cache" / "uv")
    arena_uv_tool_dir: Path = field(
        default_factory=lambda: Path.home() / ".local" / "share" / "uv" / "tools"
    )
    accuracy_enabled: bool = True
    accuracy_creation_enabled: bool = True
    accuracy_evalscope_command: Path = Path("evalscope")
    accuracy_evalscope_source: Path = field(
        default_factory=lambda: Path.home() / "Downloads" / "evalscope"
    )
    initial_local_model_roots: tuple[Path, ...] = ()

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            data_dir=Path(os.getenv("UNIFORM_DATA_DIR", "./data")),
            worker_enabled=os.getenv("UNIFORM_WORKER_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            worker_poll_seconds=float(os.getenv("UNIFORM_WORKER_POLL_SECONDS", "0.5")),
            default_startup_timeout_seconds=int(
                os.getenv("UNIFORM_STARTUP_TIMEOUT_SECONDS", "600")
            ),
            docker_binary=os.getenv("UNIFORM_DOCKER_BINARY", "docker"),
            hf_token=os.getenv("HF_TOKEN") or None,
            modelscope_token=os.getenv("MODELSCOPE_API_TOKEN") or None,
            api_token=os.getenv("UNIFORM_API_TOKEN") or None,
            download_workers=int(os.getenv("UNIFORM_DOWNLOAD_WORKERS", "4")),
            metrics_interval_seconds=float(os.getenv("UNIFORM_METRICS_INTERVAL_SECONDS", "2")),
            ssh_monitor_interval_seconds=float(os.getenv("UNIFORM_SSH_MONITOR_SECONDS", "5")),
            codex_binary=os.getenv("UNIFORM_CODEX_BINARY", "codex"),
            claude_binary=os.getenv("UNIFORM_CLAUDE_BINARY", "claude"),
            deepseek_binary=os.getenv("UNIFORM_DEEPSEEK_BINARY", "deepseek"),
            deepseek_args=os.getenv("UNIFORM_DEEPSEEK_ARGS", ""),
            ai_sessions_enabled=os.getenv("UNIFORM_AI_SESSIONS_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            ai_full_access_enabled=os.getenv(
                "UNIFORM_AI_FULL_ACCESS_ENABLED", "false"
            ).lower()
            not in {"0", "false", "no"},
            ui_editing_enabled=os.getenv("UNIFORM_UI_EDITING_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            ai_reference_max_bytes=int(os.getenv("UNIFORM_AI_REFERENCE_MAX_BYTES", "1000000")),
            benchmark_reference_dir=Path(
                os.getenv(
                    "UNIFORM_BENCHMARK_REFERENCE_DIR",
                    str(Path(__file__).with_name("benchmark_reference")),
                )
            ).expanduser(),
            benchmark_history_csv=(
                Path(os.environ["UNIFORM_BENCHMARK_HISTORY_CSV"]).expanduser()
                if os.getenv("UNIFORM_BENCHMARK_HISTORY_CSV")
                else Path.home() / "Downloads" / "模型性能测试 - Sheet2.csv"
            ),
            catalog_refresh_interval_seconds=int(
                os.getenv("UNIFORM_CATALOG_REFRESH_SECONDS", "3600")
            ),
            catalog_refresh_limit=int(os.getenv("UNIFORM_CATALOG_REFRESH_LIMIT", "50")),
            catalog_auto_refresh_enabled=os.getenv("UNIFORM_CATALOG_AUTO_REFRESH", "true").lower()
            not in {"0", "false", "no"},
            control_url=os.getenv("UNIFORM_CONTROL_URL")
            or f"http://127.0.0.1:{os.getenv('UNIFORM_PORT', '18180')}",
            publication_host=os.getenv("UNIFORM_PUBLICATION_HOST", "0.0.0.0"),
            publication_port=int(os.getenv("UNIFORM_PUBLICATION_PORT", "18181")),
            publication_advertise_host=(os.getenv("UNIFORM_PUBLICATION_ADVERTISE_HOST") or None),
            publication_request_timeout_seconds=int(
                os.getenv("UNIFORM_PUBLICATION_REQUEST_TIMEOUT_SECONDS", "1800")
            ),
            publication_control_hosts=tuple(
                host.strip().casefold()
                for host in os.getenv("UNIFORM_PUBLICATION_CONTROL_HOSTS", "").split(",")
                if host.strip()
            ),
            gateway_requests_per_minute=int(
                os.getenv("UNIFORM_GATEWAY_REQUESTS_PER_MINUTE", "60")
            ),
            gateway_tokens_per_minute=int(
                os.getenv("UNIFORM_GATEWAY_TOKENS_PER_MINUTE", "120000")
            ),
            gateway_key_concurrency=int(
                os.getenv("UNIFORM_GATEWAY_KEY_CONCURRENCY", "4")
            ),
            gateway_total_concurrency=int(
                os.getenv("UNIFORM_GATEWAY_TOTAL_CONCURRENCY", "16")
            ),
            gateway_quota_reservation_tokens=int(
                os.getenv("UNIFORM_GATEWAY_QUOTA_RESERVATION_TOKENS", "1024")
            ),
            external_access_enabled=os.getenv("UNIFORM_EXTERNAL_ACCESS_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            cloudflared_binary=os.getenv("UNIFORM_CLOUDFLARED_BINARY", "cloudflared"),
            external_access_start_timeout_seconds=float(
                os.getenv("UNIFORM_EXTERNAL_ACCESS_START_TIMEOUT_SECONDS", "30")
            ),
            external_tunnel_token=os.getenv("UNIFORM_CLOUDFLARED_TUNNEL_TOKEN") or None,
            external_public_host=os.getenv("UNIFORM_EXTERNAL_PUBLIC_HOST") or None,
            external_access_health_interval_seconds=float(
                os.getenv("UNIFORM_EXTERNAL_ACCESS_HEALTH_INTERVAL_SECONDS", "15")
            ),
            external_access_health_timeout_seconds=float(
                os.getenv("UNIFORM_EXTERNAL_ACCESS_HEALTH_TIMEOUT_SECONDS", "8")
            ),
            external_access_health_failure_threshold=int(
                os.getenv("UNIFORM_EXTERNAL_ACCESS_HEALTH_FAILURE_THRESHOLD", "2")
            ),
            startup_watchdog_seconds=float(os.getenv("UNIFORM_STARTUP_WATCHDOG_SECONDS", "90")),
            arena_uvx_binary=os.getenv("UNIFORM_ARENA_UVX_BINARY", "uvx"),
            arena_uv_cache_dir=Path(
                os.getenv(
                    "UNIFORM_ARENA_UV_CACHE_DIR",
                    str(Path.home() / ".cache" / "uv"),
                )
            ).expanduser(),
            arena_uv_tool_dir=Path(
                os.getenv(
                    "UNIFORM_ARENA_UV_TOOL_DIR",
                    str(Path.home() / ".local" / "share" / "uv" / "tools"),
                )
            ).expanduser(),
            accuracy_enabled=os.getenv("UNIFORM_ACCURACY_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            accuracy_creation_enabled=os.getenv("UNIFORM_ACCURACY_CREATION_ENABLED", "true").lower()
            not in {"0", "false", "no"},
            accuracy_evalscope_command=Path(
                os.getenv("UNIFORM_ACCURACY_EVALSCOPE_COMMAND", "evalscope")
            ).expanduser(),
            accuracy_evalscope_source=Path(
                os.getenv(
                    "UNIFORM_ACCURACY_EVALSCOPE_SOURCE",
                    str(Path.home() / "Downloads" / "evalscope"),
                )
            ).expanduser(),
            initial_local_model_roots=tuple(
                Path(value).expanduser()
                for value in os.getenv("UNIFORM_LOCAL_MODEL_DIRS", "").split(os.pathsep)
                if value.strip()
            ),
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_name

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def ai_sessions_dir(self) -> Path:
        return self.data_dir / "ai-sessions"

    @property
    def ui_edits_dir(self) -> Path:
        return self.data_dir / "ui-edits"

    @property
    def arena_runs_dir(self) -> Path:
        return self.data_dir / "arena-runs"

    @property
    def accuracy_runs_dir(self) -> Path:
        return self.data_dir / "accuracy-runs"

    @property
    def external_access_state_path(self) -> Path:
        return self.data_dir / "service-external-access.json"
