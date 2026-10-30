from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..accuracy_identity import cache_fingerprint, evaluation_fingerprint
from ..accuracy_store import AccuracyRunNotFound
from ..database import Database
from ..schemas import AccuracyRunCreate, AccuracyRunRead
from ..store import to_iso, utc_now
from .accuracy import AccuracyCompatibilityError, AccuracyManager
from .ai_terminal import AITerminalManager


class AIAccuracyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: AccuracyRunCreate
    rationale: str = Field(min_length=1, max_length=2000)


class AIAccuracyConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1, max_length=64)


class AIAccuracyAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=12_000)


class AIAccuracyManager:
    """AI proposes; the user authorizes an immutable plan; EvalScope owns all scores."""

    def __init__(
        self,
        database: Database,
        accuracy: AccuracyManager,
        terminal: AITerminalManager,
        *,
        on_complete: Callable[[str], None] | None = None,
    ) -> None:
        self.database = database
        self.accuracy = accuracy
        self.terminal = terminal
        self.on_complete = on_complete
        self._lock = threading.RLock()
        self._active: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._closed = False

    def recover(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_accuracy_workflows SET status='interrupted', "
                "error='Control service restarted; submit and confirm a new plan', updated_at=? "
                "WHERE status IN ('approved','probing','running')",
                (to_iso(utc_now()),),
            )
            connection.commit()

    def _ensure(self, session_id: str) -> dict[str, Any]:
        session = self.terminal.store.get(session_id)
        if session.task_type != "accuracy_evaluation":
            raise AccuracyCompatibilityError("this is not an active AI accuracy session")
        context = self.terminal.model_context(session_id).get("deployment", {})
        if not isinstance(context, dict) or not isinstance(context.get("accuracy_target"), dict):
            raise AccuracyCompatibilityError("AI accuracy target context is unavailable")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ai_accuracy_workflows"
                "(session_id,context_json,updated_at) VALUES(?,?,?)",
                (session_id, json.dumps(context), to_iso(utc_now())),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM ai_accuracy_workflows WHERE session_id=?", (session_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def _update(self, session_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "proposal_json",
            "proposal_id",
            "probe_run_id",
            "full_run_id",
            "analysis",
            "error",
        }
        if not set(values).issubset(allowed):
            raise ValueError("invalid AI accuracy state fields")
        values["updated_at"] = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_accuracy_workflows SET "
                + ",".join(f"{name}=?" for name in values)
                + " WHERE session_id=?",
                (*values.values(), session_id),
            )
            connection.commit()

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            result = {
                "session_id": session_id,
                "status": row["status"],
                "context": json.loads(row["context_json"]),
                "proposal": json.loads(row["proposal_json"]) if row["proposal_json"] else None,
                "proposal_id": row["proposal_id"],
                "analysis": row["analysis"],
                "error": row["error"],
                "updated_at": row["updated_at"],
            }
            for name in ("probe", "full"):
                run_id = row[f"{name}_run_id"]
                try:
                    result[f"{name}_run"] = (
                        self.accuracy.store.get(run_id).model_dump(mode="json") if run_id else None
                    )
                except AccuracyRunNotFound:
                    result[f"{name}_run"] = None
                    result["error"] = "An attached evaluation record was deleted"
            return result

    def inspect(self, session_id: str) -> dict[str, Any]:
        state = self.get(session_id)
        try:
            self.accuracy.adapter.verify_identity()
            environment = {"ready": True, "version": self.accuracy.adapter.version}
        except Exception as error:
            environment = {
                "ready": False,
                "error": self.accuracy._redact_log(str(error)),
                "action": "Ask the operator to configure the pinned EvalScope executable/source; "
                "do not install dependencies or restart any service yourself.",
            }
        state["environment"] = environment
        state["allowed_datasets"] = self.accuracy.adapter.dataset_catalog()
        return state

    def _identity(self, request: AccuracyRunCreate) -> str:
        target = self.accuracy.target_resolver.resolve(request.target_type, request.target_id)
        resolved = self.accuracy.adapter.resolve_config(request, target.snapshot)
        fingerprint = evaluation_fingerprint(
            resolved, self.accuracy.adapter.version, self.accuracy.adapter.commit
        )
        return cache_fingerprint(fingerprint, target.model_name, target.snapshot)

    def propose(self, session_id: str, proposal: AIAccuracyProposal) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            if session_id in self._active or row["status"] in {"probing", "running"}:
                raise AccuracyCompatibilityError("cancel the current workflow before replanning")
            context = json.loads(row["context_json"])
            target = context["accuracy_target"]
            request = proposal.request
            if (request.target_type, request.target_id) != (
                target["target_type"],
                target["target_id"],
            ):
                raise AccuracyCompatibilityError("the plan cannot change the selected target")
            if request.use_cache:
                raise AccuracyCompatibilityError("AI plans must measure fresh samples")
            self.accuracy.adapter.verify_identity()
            identity = self._identity(request)
            probe = request.model_copy(deep=True)
            for dataset in probe.datasets:
                dataset.limit = min(2, dataset.limit)
            full_count = sum(len(item.subset_list) * item.limit for item in request.datasets)
            probe_count = sum(len(item.subset_list) * item.limit for item in probe.datasets)
            # A <=2-sample plan needs only one run, not a duplicated probe and full run.
            separate_probe = probe_count < full_count
            total = full_count + (probe_count if separate_probe else 0)
            if total > context["sample_budget"]:
                raise AccuracyCompatibilityError("probe plus full samples exceed the user budget")
            stored = {
                **proposal.model_dump(mode="json"),
                "rationale": self.accuracy._redact_log(proposal.rationale),
                "probe_request": probe.model_dump(mode="json") if separate_probe else None,
                "probe_samples": probe_count if separate_probe else 0,
                "full_samples": full_count,
                "total_samples": total,
                "output_token_cap": total * request.generation_config.max_tokens,
                "target_identity": identity,
                "notice": "Sample limits are per subset. These are sampled results, not full "
                "official benchmark scores. No global mean is calculated.",
            }
            self._update(
                session_id,
                status="awaiting_confirmation",
                proposal_id=str(uuid4()),
                proposal_json=json.dumps(stored),
                probe_run_id=None,
                full_run_id=None,
                analysis=None,
                error=None,
            )
            return self.get(session_id)

    def confirm(self, session_id: str, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            if row["proposal_id"] != proposal_id:
                raise AccuracyCompatibilityError("the plan changed; review the latest proposal")
            if row["status"] == "awaiting_confirmation":
                plan = json.loads(row["proposal_json"])
                request = AccuracyRunCreate.model_validate(plan["request"])
                if self._identity(request) != plan["target_identity"]:
                    raise AccuracyCompatibilityError("the deployment changed; request a new plan")
                self._update(session_id, status="approved")
                self.terminal.store.add_audit(
                    session_id,
                    "accuracy_plan_confirmed",
                    "User confirmed AI accuracy plan",
                    {"proposal_id": proposal_id, "samples": plan["total_samples"]},
                )
            elif row["status"] not in {"approved", "probing", "running", "completed"}:
                raise AccuracyCompatibilityError("there is no pending plan to confirm")
            return self.start(session_id)

    def start(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            if session_id in self._active or row["status"] == "completed":
                return self.get(session_id)
            if self._closed or row["status"] != "approved":
                raise AccuracyCompatibilityError(
                    "a user-confirmed plan is required before execution"
                )
            plan = json.loads(row["proposal_json"])
            first_request = AccuracyRunCreate.model_validate(
                plan["probe_request"] or plan["request"]
            )
            if (
                self._identity(AccuracyRunCreate.model_validate(plan["request"]))
                != (plan["target_identity"])
            ):
                raise AccuracyCompatibilityError("the deployment changed; request a new plan")
            run = self.accuracy.create(first_request)
            try:
                is_probe = bool(plan["probe_request"])
                self._update(
                    session_id,
                    status="probing" if is_probe else "running",
                    **{"probe_run_id" if is_probe else "full_run_id": run.id},
                )
                cancelled = threading.Event()
                thread = threading.Thread(
                    target=self._execute,
                    args=(session_id, plan, run, cancelled),
                    daemon=True,
                    name=f"ai-accuracy-{session_id[:8]}",
                )
                self._active[session_id] = (thread, cancelled)
                thread.start()
            except Exception:
                self._active.pop(session_id, None)
                self.accuracy.cancel(run.id)
                raise
            return self.get(session_id)

    def _wait(self, run_id: str, cancelled: threading.Event) -> AccuracyRunRead:
        while True:
            run = self.accuracy.store.get(run_id)
            if run.status not in {"queued", "preparing", "running", "parsing"}:
                if not self.accuracy.is_active(run_id):
                    return run
            if cancelled.wait(0.1):
                raise AccuracyCompatibilityError("AI accuracy workflow was cancelled")

    @staticmethod
    def _require_complete(run: AccuracyRunRead) -> None:
        if run.status != "completed" or not run.summary or run.summary.incomplete:
            raise AccuracyCompatibilityError(
                "evaluation failed or was incomplete; full run stopped"
            )

    def _execute(
        self,
        session_id: str,
        plan: dict[str, Any],
        run: AccuracyRunRead,
        cancelled: threading.Event,
    ) -> None:
        completed = False
        try:
            self._require_complete(self._wait(run.id, cancelled))
            if plan["probe_request"]:
                with self._lock:
                    if cancelled.is_set() or self._closed:
                        raise AccuracyCompatibilityError("AI accuracy workflow was cancelled")
                    request = AccuracyRunCreate.model_validate(plan["request"])
                    if self._identity(request) != plan["target_identity"]:
                        raise AccuracyCompatibilityError(
                            "deployment changed after probe; full run stopped"
                        )
                    run = self.accuracy.create(request)
                    self._update(session_id, status="running", full_run_id=run.id)
                self._require_complete(self._wait(run.id, cancelled))
            with self._lock:
                if not cancelled.is_set():
                    self._update(session_id, status="completed")
                    completed = True
        except Exception as error:
            with self._lock:
                if not cancelled.is_set():
                    self._update(
                        session_id, status="blocked", error=self.accuracy._redact_log(str(error))
                    )
        finally:
            if not completed and self.accuracy.is_active(run.id):
                self.accuracy.cancel(run.id)
            with self._lock:
                self._active.pop(session_id, None)
            if completed and self.on_complete and not self._closed:
                self.on_complete(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            active = self._active.get(session_id)
            if active:
                active[1].set()
            for name in ("probe_run_id", "full_run_id"):
                if row[name] and self.accuracy.is_active(row[name]):
                    self.accuracy.cancel(row[name])
            if row["status"] != "completed":
                self._update(session_id, status="cancelled")
            return self.get(session_id)

    def record_analysis(self, session_id: str, analysis: AIAccuracyAnalysis) -> dict[str, Any]:
        with self._lock:
            state = self.get(session_id)
            run = state["full_run"]
            if state["status"] != "completed" or not run or analysis.run_id != run["id"]:
                raise AccuracyCompatibilityError(
                    "analysis requires this workflow's completed report"
                )
            self._update(session_id, analysis=self.accuracy._redact_log(analysis.text))
            return self.get(session_id)

    def report(self, session_id: str) -> str:
        state = self.get(session_id)
        run = state["full_run"]
        if state["status"] != "completed" or not run:
            raise AccuracyCompatibilityError("a completed real report is required")
        lines = [
            "# 模型精度评测报告",
            "",
            f"模型：{run['model_name']}",
            f"任务：{run['id']}",
            f"评测指纹：{run['evaluation_fingerprint']}",
            "",
            "抽样评测，不代表完整官方成绩。分数由 EvalScope 计算，不计算综合平均分。",
            "",
            "| 数据集 | 子集 | 指标 | 得分 | 样本 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for metric in run["metrics"]:
            score = metric["score"]
            rendered = (
                f"{score * 100:.2f}%" if metric["metric_key"] == "accuracy:mean" else str(score)
            )
            lines.append(
                f"| {metric['dataset_name']} | {metric['subset_name']} | "
                f"{metric['metric_display_name']} | {rendered} | {metric['sample_count']} |"
            )
        lines.extend(
            [
                "",
                "## AI 解读（以标准报告数据为准）",
                "",
                state["analysis"] or "AI 尚未完成解读；以上真实指标不受 AI 会话状态影响。",
                "",
            ]
        )
        return "\n".join(lines)

    def stop_all(self) -> None:
        with self._lock:
            self._closed = True
            active = list(self._active.items())
        for session_id, _ in active:
            self.cancel(session_id)
        for _, (thread, _) in active:
            thread.join(timeout=5)

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active
