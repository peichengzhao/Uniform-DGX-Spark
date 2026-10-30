from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..schemas import (
    AccuracyMetricRead,
    AccuracyResolvedConfig,
    AccuracyResolvedDataset,
    AccuracyRunCreate,
    AccuracyRunProgress,
)

EXPECTED_EVALSCOPE_VERSION = "0.0.0_dev"
EXPECTED_EVALSCOPE_COMMIT = "ccb9019ff56fd7dcc8602f4d17b706b31228edca"
EXPECTED_REPORT_SCHEMA_VERSION = 2
MAX_ARTIFACT_BYTES = 20_000_000


class EvalScopeAccuracyError(RuntimeError):
    pass


class EvalScopeIdentityError(EvalScopeAccuracyError):
    pass


class EvalScopeReportError(EvalScopeAccuracyError):
    pass


@dataclass(frozen=True, slots=True)
class EvalScopeIdentity:
    version: str
    commit: str


@dataclass(frozen=True, slots=True)
class _DatasetSpec:
    dataset_id: str
    pretty_name: str
    evaluation_version: str
    subsets: frozenset[str]
    prompt_signature: str
    zero_shot_only: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedEvalScopeReport:
    metrics: list[AccuracyMetricRead]
    execution_requested: int
    execution_succeeded: int
    execution_errored: int
    incomplete: bool


_CEVAL_SUBJECTS = frozenset(
    {
        "computer_network",
        "operating_system",
        "computer_architecture",
        "college_programming",
        "college_physics",
        "college_chemistry",
        "advanced_mathematics",
        "probability_and_statistics",
        "discrete_mathematics",
        "electrical_engineer",
        "metrology_engineer",
        "high_school_mathematics",
        "high_school_physics",
        "high_school_chemistry",
        "high_school_biology",
        "middle_school_mathematics",
        "middle_school_biology",
        "middle_school_physics",
        "middle_school_chemistry",
        "veterinary_medicine",
        "college_economics",
        "business_administration",
        "marxism",
        "mao_zedong_thought",
        "education_science",
        "teacher_qualification",
        "high_school_politics",
        "high_school_geography",
        "middle_school_politics",
        "middle_school_geography",
        "modern_chinese_history",
        "ideological_and_moral_cultivation",
        "logic",
        "law",
        "chinese_language_and_literature",
        "art_studies",
        "professional_tour_guide",
        "legal_professional",
        "high_school_chinese",
        "high_school_history",
        "middle_school_history",
        "civil_servant",
        "sports_science",
        "plant_protection",
        "basic_medicine",
        "clinical_medicine",
        "urban_and_rural_planner",
        "accountant",
        "fire_engineer",
        "environmental_impact_assessment_engineer",
        "tax_accountant",
        "physician",
    }
)

_DATASETS = {
    "gsm8k": _DatasetSpec(
        dataset_id="AI-ModelScope/gsm8k",
        pretty_name="GSM8K",
        evaluation_version="v1.1",
        subsets=frozenset({"main"}),
        prompt_signature=(
            "{question}\nPlease reason step by step, and put your final answer within "
            "\\\\boxed{{}}."
        ),
    ),
    "arc": _DatasetSpec(
        dataset_id="allenai/ai2_arc",
        pretty_name="ARC",
        evaluation_version="v1.0",
        subsets=frozenset({"ARC-Easy", "ARC-Challenge"}),
        prompt_signature=(
            "Answer the following multiple choice question. The entire content of your "
            "response should be of the following format: 'ANSWER: [LETTER]' where [LETTER] "
            "is one of {letters}.\n\n{question}\n\n{choices}"
        ),
    ),
    "ceval": _DatasetSpec(
        dataset_id="evalscope/ceval",
        pretty_name="C-Eval",
        evaluation_version="v1.1",
        subsets=_CEVAL_SUBJECTS,
        prompt_signature=(
            "以下是中国关于{subject}的单项选择题，请选出其中的正确答案。回答最后一行必须为："
            "答案：[LETTER]。\n\n问题：{question}\n选项：\n{choices}"
        ),
    ),
}


