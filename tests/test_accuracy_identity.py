from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_accuracy_api import (
    accept_identity,
    create_target,
    make_fake_evalscope,
    request_payload,
    settings_for,
)
from test_accuracy_store import make_resolved_config, make_store

from uniform_dgxspark.accuracy_identity import cache_fingerprint, evaluation_fingerprint
from uniform_dgxspark.main import create_app
from uniform_dgxspark.schemas import AccuracyRunCreate


def fingerprint(config=None):
    return evaluation_fingerprint(config or make_resolved_config(), "0.0.0_dev", "fixed-commit")


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "ceval"),
        ("subset_list", ["other"]),
        ("limit", 20),
        ("few_shot_num", 1),
        ("evaluation_version", "v2.0"),
        ("prompt_hash", "sha256:" + "b" * 64),
        ("dataset_revision", "other"),
        ("content_hash", "sha256:content"),
        ("dataset_id", "other/data"),
    ],
)
def test_dataset_identity_changes_with_every_scoring_setting(field, value) -> None:
    config = make_resolved_config()
    changed = config.model_copy(deep=True)
    setattr(changed.datasets[0], field, value)
    assert fingerprint(config) != fingerprint(changed)


@pytest.mark.parametrize(
    "field,value", [("seed", 13), ("eval_batch_size", 2), ("timeout_seconds", 100)]
)
def test_execution_settings_change_identity(field, value) -> None:
    config = make_resolved_config()
    changed = config.model_copy(update={field: value})
    assert fingerprint(config) != fingerprint(changed)


def test_generation_and_evalscope_identity_and_model_cache_boundaries() -> None:
    config = make_resolved_config()
    assert fingerprint(config) == fingerprint(config.model_copy(deep=True))
    for field, value in [("temperature", 0.5), ("max_tokens", 1024), ("top_p", 0.95)]:
        changed = config.model_copy(deep=True)
        setattr(changed.generation_config, field, value)
        assert fingerprint(config) != fingerprint(changed)
    assert fingerprint(config) != evaluation_fingerprint(config, "new-version", "fixed-commit")
    assert fingerprint(config) != evaluation_fingerprint(config, "0.0.0_dev", "other-commit")
    moved = config.model_copy(update={"deployment_snapshot": {"port": 9000}})
    assert fingerprint(config) == fingerprint(moved)
    assert cache_fingerprint(fingerprint(), "model-a", {}) != cache_fingerprint(
        fingerprint(), "model-b", {}
    )
    assert cache_fingerprint(fingerprint(), "model-a", {}) != cache_fingerprint(
        fingerprint(), "model-a", {"revision": "new"}
    )


