from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 26


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feishu_recipient (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    app_id TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    bound_at REAL NOT NULL,
                    verified_at REAL
                );

                CREATE TABLE IF NOT EXISTS system_alert_state (
                    node_id TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    PRIMARY KEY(node_id, metric)
                );

                CREATE TABLE IF NOT EXISTS system_alert_events (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_label TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    generation INTEGER NOT NULL,
                    delivery TEXT NOT NULL
                        CHECK(delivery IN ('pending','sent','failed','cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_alert_events_pending
                    ON system_alert_events(delivery, next_attempt);

                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    revision TEXT,
                    display_name TEXT,
                    parameters_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 1),
                    error_code TEXT,
                    error_message TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
                    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                    archived_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    deployment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id) ON DELETE CASCADE,
                    UNIQUE(deployment_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_deployments_status
                    ON deployments(status);
                CREATE INDEX IF NOT EXISTS idx_deployments_updated_at
                    ON deployments(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_deployment_sequence
                    ON deployment_events(deployment_id, sequence);

                CREATE TABLE IF NOT EXISTS model_catalog (
                    source TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    revision TEXT,
                    author TEXT,
                    pipeline_tag TEXT,
                    library_name TEXT,
                    tags_json TEXT NOT NULL,
                    downloads INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    last_modified TEXT,
                    created_at TEXT,
                    gated INTEGER NOT NULL DEFAULT 0,
                    private INTEGER NOT NULL DEFAULT 0,
                    compatibility TEXT NOT NULL,
                    compatibility_reason TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL,
                    popularity_rank INTEGER,
                    PRIMARY KEY(source, model_id)
                );

                CREATE INDEX IF NOT EXISTS idx_catalog_source_modified
                    ON model_catalog(source, last_modified DESC);
                CREATE INDEX IF NOT EXISTS idx_catalog_downloads
                    ON model_catalog(downloads DESC);

                CREATE TABLE IF NOT EXISTS ai_sessions (
                    id TEXT PRIMARY KEY,
                    client_request_id TEXT,
                    deployment_id TEXT,
                    provider TEXT NOT NULL CHECK(
                        provider IN ('codex', 'claude', 'deepseek', 'dsh')
                    ),
                    task_type TEXT NOT NULL DEFAULT 'general',
                    display_name TEXT,
                    target_model_id TEXT,
                    ai_model TEXT,
                    reasoning_effort TEXT,
                    archived_at TEXT,
                    execution_mode TEXT NOT NULL DEFAULT 'interactive',
                    status TEXT NOT NULL,
                    initial_prompt TEXT NOT NULL,
                    reference_urls_json TEXT NOT NULL,
                    workspace_path TEXT NOT NULL,
                    pid INTEGER,
                    exit_code INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS ai_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES ai_sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_ai_sessions_created
                    ON ai_sessions(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_audit_session
                    ON ai_audit_events(session_id, id);

                CREATE TABLE IF NOT EXISTS ai_accuracy_workflows (
                    session_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    proposal_json TEXT,
                    proposal_id TEXT,
                    probe_run_id TEXT,
                    full_run_id TEXT,
                    analysis TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES ai_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ai_dataset_integrations (
                    session_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    proposal_json TEXT,
                    proposal_id TEXT,
                    validation_run_id TEXT,
                    analysis TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES ai_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS accuracy_dataset_integrations (
                    dataset_name TEXT PRIMARY KEY,
                    contract_fingerprint TEXT NOT NULL,
                    validation_run_id TEXT NOT NULL,
                    enabled_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ui_edit_jobs (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('codex')),
                    status TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    changed_files_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT,
                    error_message TEXT,
                    workspace_path TEXT NOT NULL,
                    base_hashes_json TEXT NOT NULL DEFAULT '{}',
                    applied_hashes_json TEXT NOT NULL DEFAULT '{}',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    codex_session_id TEXT,
                    commit_sha TEXT,
                    revert_commit_sha TEXT,
                    rollback_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ui_edit_jobs_created
                    ON ui_edit_jobs(created_at DESC);

                CREATE TABLE IF NOT EXISTS local_model_roots (
                    path TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_deployment_results (
                    session_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES ai_sessions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ai_deployment_templates (
                    id TEXT PRIMARY KEY,
                    source_session_id TEXT UNIQUE,
                    model_id TEXT NOT NULL,
                    target_source TEXT NOT NULL,
                    target_revision TEXT,
                    target_local_path TEXT,
                    deployment_topology TEXT NOT NULL,
                    template_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_session_id) REFERENCES ai_sessions(id) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_deployment_templates_identity
                    ON ai_deployment_templates(
                        model_id, target_source, deployment_topology, updated_at DESC
                    );

                CREATE TABLE IF NOT EXISTS ai_deployment_reuse_requests (
                    client_request_id TEXT PRIMARY KEY,
                    request_fingerprint TEXT NOT NULL,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS service_publications (
                    publication_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL
                        CHECK(target_type IN ('deployment', 'ai_session')),
                    target_id TEXT NOT NULL,
                    api_key_hash TEXT NOT NULL,
                    key_hint TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(target_type, target_id)
                );

                CREATE INDEX IF NOT EXISTS idx_service_publications_enabled
                    ON service_publications(enabled);

                CREATE TABLE IF NOT EXISTS ssh_nodes (
                    id TEXT PRIMARY KEY,
                    label TEXT,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL CHECK(port >= 1 AND port <= 65535),
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(host, port, username)
                );

                CREATE TABLE IF NOT EXISTS benchmarks (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL CHECK(target_type IN ('deployment', 'ai_session')),
                    target_id TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    service_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    completed_requests INTEGER NOT NULL DEFAULT 0,
                    successful_requests INTEGER NOT NULL DEFAULT 0,
                    failed_requests INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_benchmarks_created
                    ON benchmarks(created_at DESC);

                CREATE TABLE IF NOT EXISTS arena_runs (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL CHECK(target_type IN ('deployment', 'ai_session')),
                    target_id TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    service_url TEXT NOT NULL,
                    served_model_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    result_summary_json TEXT,
                    leaderboard_reference_json TEXT,
                    command_preview_json TEXT NOT NULL,
                    run_directory TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_arena_runs_created
                    ON arena_runs(created_at DESC);

                CREATE TABLE IF NOT EXISTS accuracy_runs (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL
                        CHECK(target_type IN ('deployment', 'ai_session')),
                    target_id TEXT NOT NULL,
                    target_label TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    service_url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'queued', 'preparing', 'running', 'parsing', 'completed',
                        'partial', 'failed', 'cancelled', 'interrupted'
                    )),
                    request_json TEXT NOT NULL,
                    resolved_config_json TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    summary_json TEXT,
                    evalscope_version TEXT NOT NULL,
                    evalscope_commit TEXT,
                    run_directory TEXT NOT NULL UNIQUE,
                    pid INTEGER CHECK(pid IS NULL OR pid > 0),
                    error_code TEXT,
                    error_message TEXT,
                    error_retryable INTEGER CHECK(
                        error_retryable IS NULL OR error_retryable IN (0, 1)
                    ),
                    error_details_json TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_accuracy_runs_created
                    ON accuracy_runs(created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_accuracy_runs_status
                    ON accuracy_runs(status, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_accuracy_runs_target
                    ON accuracy_runs(target_type, target_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS accuracy_run_metrics (
                    run_id TEXT NOT NULL,
                    dataset_name TEXT NOT NULL,
                    dataset_pretty_name TEXT NOT NULL,
                    evaluation_version TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    metric_display_name TEXT NOT NULL,
                    metric_kind TEXT NOT NULL CHECK(metric_kind IN ('quality', 'diagnostic')),
                    higher_is_better INTEGER NOT NULL CHECK(higher_is_better IN (0, 1)),
                    category_path_json TEXT NOT NULL,
                    subset_name TEXT NOT NULL,
                    sample_count INTEGER NOT NULL CHECK(sample_count >= 0),
                    score REAL NOT NULL,
                    is_primary INTEGER NOT NULL CHECK(is_primary IN (0, 1)),
                    execution_requested INTEGER NOT NULL CHECK(execution_requested >= 0),
                    execution_succeeded INTEGER NOT NULL CHECK(execution_succeeded >= 0),
                    execution_errored INTEGER NOT NULL CHECK(execution_errored >= 0),
                    PRIMARY KEY(
                        run_id, dataset_name, metric_key, category_path_json, subset_name
                    ),
                    FOREIGN KEY(run_id) REFERENCES accuracy_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_accuracy_metrics_run_dataset
                    ON accuracy_run_metrics(run_id, dataset_name);
                CREATE INDEX IF NOT EXISTS idx_accuracy_metrics_primary
                    ON accuracy_run_metrics(run_id, is_primary);
                """
            )
            accuracy_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(accuracy_runs)")
            }
            for name, definition in {
                "evaluation_fingerprint": "TEXT",
                "cache_fingerprint": "TEXT",
                "cached_samples": "INTEGER NOT NULL DEFAULT 0 CHECK(cached_samples >= 0)",
            }.items():
                if name not in accuracy_columns:
                    connection.execute(f"ALTER TABLE accuracy_runs ADD COLUMN {name} {definition}")
            catalog_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(model_catalog)").fetchall()
            }
            if "popularity_rank" not in catalog_columns:
                connection.execute("ALTER TABLE model_catalog ADD COLUMN popularity_rank INTEGER")
            if "created_at" not in catalog_columns:
                connection.execute("ALTER TABLE model_catalog ADD COLUMN created_at TEXT")
            deployment_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(deployments)").fetchall()
            }
            if "archived_at" not in deployment_columns:
                connection.execute("ALTER TABLE deployments ADD COLUMN archived_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deployments_archived_at "
                "ON deployments(archived_at, created_at DESC)"
            )
            ai_session_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ai_sessions)").fetchall()
            }
            if "execution_mode" not in ai_session_columns:
                connection.execute(
                    "ALTER TABLE ai_sessions ADD COLUMN execution_mode TEXT "
                    "NOT NULL DEFAULT 'interactive'"
                )
            if "client_request_id" not in ai_session_columns:
                connection.execute("ALTER TABLE ai_sessions ADD COLUMN client_request_id TEXT")
            if "task_type" not in ai_session_columns:
                connection.execute(
                    "ALTER TABLE ai_sessions ADD COLUMN task_type TEXT NOT NULL DEFAULT 'general'"
                )
            for column_name in (
                "display_name",
                "target_model_id",
                "ai_model",
                "reasoning_effort",
                "archived_at",
            ):
                if column_name not in ai_session_columns:
                    connection.execute(f"ALTER TABLE ai_sessions ADD COLUMN {column_name} TEXT")
            self._allow_native_ai_sessions(connection)
            connection.execute(
                "UPDATE ai_sessions SET task_type = 'deployment' "
                "WHERE task_type = 'general' AND target_model_id IS NOT NULL"
            )
            connection.execute(
                "UPDATE ai_sessions SET task_type = 'benchmark_analysis' "
                "WHERE task_type = 'general' "
                "AND initial_prompt LIKE '分析以下推理性能测试结果%'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_sessions_client_request "
                "ON ai_sessions(client_request_id) "
                "WHERE client_request_id IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_sessions_target_model "
                "ON ai_sessions(target_model_id, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_sessions_created ON ai_sessions(created_at DESC)"
            )
            ui_edit_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ui_edit_jobs)").fetchall()
            }
            ui_edit_additions = {
                "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
                "codex_session_id": "TEXT",
                "commit_sha": "TEXT",
                "revert_commit_sha": "TEXT",
                "rollback_reason": "TEXT",
            }
            for column_name, definition in ui_edit_additions.items():
                if column_name not in ui_edit_columns:
                    connection.execute(
                        f"ALTER TABLE ui_edit_jobs ADD COLUMN {column_name} {definition}"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                """,
                (SCHEMA_VERSION,),
            )
            connection.commit()

    @staticmethod
    def _allow_native_ai_sessions(connection: sqlite3.Connection) -> None:
        """Widen the ai_sessions provider CHECK constraint for older databases."""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ai_sessions'"
        ).fetchone()
        original = (row["sql"] if row else None) or ""
        if not original or "'dsh'" in original:
            return
        if re.search(r"CHECK\s*\(\s*provider\s+IN", original, re.IGNORECASE) is None:
            return
        rebuilt = re.sub(
            r"CREATE\s+TABLE\s+[\"`]?ai_sessions[\"`]?",
            "CREATE TABLE ai_sessions_rebuild",
            original,
            count=1,
            flags=re.IGNORECASE,
        )
        rebuilt = re.sub(
            r"CHECK\s*\(\s*provider\s+IN\s*\([^)]*\)\s*\)",
            "CHECK(provider IN ('codex', 'claude', 'deepseek', 'dsh'))",
            rebuilt,
            count=1,
            flags=re.IGNORECASE,
        )
        if "'dsh'" not in rebuilt:
            raise RuntimeError("could not widen the ai_sessions provider constraint")
        columns = ", ".join(
            row["name"] for row in connection.execute("PRAGMA table_info(ai_sessions)")
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                {rebuilt};
                INSERT INTO ai_sessions_rebuild ({columns})
                    SELECT {columns} FROM ai_sessions;
                DROP TABLE ai_sessions;
                ALTER TABLE ai_sessions_rebuild RENAME TO ai_sessions;
                COMMIT;
                """
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        broken = connection.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError("ai_sessions migration broke a foreign key reference")

    def check(self) -> bool:
        with self.connect() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        return result is not None and result[0] == "ok"
