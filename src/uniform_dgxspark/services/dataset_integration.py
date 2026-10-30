"""Capability-scoped AI onboarding with real probe evidence, never AI-edited code."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..database import Database
from ..schemas import AccuracyRunCreate
from ..store import to_iso, utc_now
from .accuracy import AccuracyCompatibilityError, AccuracyManager
from .ai_terminal import AITerminalManager
from .evalscope_accuracy import EvalScopeAccuracyAdapter, EvalScopeAccuracyError, _DatasetSpec

# Explicitly audited native, rule-scored chat benchmarks. Catalog membership alone is not trust.
_CANDIDATES = {
    "aime24": ("aime/aime_adapter.py", "v1.1"),
    "aime25": ("aime/aime_adapter.py", "v1.1"),
    "aime26": ("aime/aime_adapter.py", "v1.1"),
    "amc": ("amc/amc_adapter.py", "v1.1"),
    "mmlu": ("mmlu/mmlu_adapter.py", "v1.0"),
    "hellaswag": ("hellaswag/hellaswag_adapter.py", "v1.0"),
    "boolq": ("boolq/boolq_adapter.py", "v1.1"),
    "sciq": ("sciq/sciq_adapter.py", "v1.0"),
}
_CATALOG = Path(__file__).parents[1] / "web" / "accuracy-datasets.json"


class DatasetIntegrationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subset: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    rationale: str = Field(min_length=1, max_length=2000)


class DatasetIntegrationConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1, max_length=64)


class DatasetIntegrationAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=6000)


class DatasetIntegrationManager:
    def __init__(
        self, database: Database, accuracy: AccuracyManager, terminal: AITerminalManager
    ) -> None:
        self.database, self.accuracy, self.terminal = database, accuracy, terminal
        self.items = {item["name"]: item for item in json.loads(_CATALOG.read_text())["items"]}
        self._lock = threading.RLock()
        self._active: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._closed = False

    def recover(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_dataset_integrations SET status='interrupted', error=?, updated_at=? "
                "WHERE status='validating'",
                ("平台重载中断了接入验证，请重新提交并确认方案。", to_iso(utc_now())),
            )
            connection.commit()

    def candidate(self, name: str) -> tuple[_DatasetSpec, str]:
        if name not in _CANDIDATES:
            raise AccuracyCompatibilityError("此数据集尚缺专用适配，不能自动开放执行。")
        item = self.items[name]
        path, _ = _CANDIDATES[name]
        root = self.accuracy.adapter.evalscope_source
        paths = [
            root / "evalscope/benchmarks" / path,
            root / "evalscope/api/benchmark/adapters/default_data_adapter.py",
            root / "evalscope/api/benchmark/adapters/multi_choice_adapter.py",
            root / "evalscope/utils/multi_choices.py",
            root / "evalscope/metrics/math/parser.py",
        ]
        digest = hashlib.sha256()
        for source in paths:
            digest.update(source.relative_to(root).as_posix().encode() + b"\0")
            digest.update(source.read_bytes())
        tree = ast.parse(paths[0].read_text(encoding="utf-8"))
        declarations = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "BenchmarkMeta"
            ):
                fields = {field.arg: field.value for field in node.keywords}
                if isinstance(fields.get("name"), ast.Constant) and fields["name"].value == name:
                    declarations.append(fields)
        if len(declarations) != 1:
            raise AccuracyCompatibilityError("原生基准声明不唯一或缺失。")
        fields = declarations[0]
        dataset_id = ast.literal_eval(fields["dataset_id"])
        pretty_name = ast.literal_eval(fields["pretty_name"])
        version = (
            ast.literal_eval(fields["evaluation_version"])
            if "evaluation_version" in fields
            else "v1.0"
        )
        if name == "mmlu":
            mapping = next(
                node.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "SUBJECT_MAPPING"
                    for target in node.targets
                )
            )
            subsets = sorted(ast.literal_eval(mapping))
        else:
            subsets = (
                ast.literal_eval(fields["subset_list"]) if "subset_list" in fields else ["default"]
            )
        if set(subsets) != set(item["subsets"]):
            raise AccuracyCompatibilityError("目录子集与固定版本的原生契约不一致。")
        contract = {
            "name": name,
            "source": self.accuracy.adapter.commit,
            "adapter_hash": digest.hexdigest(),
            "dataset_id": dataset_id,
            "subsets": sorted(subsets),
            "version": version,
            "few_shot": 0,
            "scoring": "rule_only",
            "report_schema": 2,
        }
        fingerprint = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        return _DatasetSpec(
            dataset_id=dataset_id,
            pretty_name=pretty_name,
            evaluation_version=version,
            subsets=frozenset(subsets),
            prompt_signature=f"native-contract:{fingerprint}",
            zero_shot_only=True,
        ), fingerprint

    def verify_source(self) -> None:
        self.accuracy.adapter.verify_identity()
        self.check_source_clean()

    def check_source_clean(self) -> None:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(self.accuracy.adapter.evalscope_source),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        if result.stdout.strip():
            raise EvalScopeAccuracyError("固定版本 EvalScope 有未提交变更，接入验证已阻止。")

    def approved_specs(self) -> dict[str, _DatasetSpec]:
        result = {}
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM accuracy_dataset_integrations").fetchall()
        for row in rows:
            try:
                spec, fingerprint = self.candidate(row["dataset_name"])
                if fingerprint == row["contract_fingerprint"]:
                    result[row["dataset_name"]] = spec
            except (OSError, ValueError, KeyError):
                continue  # A changed/unavailable contract is not executable.
        return result

    def catalog(self) -> dict[str, Any]:
        allowed = self.accuracy.adapter.dataset_catalog()
        with self.database.connect() as connection:
            evidence = {
                row["dataset_name"]: row["validation_run_id"]
                for row in connection.execute("SELECT * FROM accuracy_dataset_integrations")
            }
        return {
            "allowed_datasets": allowed,
            "items": [
                {
                    "name": name,
                    "platform": allowed.get(name),
                    "can_onboard": name in _CANDIDATES,
                    "validation_run_id": evidence.get(name),
                }
                for name in self.items
            ],
        }

    def _ensure(self, session_id: str) -> dict[str, Any]:
        session = self.terminal.store.get(session_id)
        if session.task_type != "dataset_integration":
            raise AccuracyCompatibilityError("这不是数据集接入会话。")
        context = self.terminal.model_context(session_id).get("deployment", {})
        if context.get("dataset_name") not in self.items:
            raise AccuracyCompatibilityError("接入会话缺少已绑定的数据集。")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO ai_dataset_integrations(session_id,context_json,updated_at) "
                "VALUES(?,?,?)",
                (session_id, json.dumps(context), to_iso(utc_now())),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM ai_dataset_integrations WHERE session_id=?", (session_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def _update(self, session_id: str, **values: Any) -> None:
        allowed = {
            "status",
            "proposal_json",
            "proposal_id",
            "validation_run_id",
            "analysis",
            "error",
        }
        if not set(values) <= allowed:
            raise ValueError("invalid integration state field")
        values["updated_at"] = to_iso(utc_now())
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE ai_dataset_integrations SET "
                + ",".join(f"{k}=?" for k in values)
                + " WHERE session_id=?",
                (*values.values(), session_id),
            )
            connection.commit()

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._ensure(session_id)
            context = json.loads(row.pop("context_json"))
            row["context"] = context
            row["dataset"] = self.items[context["dataset_name"]]
            row["proposal"] = json.loads(row.pop("proposal_json")) if row["proposal_json"] else None
            row["integrated"] = context["dataset_name"] in self.accuracy.adapter.dataset_catalog()
            if row["validation_run_id"]:
                try:
                    row["validation_run"] = self.accuracy.store.get(row["validation_run_id"])
                except LookupError:
                    row["validation_run"] = None
            return row

    def inspect(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(session_id)
            name = state["context"]["dataset_name"]
            supported = name in _CANDIDATES
            requirements = []
            if not supported:
                item = self.items[name]
                if item["category"] != "llm":
                    requirements.append("需要对应模型类型、媒体输入或智能体执行器的专用适配。")
                if "Coding" in item["tags"] or item["category"] == "agent":
                    requirements.append("需要独立执行沙箱、资源限额和外部工具权限审核。")
                requirements.append("需先开发并验证该基准的专用适配、依赖、提示词和判分契约。")
                if state["status"] in {"draft", "interrupted", "blocked"}:
                    self._update(session_id, status="blocked", error=" ".join(requirements))
            environment = {"ready": False}
            if supported:
                try:
                    self.verify_source()
                    _, fingerprint = self.candidate(name)
                    environment = {"ready": True, "contract_fingerprint": fingerprint}
                except (
                    OSError,
                    ValueError,
                    subprocess.SubprocessError,
                    EvalScopeAccuracyError,
                ) as e:
                    environment["error"] = self.accuracy._redact_log(str(e))
            state = self.get(session_id)
            state.update(
                can_onboard=supported,
                requirements=requirements,
                environment=environment,
                probe_samples=2,
                automatic_installation=False,
                instructions="只使用本会话数据集接入工具；用户在平台确认后才可验证。",
            )
            return state

    def _plan_identity(self, context: dict, subset: str) -> tuple[AccuracyRunCreate, str]:
        target = context.get("accuracy_target")
        if not target:
            raise AccuracyCompatibilityError("请先选择运行中的模型作为接入验证目标。")
        spec, fingerprint = self.candidate(context["dataset_name"])
        if subset not in spec.subsets:
            raise AccuracyCompatibilityError("子集不在固定版本的数据集目录中。")
        request = AccuracyRunCreate(
            target_type=target["target_type"],
            target_id=target["target_id"],
            preset="custom-objective-v1",
            datasets=[
                {
                    "name": context["dataset_name"],
                    "subset_list": [subset],
                    "limit": 2,
                    "few_shot_num": 0,
                }
            ],
        )
        resolved = self.accuracy.target_resolver.resolve(request.target_type, request.target_id)
        identity = hashlib.sha256(
            json.dumps(
                {
                    "contract": fingerprint,
                    "request": request.model_dump(mode="json"),
                    "target": resolved.snapshot,
                    "model": resolved.model_name,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return request, identity

    def propose(self, session_id: str, payload: DatasetIntegrationProposal) -> dict[str, Any]:
        with self._lock:
            if self._closed or session_id in self._active:
                raise AccuracyCompatibilityError("接入验证正在执行或平台正在关闭。")
            state = self.inspect(session_id)
            if state["integrated"]:
                raise AccuracyCompatibilityError("此数据集已经接入。")
            if not state["environment"]["ready"]:
                raise AccuracyCompatibilityError("当前数据集或环境尚不具备自动接入条件。")
            request, identity = self._plan_identity(state["context"], payload.subset)
            proposal = {
                "request": request.model_dump(mode="json"),
                "identity": identity,
                "rationale": payload.rationale,
                "probe_samples": 2,
                "effect": "请求已选模型 2 条样本；报告完整后启用此数据集的客观评测。",
            }
            self._update(
                session_id,
                status="proposed",
                proposal_json=json.dumps(proposal),
                proposal_id=str(uuid4()),
                error=None,
                validation_run_id=None,
            )
            return self.get(session_id)

    def confirm(self, session_id: str, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise AccuracyCompatibilityError("平台正在关闭。")
            state = self.get(session_id)
            if state["proposal_id"] != proposal_id:
                raise AccuracyCompatibilityError("接入方案已更新，请重新核对。")
            if state["status"] in {"validating", "ready"}:
                return state
            if state["status"] != "proposed":
                raise AccuracyCompatibilityError("尚无可以确认的接入方案。")
            self.verify_source()
            proposal = state["proposal"]
            request, identity = self._plan_identity(
                state["context"], proposal["request"]["datasets"][0]["subset_list"][0]
            )
            if identity != proposal["identity"]:
                raise AccuracyCompatibilityError("模型目标或接入契约已变化，请重新制定方案。")
            spec, _ = self.candidate(state["context"]["dataset_name"])
            base = self.accuracy.adapter
            adapter = EvalScopeAccuracyAdapter(base.evalscope_command, base.evalscope_source)
            adapter.source_validator = self.check_source_clean
            adapter.extra_datasets[state["context"]["dataset_name"]] = spec
            run = self.accuracy.create(request, validation_adapter=adapter)
            self._update(session_id, status="validating", validation_run_id=run.id, error=None)
            event = threading.Event()
            thread = threading.Thread(
                target=self._validate,
                args=(session_id, run.id, event),
                daemon=True,
                name=f"dataset-{session_id[:8]}",
            )
            self._active[session_id] = (thread, event)
            thread.start()
            return self.get(session_id)

    def _validate(self, session_id: str, run_id: str, event: threading.Event) -> None:
        try:
            while not event.wait(0.1):
                run = self.accuracy.store.get(run_id)
                if run.status not in {"queued", "preparing", "running", "parsing"}:
                    break
            if event.is_set():
                return
            with self._lock:
                state = self.get(session_id)
                if event.is_set() or state["status"] != "validating":
                    return
                name = state["context"]["dataset_name"]
                summary = run.summary
                primary = [m for m in run.metrics if m.is_primary]
                if (
                    run.status != "completed"
                    or not summary
                    or summary.incomplete
                    or summary.execution_requested != 2
                    or summary.execution_succeeded != 2
                    or summary.execution_errored
                    or not primary
                    or any(
                        m.dataset_name != name
                        or m.sample_count != 2
                        or m.metric_key not in {"acc:mean", "accuracy:mean"}
                        or m.metric_kind != "quality"
                        or not m.higher_is_better
                        or not 0 <= m.score <= 1
                        for m in primary
                    )
                ):
                    raise AccuracyCompatibilityError(
                        "试跑未产出完整的 2 条客观正确率报告，未启用数据集。"
                    )
                self.verify_source()
                _, fingerprint = self.candidate(name)
                request, identity = self._plan_identity(
                    state["context"], run.request.datasets[0].subset_list[0]
                )
                if identity != state["proposal"]["identity"]:
                    raise AccuracyCompatibilityError("验证期间模型或接入契约变化，未启用数据集。")
                if run.request != request:
                    raise AccuracyCompatibilityError("验证记录与确认方案不匹配。")
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO accuracy_dataset_integrations "
                        "(dataset_name,contract_fingerprint,validation_run_id,enabled_at) "
                        "VALUES(?,?,?,?)",
                        (name, fingerprint, run_id, to_iso(utc_now())),
                    )
                    connection.execute(
                        "UPDATE ai_dataset_integrations SET status='ready',error=NULL,updated_at=? "
                        "WHERE session_id=?",
                        (to_iso(utc_now()), session_id),
                    )
                    connection.commit()
        except Exception as error:
            with self._lock:
                if not event.is_set():
                    self._update(
                        session_id, status="blocked", error=self.accuracy._redact_log(str(error))
                    )
        finally:
            with self._lock:
                self._active.pop(session_id, None)

    def record_analysis(self, session_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(session_id)
            if state["status"] not in {"ready", "blocked", "interrupted"}:
                raise AccuracyCompatibilityError("请先读取接入检查或验证结果再记录解读。")
            self._update(session_id, analysis=text)
            return self.get(session_id)

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.get(session_id)
            if state["status"] == "ready":
                return state  # Cancelling a conversation does not disable verified integration.
            active = self._active.get(session_id)
            if active:
                active[1].set()
            if state["status"] == "validating" and state["validation_run_id"]:
                self.accuracy.cancel(state["validation_run_id"])
            self._update(session_id, status="cancelled", error=None)
            return self.get(session_id)

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active

    def stop_all(self) -> None:
        with self._lock:
            self._closed = True
            active = list(self._active.items())
        for session_id, (thread, _) in active:
            self.cancel(session_id)
            thread.join(timeout=5)