def make_cached_runner(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json, sys, time
from pathlib import Path
args=sys.argv[1:]
def val(name): return args[args.index(name)+1]
work=Path(val('--work-dir')); model=val('--model'); dataset=val('--datasets')
config=json.loads(val('--dataset-args'))[dataset]; subsets=config['subset_list']
limit=int(val('--limit')); total=limit*len(subsets)
work.mkdir(parents=True,exist_ok=True)
(work/'configs').mkdir(exist_ok=True)
(work/'configs'/'task_config.yaml').write_text('fake_cache_identity: pinned\\n')
counts={}; reused=0; processed=0
for subset in subsets:
    prediction=work/'predictions'/model/(dataset+'_'+subset+'.jsonl')
    prediction.parent.mkdir(parents=True,exist_ok=True)
    cached=set()
    if prediction.exists() and '--use-cache' in args:
        cached={json.loads(line)['sample_id'] for line in prediction.read_text().splitlines()}
    reused+=len(cached)
    for index in range(limit):
        sid=subset+':'+str(index)
        if sid not in cached:
            time.sleep(0.15)
            with prediction.open('a') as stream: stream.write(json.dumps({'sample_id':sid})+'\\n')
        processed+=1
        (work/'progress.json').write_text(json.dumps({'status':'running','total_count':total,'processed_count':processed,'percent':processed/total*100}))
    counts[subset]={'requested':limit,'succeeded':limit,'errored':0}
if '--use-cache' in args:
    print(f'Reusing predictions from {work}, got {reused} predictions, '
          f'remaining {total-reused} samples',flush=True)
identity={'name':'accuracy','aggregation':'mean','dimensions':{}}
report={'schema_version':2,'model_name':model,'dataset_name':dataset,'dataset_pretty_name':dataset,
        'primary_metric_identity':identity,
        'metrics':[{'identity':identity,'num':total,'score':0.5,
                    'categories':[{'name':['default'],'num':total,'score':0.5,
                        'subsets':[{'name':subset,'num':limit,'score':0.5}
                                   for subset in subsets]}],
                    'semantics':{'metric_name':'Accuracy','kind':'quality','direction':'higher_is_better'}}],
        'execution_summary':{'requested':total,'succeeded':total,'errored':0,'incomplete':False,'subsets':counts}}
target=work/'reports'/model/(dataset+'.json')
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(json.dumps(report))
(work/'progress.json').write_text(json.dumps({'status':'completed','total_count':total,'processed_count':total,'percent':100}))
""",
        encoding="utf-8",
    )
    path.chmod(0o700)


def wait_terminal(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get(f"/api/v1/accuracy/runs/{run_id}").json()
        if run["status"] not in {"queued", "preparing", "running", "parsing"}:
            while (
                client.app.state.accuracy_manager.is_active(run_id) and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            return client.get(f"/api/v1/accuracy/runs/{run_id}").json()
        time.sleep(0.05)
    raise AssertionError("accuracy task timed out")


def test_api_comparison_cache_cancel_resume_report_and_mismatch(tmp_path: Path) -> None:
    executable = tmp_path / "fake-evalscope"
    make_cached_runner(executable)
    with TestClient(create_app(settings_for(tmp_path, executable))) as client:
        accept_identity(client)
        target = create_target(client)
        payload = request_payload(target)
        first = client.post("/api/v1/accuracy/runs", json=payload).json()
        completed = wait_terminal(client, first["id"])
        assert completed["status"] == "completed"
        assert completed["evaluation_fingerprint"]
        original = Path(completed["run_directory"])
        prediction = next(original.rglob("*.jsonl"))
        before = prediction.read_bytes()
        cached_payload = {**payload, "use_cache": True, "cache_source_run_id": first["id"]}
        second_response = client.post("/api/v1/accuracy/runs", json=cached_payload)
        assert second_response.status_code == 201, second_response.text
        second = wait_terminal(client, second_response.json()["id"])
        assert second["status"] == "completed"
        assert second["cached_samples"] == 2
        assert prediction.read_bytes() == before
        comparison = client.post(
            "/api/v1/accuracy/compare", json={"run_ids": [first["id"], second["id"]]}
        )
        assert comparison.status_code == 200
        assert len(comparison.json()["items"]) == 2
        report_path = "evalscope/00-gsm8k/reports/served-model/gsm8k.json"
        report = client.get(f"/api/v1/accuracy/runs/{second['id']}/artifacts/{report_path}")
        assert report.status_code == 200
        assert report.json()["metrics"][0]["score"] == second["metrics"][0]["score"]
        assert f"accuracy-{second['id']}-gsm8k.json" in report.headers["content-disposition"]
        assert (
            client.post(
                "/api/v1/accuracy/compare", json={"run_ids": [first["id"], first["id"]]}
            ).status_code
            == 422
        )
        changed = {**cached_payload, "generation_config": {"temperature": 0.5}}
        assert client.post("/api/v1/accuracy/runs", json=changed).status_code == 409
        other = client.post(
            "/api/v1/accuracy/runs", json={**payload, "generation_config": {"temperature": 0.5}}
        ).json()
        wait_terminal(client, other["id"])
        assert (
            client.post(
                "/api/v1/accuracy/compare", json={"run_ids": [first["id"], other["id"]]}
            ).status_code
            == 409
        )
        long_payload = request_payload(target)
        long_payload["datasets"][0]["limit"] = 20
        running = client.post("/api/v1/accuracy/runs", json=long_payload).json()
        deadline = time.monotonic() + 5
        while (
            not list(Path(running["run_directory"]).rglob("*.jsonl"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        client.post(f"/api/v1/accuracy/runs/{running['id']}/cancel")
        wait_terminal(client, running["id"])
        resumed = client.post(f"/api/v1/accuracy/runs/{running['id']}/resume")
        assert resumed.status_code == 201, resumed.text
        result = wait_terminal(client, resumed.json()["id"])
        assert result["status"] == "completed"
        assert result["cached_samples"] >= 1
        assert result["id"] != running["id"]
        assert client.get(f"/api/v1/accuracy/runs/{running['id']}").json()["status"] == "cancelled"
        assert client.post(f"/api/v1/accuracy/runs/{first['id']}/resume").status_code == 409


def test_cache_contract_requires_explicit_source() -> None:
    with pytest.raises(ValueError, match="explicit"):
        AccuracyRunCreate.model_validate({**request_payload("x"), "use_cache": True})


def test_stage_21_migration_preserves_unverified_legacy_runs(tmp_path: Path) -> None:
    from test_accuracy_store import create_run, make_metric, make_summary

    database, store = make_store(tmp_path)
    run = create_run(store, tmp_path)
    store.mark_preparing(run.id)
    store.mark_running(run.id, pid=1)
    store.mark_parsing(run.id)
    store.complete(run.id, make_summary(), [make_metric()])
    with database.connect() as connection:
        for column in ("evaluation_fingerprint", "cache_fingerprint", "cached_samples"):
            connection.execute(f"ALTER TABLE accuracy_runs DROP COLUMN {column}")
        connection.execute("DELETE FROM schema_migrations")
        connection.execute("INSERT INTO schema_migrations VALUES(21,'2026-10-22T00:00:00Z')")
        connection.commit()
    database.initialize()
    old = store.get(run.id)
    assert old.status == "completed" and old.metrics[0].score == 1
    assert old.evaluation_fingerprint is None and old.cache_fingerprint is None


def test_cache_rejects_symlinks_and_forged_manifest(tmp_path: Path) -> None:
    executable = tmp_path / "fake-evalscope"
    make_cached_runner(executable)
    with TestClient(create_app(settings_for(tmp_path, executable))) as client:
        accept_identity(client)
        target = create_target(client)
        payload = request_payload(target)
        run = client.post("/api/v1/accuracy/runs", json=payload).json()
        result = wait_terminal(client, run["id"])
        cached = {**payload, "use_cache": True, "cache_source_run_id": run["id"]}
        root = Path(result["run_directory"])
        outside = tmp_path / "outside-cache"
        outside.mkdir()
        (root / "evalscope" / "00-gsm8k" / "reviews").symlink_to(outside, target_is_directory=True)
        before = set(client.app.state.accuracy_manager.runs_directory.iterdir())
        assert client.post("/api/v1/accuracy/runs", json=cached).status_code == 409
        assert set(client.app.state.accuracy_manager.runs_directory.iterdir()) == before
        (root / "evalscope" / "00-gsm8k" / "reviews").unlink()
        (root / "uniform-accuracy-config.json").write_text('{"cache_fingerprint":"forged"}')
        assert client.post("/api/v1/accuracy/runs", json=cached).status_code == 409


def test_comparison_and_resume_inherit_api_key_and_disable_switch(tmp_path: Path) -> None:
    executable = tmp_path / "fake-evalscope"
    make_cached_runner(executable)
    with TestClient(
        create_app(settings_for(tmp_path, executable, api_token="test-secret"))
    ) as client:
        assert (
            client.post("/api/v1/accuracy/compare", json={"run_ids": ["a", "b"]}).status_code == 401
        )
        assert client.post("/api/v1/accuracy/runs/a/resume").status_code == 401
    disabled_path = tmp_path / "disabled"
    with TestClient(
        create_app(settings_for(disabled_path, executable, accuracy_enabled=False))
    ) as client:
        assert (
            client.post("/api/v1/accuracy/compare", json={"run_ids": ["a", "b"]}).status_code == 403
        )
        assert client.post("/api/v1/accuracy/runs/a/resume").status_code == 403


def test_read_only_rollback_preserves_history_and_reports(tmp_path: Path) -> None:
    executable = tmp_path / "fake-evalscope"
    make_cached_runner(executable)
    settings = settings_for(tmp_path, executable)
    with TestClient(create_app(settings)) as client:
        accept_identity(client)
        target = create_target(client)
        source = client.post("/api/v1/accuracy/runs", json=request_payload(target)).json()
        run = wait_terminal(client, source["id"])
    with TestClient(
        create_app(settings_for(tmp_path, executable, accuracy_creation_enabled=False))
    ) as client:
        assert client.get("/api/v1/accuracy/runs").json()["creation_enabled"] is False
        assert client.get(f"/api/v1/accuracy/runs/{run['id']}").status_code == 200
        path = "evalscope/00-gsm8k/reports/served-model/gsm8k.json"
        assert client.get(f"/api/v1/accuracy/runs/{run['id']}/artifacts/{path}").status_code == 200
        assert client.post("/api/v1/accuracy/runs", json=request_payload(target)).status_code == 403
        assert client.post(f"/api/v1/accuracy/runs/{run['id']}/resume").status_code == 403


def test_state_write_failure_does_not_orphan_evalscope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-evalscope"
    make_fake_evalscope(executable, delay_seconds=10)
    with TestClient(create_app(settings_for(tmp_path, executable))) as client:
        accept_identity(client)
        target = create_target(client)
        processes = []

        def fail_running(run_id: str, *, pid: int) -> None:
            processes.append(client.app.state.accuracy_manager._active[run_id].process)
            raise RuntimeError("simulated state write failure")

        monkeypatch.setattr(client.app.state.accuracy_store, "mark_running", fail_running)
        response = client.post("/api/v1/accuracy/runs", json=request_payload(target))
        run = wait_terminal(client, response.json()["id"])
        assert run["status"] == "failed"
        assert processes and processes[0].poll() is not None