class EvalScopeAccuracyAdapter:
    """Build pinned EvalScope commands and normalize its schema-v2 reports."""

    version = EXPECTED_EVALSCOPE_VERSION
    commit = EXPECTED_EVALSCOPE_COMMIT

    def __init__(self, evalscope_command: Path, evalscope_source: Path) -> None:
        self.evalscope_command = evalscope_command
        self.evalscope_source = evalscope_source
        self.dataset_provider: Callable[[], dict[str, _DatasetSpec]] | None = None
        self.source_validator: Callable[[], None] | None = None
        self.extra_datasets: dict[str, _DatasetSpec] = {}

    def _datasets(self) -> dict[str, _DatasetSpec]:
        extra = self.dataset_provider() if self.dataset_provider else {}
        return {**_DATASETS, **extra, **self.extra_datasets}

    def dataset_catalog(self=None) -> dict[str, dict[str, Any]]:
        # Class-level calls used by the offline exporter retain the built-in catalog.
        datasets = self._datasets() if self else _DATASETS
        return {
            name: {
                "subsets": sorted(spec.subsets),
                "version": spec.evaluation_version,
                **({"zero_shot_only": True} if spec.zero_shot_only else {}),
            }
            for name, spec in datasets.items()
        }

    def verify_identity(self) -> EvalScopeIdentity:
        command = self.evalscope_command.resolve(strict=True)
        source = self.evalscope_source.resolve(strict=True)
        try:
            version = subprocess.run(
                [str(command), "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise EvalScopeIdentityError(f"could not verify EvalScope identity: {error}") from error
        expected_version = f"evalscope {self.version}"
        if version != expected_version:
            raise EvalScopeIdentityError(f"expected {expected_version!r}, got {version!r}")
        if commit != self.commit:
            raise EvalScopeIdentityError(f"expected EvalScope commit {self.commit}, got {commit}")
        if self.source_validator:
            self.source_validator()
        return EvalScopeIdentity(version=self.version, commit=commit)

    def resolve_config(
        self,
        request: AccuracyRunCreate,
        deployment_snapshot: dict[str, Any],
    ) -> AccuracyResolvedConfig:
        datasets: list[AccuracyResolvedDataset] = []
        for requested in request.datasets:
            spec = self._datasets().get(requested.name)
            if spec is None:
                raise EvalScopeAccuracyError(f"dataset is not integrated: {requested.name}")
            if spec.zero_shot_only and requested.few_shot_num != 0:
                raise EvalScopeAccuracyError("newly integrated datasets currently require 0-shot")
            unknown = sorted(set(requested.subset_list) - spec.subsets)
            if unknown:
                raise EvalScopeAccuracyError(
                    f"unsupported {requested.name} subsets: {', '.join(unknown)}"
                )
            prompt_hash = hashlib.sha256(spec.prompt_signature.encode("utf-8")).hexdigest()
            datasets.append(
                AccuracyResolvedDataset(
                    name=requested.name,
                    dataset_id=spec.dataset_id,
                    dataset_hub="modelscope",
                    dataset_revision=None,
                    content_hash=None,
                    evaluation_version=spec.evaluation_version,
                    subset_list=requested.subset_list,
                    limit=requested.limit,
                    few_shot_num=requested.few_shot_num,
                    prompt_hash=f"sha256:{prompt_hash}",
                )
            )
        return AccuracyResolvedConfig(
            datasets=datasets,
            generation_config=request.generation_config,
            eval_batch_size=request.eval_batch_size,
            timeout_seconds=request.timeout_seconds,
            seed=request.seed,
            deployment_snapshot=deployment_snapshot,
        )

    def build_command(
        self,
        *,
        model_name: str,
        service_url: str,
        dataset: AccuracyResolvedDataset,
        config: AccuracyResolvedConfig,
        run_directory: Path,
        work_directory: Path,
        use_cache: bool = False,
    ) -> list[str]:
        if not model_name or model_name.strip() != model_name:
            raise EvalScopeAccuracyError("model name must be non-empty without outer whitespace")
        service_url = self._validate_service_url(service_url)
        dataset_args = {
            dataset.name: {
                "subset_list": dataset.subset_list,
                "few_shot_num": dataset.few_shot_num,
            }
        }
        generation = config.generation_config.model_dump()
        generation["timeout"] = config.timeout_seconds
        command = [
            str(self.evalscope_command.resolve()),
            "eval",
            "--model",
            model_name,
            "--model-id",
            model_name,
            "--api-url",
            f"{service_url.rstrip('/')}/v1",
            "--api-key",
            "EMPTY",
            "--eval-type",
            "openai_api",
            "--datasets",
            dataset.name,
            "--dataset-hub",
            dataset.dataset_hub,
            "--dataset-dir",
            str(run_directory / "dataset-cache"),
            "--dataset-args",
            json.dumps(dataset_args, ensure_ascii=False, separators=(",", ":")),
            "--limit",
            str(dataset.limit),
            "--eval-batch-size",
            str(config.eval_batch_size),
            "--generation-config",
            json.dumps(generation, separators=(",", ":")),
            "--work-dir",
            str(work_directory),
            "--no-timestamp",
            "--enable-progress-tracker",
            "--no-collect-perf",
            "--seed",
            str(config.seed),
        ]
        if use_cache:
            # EvalScope writes into use_cache itself: only pass the new isolated copy.
            command.extend(["--use-cache", str(work_directory)])
        return command

    @staticmethod
    def _validate_service_url(service_url: str) -> str:
        try:
            parsed = urlparse(service_url)
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as error:
            raise EvalScopeAccuracyError("invalid EvalScope model service URL") from error
        if (
            parsed.scheme != "http"
            or not address.is_loopback
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise EvalScopeAccuracyError("EvalScope model service must use loopback HTTP")
        return f"http://{parsed.netloc.rstrip('/')}"

    @staticmethod
    def build_environment(run_directory: Path) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in (
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "all_proxy",
        ):
            if value := os.environ.get(name):
                environment[name] = value
        environment.update(
            {
                "NO_PROXY": "127.0.0.1,localhost,::1",
                "no_proxy": "127.0.0.1,localhost,::1",
                "MODELSCOPE_CACHE": str(run_directory / "modelscope-cache"),
                "EVALSCOPE_CACHE": str(run_directory / "evalscope-cache"),
                "HF_HOME": str(run_directory / "huggingface-cache"),
                "XDG_CACHE_HOME": str(run_directory / "xdg-cache"),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return environment

    def read_progress(self, work_directory: Path) -> AccuracyRunProgress | None:
        path = work_directory / "progress.json"
        if not path.is_file():
            return None
        try:
            payload = self._load_json(path, work_directory)
            total = self._integer(payload.get("total_count"), "progress.total_count")
            processed = self._integer(payload.get("processed_count"), "progress.processed_count")
            percent = self._number(payload.get("percent"), "progress.percent")
        except EvalScopeReportError:
            return None
        if processed > total:
            return None
        return AccuracyRunProgress(
            phase="running",
            processed_samples=processed,
            total_samples=total,
            percent=max(0.0, min(100.0, percent)),
            message=f"EvalScope {payload.get('status') or 'running'}",
        )

    def has_report(self, work_directory: Path, dataset_name: str) -> bool:
        return bool(list((work_directory / "reports").glob(f"**/{dataset_name}.json")))

    def normalize_report(
        self,
        work_directory: Path,
        dataset: AccuracyResolvedDataset,
        expected_model: str,
    ) -> NormalizedEvalScopeReport:
        candidates = sorted((work_directory / "reports").glob(f"**/{dataset.name}.json"))
        if len(candidates) != 1:
            raise EvalScopeReportError(
                f"expected one {dataset.name} report, found {len(candidates)}"
            )
        report = self._load_json(candidates[0], work_directory)
        schema_version = self._integer(report.get("schema_version"), "schema_version")
        if schema_version != EXPECTED_REPORT_SCHEMA_VERSION:
            raise EvalScopeReportError(
                f"unsupported EvalScope report schema_version={schema_version}"
            )
        if report.get("dataset_name") != dataset.name:
            raise EvalScopeReportError("report dataset_name does not match the request")
        if report.get("model_name") != expected_model:
            raise EvalScopeReportError("report model_name does not match the resolved model")
        pretty_name = report.get("dataset_pretty_name")
        if not isinstance(pretty_name, str) or not pretty_name:
            pretty_name = self._datasets()[dataset.name].pretty_name

        execution = self._mapping(report.get("execution_summary"), "execution_summary")
        requested = self._integer(execution.get("requested"), "execution_summary.requested")
        succeeded = self._integer(execution.get("succeeded"), "execution_summary.succeeded")
        errored = self._integer(execution.get("errored"), "execution_summary.errored")
        incomplete = execution.get("incomplete")
        if not isinstance(incomplete, bool):
            raise EvalScopeReportError("execution_summary.incomplete must be boolean")
        if succeeded + errored > requested:
            raise EvalScopeReportError("execution counts exceed requested samples")
        execution_subsets = execution.get("subsets")
        if not isinstance(execution_subsets, dict):
            execution_subsets = {}

        primary = self._mapping(report.get("primary_metric_identity"), "primary_metric_identity")
        primary_key = self._identity_key(primary)
        raw_metrics = self._list(report.get("metrics"), "metrics")
        matching_primary = 0
        metrics: list[AccuracyMetricRead] = []
        seen: set[tuple[str, str, tuple[str, ...], str]] = set()
        for index, raw_value in enumerate(raw_metrics):
            raw_metric = self._mapping(raw_value, f"metrics[{index}]")
            identity = self._mapping(raw_metric.get("identity"), f"metrics[{index}].identity")
            identity_key = self._identity_key(identity)
            is_primary = identity_key == primary_key
            matching_primary += int(is_primary)
            semantics = self._mapping(raw_metric.get("semantics"), f"metrics[{index}].semantics")
            kind = semantics.get("kind")
            if kind not in {"quality", "diagnostic"}:
                raise EvalScopeReportError(f"metrics[{index}] has unsupported semantic kind")
            direction = semantics.get("direction")
            if direction not in {"higher_is_better", "lower_is_better"}:
                raise EvalScopeReportError(f"metrics[{index}] has unsupported direction")
            metric_key = self._metric_key(identity)
            display_name = (
                semantics.get("display_name")
                or semantics.get("metric_name")
                or identity.get("name")
            )
            if not isinstance(display_name, str) or not display_name:
                raise EvalScopeReportError(f"metrics[{index}] has no display name")
            leaves = self._metric_leaves(raw_metric, dataset, requested, succeeded, errored)
            for category_path, subset_name, sample_count, score in leaves:
                counts = execution_subsets.get(subset_name)
                if isinstance(counts, dict):
                    leaf_requested = self._integer(
                        counts.get("requested"),
                        f"execution_summary.subsets.{subset_name}.requested",
                    )
                    leaf_succeeded = self._integer(
                        counts.get("succeeded"),
                        f"execution_summary.subsets.{subset_name}.succeeded",
                    )
                    leaf_errored = self._integer(
                        counts.get("errored"), f"execution_summary.subsets.{subset_name}.errored"
                    )
                else:
                    leaf_requested, leaf_succeeded, leaf_errored = requested, succeeded, errored
                unique = (dataset.name, metric_key, tuple(category_path), subset_name)
                if unique in seen:
                    raise EvalScopeReportError("report contains duplicate normalized metrics")
                seen.add(unique)
                metrics.append(
                    AccuracyMetricRead(
                        dataset_name=dataset.name,
                        dataset_pretty_name=pretty_name,
                        evaluation_version=dataset.evaluation_version,
                        metric_key=metric_key,
                        metric_display_name=display_name,
                        metric_kind=kind,
                        higher_is_better=direction == "higher_is_better",
                        category_path=category_path,
                        subset_name=subset_name,
                        sample_count=sample_count,
                        score=score,
                        is_primary=is_primary,
                        execution_requested=leaf_requested,
                        execution_succeeded=leaf_succeeded,
                        execution_errored=leaf_errored,
                    )
                )
        if matching_primary != 1:
            raise EvalScopeReportError(
                "primary_metric_identity must match exactly one report metric"
            )
        if not metrics:
            raise EvalScopeReportError("report contains no normalized metrics")
        return NormalizedEvalScopeReport(
            metrics=metrics,
            execution_requested=requested,
            execution_succeeded=succeeded,
            execution_errored=errored,
            incomplete=incomplete,
        )

    def _metric_leaves(
        self,
        metric: dict[str, Any],
        dataset: AccuracyResolvedDataset,
        requested: int,
        succeeded: int,
        errored: int,
    ) -> list[tuple[list[str], str, int, float]]:
        del requested, succeeded, errored
        categories = metric.get("categories")
        if not isinstance(categories, list) or not categories:
            subset_name = dataset.subset_list[0] if len(dataset.subset_list) == 1 else "all"
            return [
                (
                    [],
                    subset_name,
                    self._integer(metric.get("num"), "metric.num"),
                    self._number(metric.get("score"), "metric.score"),
                )
            ]
        leaves: list[tuple[list[str], str, int, float]] = []
        for category_index, raw_category in enumerate(categories):
            category = self._mapping(raw_category, f"categories[{category_index}]")
            names = category.get("name")
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(name, str) and name for name in names)
            ):
                raise EvalScopeReportError("metric category name must be non-empty text list")
            if len(names) > 16:
                raise EvalScopeReportError("metric category path is too deep")
            subsets = category.get("subsets")
            if not isinstance(subsets, list) or not subsets:
                leaves.append(
                    (
                        names,
                        "all",
                        self._integer(category.get("num"), "category.num"),
                        self._number(category.get("score"), "category.score"),
                    )
                )
                continue
            for subset_index, raw_subset in enumerate(subsets):
                subset = self._mapping(
                    raw_subset, f"categories[{category_index}].subsets[{subset_index}]"
                )
                name = subset.get("name")
                if not isinstance(name, str) or not name:
                    raise EvalScopeReportError("metric subset name must be non-empty text")
                leaves.append(
                    (
                        names,
                        name,
                        self._integer(subset.get("num"), "subset.num"),
                        self._number(subset.get("score"), "subset.score"),
                    )
                )
        return leaves

    @staticmethod
    def _identity_key(identity: dict[str, Any]) -> tuple[str, str, str]:
        name = identity.get("name")
        aggregation = identity.get("aggregation")
        dimensions = identity.get("dimensions")
        if not isinstance(name, str) or not name:
            raise EvalScopeReportError("metric identity.name must be non-empty text")
        if not isinstance(aggregation, str) or not aggregation:
            raise EvalScopeReportError("metric identity.aggregation must be non-empty text")
        if not isinstance(dimensions, dict):
            raise EvalScopeReportError("metric identity.dimensions must be an object")
        return name, aggregation, json.dumps(dimensions, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _metric_key(cls, identity: dict[str, Any]) -> str:
        name, aggregation, dimensions = cls._identity_key(identity)
        base = f"{name}:{aggregation}"
        if dimensions != "{}":
            digest = hashlib.sha256(dimensions.encode("utf-8")).hexdigest()[:12]
            base = f"{base}:{digest}"
        if len(base) > 255:
            raise EvalScopeReportError("normalized metric key is too long")
        return base

    @staticmethod
    def _load_json(path: Path, root: Path) -> dict[str, Any]:
        root = root.resolve()
        resolved = path.resolve()
        if path.is_symlink() or root not in resolved.parents:
            raise EvalScopeReportError("artifact path escapes the run directory")
        if not resolved.is_file():
            raise EvalScopeReportError(f"artifact is not a regular file: {path.name}")
        if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
            raise EvalScopeReportError(f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        try:
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EvalScopeReportError(f"invalid JSON artifact {path.name}: {error}") from error
        return EvalScopeAccuracyAdapter._mapping(value, path.name)

    @staticmethod
    def _mapping(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise EvalScopeReportError(f"{label} must be an object")
        return value

    @staticmethod
    def _list(value: Any, label: str) -> list[Any]:
        if not isinstance(value, list):
            raise EvalScopeReportError(f"{label} must be an array")
        return value

    @staticmethod
    def _integer(value: Any, label: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvalScopeReportError(f"{label} must be a non-negative integer")
        return value

    @staticmethod
    def _number(value: Any, label: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise EvalScopeReportError(f"{label} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise EvalScopeReportError(f"{label} must be finite")
        return number
