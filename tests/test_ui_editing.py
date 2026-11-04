from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from uniform_dgxspark.database import Database
from uniform_dgxspark.schemas import (
    UIEditCreate,
    UIEditRect,
    UIEditScreenshot,
    UIEditScreenshotContext,
    UIEditTarget,
)
from uniform_dgxspark.services.ui_editing import UIEditConflict, UIEditManager
from uniform_dgxspark.ui_edit_store import UIEditStore


def make_request(*, screenshot: UIEditScreenshot | None = None) -> UIEditCreate:
    return UIEditCreate(
        instruction="Change the visible title to New title",
        scope="element",
        target=UIEditTarget(
            ui_id="page.test",
            tag_name="h1",
            element_id="title",
            classes=["page-title"],
            text="Old title",
            html='<h1 id="title">Old title</h1>',
            dom_path="#title",
            view_id="view-test",
            page_url="http://127.0.0.1:18180/",
            computed_styles={"font-size": "24px"},
            rect=UIEditRect(x=10, y=20, width=200, height=40),
        ),
        screenshot=screenshot,
    )


def wait_for_job(manager: UIEditManager, job_id: str):  # type: ignore[no-untyped-def]
    for _ in range(400):
        job = manager.get(job_id)
        if job.status in {"applied", "applied_with_warnings", "failed", "stopped"}:
            return job
        time.sleep(0.01)
    raise AssertionError("UI edit did not finish")


def make_manager(tmp_path: Path, codex_body: str) -> tuple[UIEditManager, Path]:
    repository = tmp_path / "repository"
    web_dir = repository / "source-web"
    web_dir.mkdir(parents=True)
    (web_dir / "index.html").write_text(
        '<!doctype html><body><h1 id="title">Old title</h1>'
        '<script type="module" src="/static/app.js"></script></body>',
        encoding="utf-8",
    )
    (web_dir / "app.css").write_text("h1 { font-size: 24px; }\n", encoding="utf-8")
    (web_dir / "app.js").write_text('console.log("ready");\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repository, check=True
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repository, check=True
    )
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "resume = 'resume' in args[:4]\n"
        "print(f'args={args}', flush=True)\n"
        "images = [args[index + 1] for index, value in enumerate(args) if value == '-i']\n"
        "output = Path(args[args.index('--output-last-message') + 1])\n"
        "print('Authorization: Bearer very-secret-token', flush=True)\n"
        "print(f'images={len(images)}', flush=True)\n"
        "event = {'type':'thread.started', 'thread_id':Path.cwd().name}\n"
        "print(json.dumps(event), flush=True)\n"
        "print(json.dumps({'type':'turn.started'}), flush=True)\n"
        "item = {'type':'command_execution','command':'edit web files'}\n"
        "print(json.dumps({'type':'item.started','item':item}), flush=True)\n"
        f"{codex_body}\n"
        "item = {'type':'command_execution','command':'edit web files', "
        "'aggregated_output':'tests completed'}\n"
        "print(json.dumps({'type':'item.completed','item':item}), flush=True)\n"
        "item = {'type':'agent_message', "
        "'text':'Implemented and tested the requested UI change.'}\n"
        "print(json.dumps({'type':'item.completed','item':item}), flush=True)\n"
        "output.write_text('Implemented and tested the requested UI change.', encoding='utf-8')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o700)
    database = Database(tmp_path / "data" / "uniform.db")
    database.initialize()
    manager = UIEditManager(
        UIEditStore(database),
        web_dir,
        tmp_path / "data" / "ui-edits",
        codex_binary=str(fake_codex),
    )
    return manager, web_dir


def test_ui_edit_directly_edits_commits_and_can_revert(tmp_path: Path) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'New title'))\n"
        "Path('web/ui-helper.js').write_text('export const edited = true;\\n')",
    )

    job = wait_for_job(manager, manager.submit(make_request()).id)

    assert job.status == "applied"
    assert job.changed_files == ["index.html", "ui-helper.js"]
    assert job.commit_sha
    assert job.codex_session_id == job.id
    assert "New title" in (web_dir / "index.html").read_text(encoding="utf-8")
    assert (web_dir / "ui-helper.js").is_file()
    logs = manager.logs(job.id)
    assert "--no-daemon" in logs.logs
    assert "very-secret-token" not in logs.logs
    assert "Bearer [REDACTED]" in logs.logs
    events = manager.conversation(job.id).events
    assert {event.kind for event in events} >= {"user", "command", "output", "assistant"}

    reverted = manager.revert(job.id, "The visual result was too prominent")

    assert reverted.status == "reverted"
    assert reverted.rollback_reason == "The visual result was too prominent"
    assert reverted.revert_commit_sha
    assert "Old title" in (web_dir / "index.html").read_text(encoding="utf-8")
    assert not (web_dir / "ui-helper.js").exists()
    feedback = manager.store.rollback_feedback()
    assert feedback[0]["rollback_reason"] == "The visual result was too prominent"


def test_ui_edit_refuses_to_overwrite_a_newer_manual_change(tmp_path: Path) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'New title'))",
    )
    job = wait_for_job(manager, manager.submit(make_request()).id)
    assert job.status == "applied"
    (web_dir / "index.html").write_text(
        (web_dir / "index.html").read_text(encoding="utf-8") + "\n<!-- manual -->\n",
        encoding="utf-8",
    )

    with pytest.raises(UIEditConflict, match="could not revert"):
        manager.revert(job.id, "I changed it manually afterward")


def test_validation_warning_is_repaired_then_applied_without_blocking(
    tmp_path: Path,
) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "Path('web/app.js').write_text('const broken = ;\\n')",
    )

    job = wait_for_job(manager, manager.submit(make_request()).id)

    assert job.status == "applied_with_warnings"
    assert job.warnings
    assert "语法检查" in job.warnings[0]
    assert (web_dir / "app.js").read_text(encoding="utf-8") == "const broken = ;\n"
    assert any(event.kind == "warning" for event in manager.conversation(job.id).events)


def test_human_guidance_continues_the_same_codex_session(tmp_path: Path) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "time.sleep(0.1)\n"
        "path = Path('web/index.html')\n"
        "current = path.read_text()\n"
        "if resume:\n"
        "    current = current.replace('New title', 'Guided title')\n"
        "else:\n"
        "    current = current.replace('Old title', 'New title')\n"
        "path.write_text(current)",
    )
    submitted = manager.submit(make_request())
    manager.intervene(submitted.id, "Use Guided title instead")

    job = wait_for_job(manager, submitted.id)

    assert job.status == "applied"
    assert "Guided title" in (web_dir / "index.html").read_text(encoding="utf-8")
    user_messages = [
        event.content
        for event in manager.conversation(job.id).events
        if event.kind == "user"
    ]
    assert user_messages == ["Change the visible title to New title", "Use Guided title instead"]


def test_user_can_stop_an_edit_before_it_is_applied(tmp_path: Path) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "time.sleep(5)\n"
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'Too late'))",
    )
    submitted = manager.submit(make_request())

    stopped = manager.stop(submitted.id)
    job = wait_for_job(manager, submitted.id)

    assert stopped.status == "stopped"
    assert job.status == "stopped"
    assert "Old title" in (web_dir / "index.html").read_text(encoding="utf-8")


def test_separate_ui_edits_keep_independent_workspaces_and_codex_sessions(
    tmp_path: Path,
) -> None:
    manager, _ = make_manager(
        tmp_path,
        "path = Path('web/index.html')\n"
        "current = path.read_text()\n"
        "current = current.replace('Old title', 'First title') if 'Old title' in current "
        "else current.replace('First title', 'Second title')\n"
        "path.write_text(current)",
    )

    first = wait_for_job(manager, manager.submit(make_request()).id)
    second_request = make_request().model_copy(
        update={"instruction": "Change First title to Second title"}
    )
    second = wait_for_job(manager, manager.submit(second_request).id)

    assert first.status == second.status == "applied"
    assert first.codex_session_id == first.id
    assert second.codex_session_id == second.id
    assert first.codex_session_id != second.codex_session_id
    assert manager.store.metadata(first.id)["workspace_path"] != manager.store.metadata(
        second.id
    )["workspace_path"]
    assert [
        event.content
        for event in manager.conversation(second.id).events
        if event.kind == "user"
    ] == ["Change First title to Second title"]


def test_legacy_cli_log_is_reconstructed_as_readable_conversation(
    tmp_path: Path,
) -> None:
    manager, _ = make_manager(
        tmp_path,
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'New title'))",
    )
    job = wait_for_job(manager, manager.submit(make_request()).id)
    workspace = Path(manager.store.metadata(job.id)["workspace_path"])
    (workspace / "conversation.jsonl").unlink()
    (workspace / "agent.log").write_text(
        'OpenAI Codex\n--------\n"valid JSON scalar"\nuser\nRead instructions\n'
        "codex\nI will make the requested change.\n"
        "exec\nnode --check web/app.js\n succeeded in 12ms:\nSyntax OK\n",
        encoding="utf-8",
    )

    conversation = manager.conversation(job.id)

    assert [event.kind for event in conversation.events] == [
        "user",
        "status",
        "assistant",
        "command",
        "output",
        "result",
    ]
    assert conversation.events[0].content == "Change the visible title to New title"
    assert conversation.events[2].content == "I will make the requested change."
    assert conversation.events[3].content == "node --check web/app.js"
    assert conversation.events[4].content == "succeeded in 12ms:\nSyntax OK"


def test_screenshot_edit_passes_full_annotated_and_detail_images_to_codex(
    tmp_path: Path,
) -> None:
    manager, web_dir = make_manager(
        tmp_path,
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'New title'))",
    )
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
    )
    screenshot = UIEditScreenshot(
        full_image_data_url=tiny_png,
        annotated_image_data_url=tiny_png,
        detail_image_data_url=tiny_png,
        selection=UIEditRect(x=10, y=20, width=200, height=100),
        image_width=1440,
        image_height=900,
        viewport_width=1440,
        viewport_height=900,
        device_pixel_ratio=1,
        dom_paths=["#title", "main > section"],
        context_images=[
            UIEditScreenshotContext(
                kind="page_segment",
                label="Full page segment 1/2",
                image_data_url=tiny_png,
                image_width=1440,
                image_height=900,
                offset_y=900,
            ),
            UIEditScreenshotContext(
                kind="scroll_region",
                label="Dialog segment 2/2",
                image_data_url=tiny_png,
                image_width=800,
                image_height=600,
                offset_y=600,
                dom_path="#history-dialog > section",
            ),
        ],
    )

    job = wait_for_job(manager, manager.submit(make_request(screenshot=screenshot)).id)

    assert job.status == "applied"
    assert "images=5" in manager.logs(job.id).logs
    workspace = Path(manager.store.metadata(job.id)["workspace_path"])
    assert (workspace / "screenshot-full.png").is_file()
    assert (workspace / "screenshot-annotated.png").is_file()
    assert (workspace / "screenshot-detail.png").is_file()
    assert (workspace / "screenshot-context-01.png").is_file()
    assert (workspace / "screenshot-context-02.png").is_file()
    context = (workspace / "UI_EDIT_CONTEXT.json").read_text(encoding="utf-8")
    assert "reference_images" in context
    assert "Full page segment 1/2" in context
    assert "#history-dialog > section" in context
    assert "data:image" not in context
    assert "New title" in (web_dir / "index.html").read_text(encoding="utf-8")


def test_failed_ui_edit_can_retry_with_error_context_and_original_screenshot(
    tmp_path: Path,
) -> None:
    first_attempt = tmp_path / "first-attempt"
    manager, web_dir = make_manager(
        tmp_path,
        f"marker = Path({str(first_attempt)!r})\n"
        "if not marker.exists():\n"
        "    marker.write_text('failed once')\n"
        "    raise SystemExit(3)\n"
        "path = Path('web/index.html')\n"
        "path.write_text(path.read_text().replace('Old title', 'Retried title'))",
    )
    tiny_png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
    )
    screenshot = UIEditScreenshot(
        full_image_data_url=tiny_png,
        annotated_image_data_url=tiny_png,
        detail_image_data_url=tiny_png,
        selection=UIEditRect(x=10, y=20, width=200, height=100),
        image_width=1440,
        image_height=900,
        viewport_width=1440,
        viewport_height=900,
        device_pixel_ratio=1,
        dom_paths=["#title"],
        context_images=[
            UIEditScreenshotContext(
                kind="page_segment",
                label="Full page bottom",
                image_data_url=tiny_png,
                image_width=1440,
                image_height=900,
                offset_y=900,
            )
        ],
    )
    failed = wait_for_job(manager, manager.submit(make_request(screenshot=screenshot)).id)
    assert failed.status == "failed"

    retried = wait_for_job(manager, manager.retry(failed.id).id)

    assert retried.id != failed.id
    assert retried.status == "applied"
    assert "Retried title" in (web_dir / "index.html").read_text(encoding="utf-8")
    retry_workspace = Path(manager.store.metadata(retried.id)["workspace_path"])
    retry_context = json.loads((retry_workspace / "UI_EDIT_CONTEXT.json").read_text())
    assert retry_context["retry_context"]["previous_job_id"] == failed.id
    assert retry_context["retry_context"]["previous_error"]
    assert "images=4" in manager.logs(retried.id).logs
    assert (retry_workspace / "screenshot-context-01.png").is_file()
    failed_events = manager.conversation(failed.id).events
    assert any(retried.id in event.content for event in failed_events)

    with pytest.raises(UIEditConflict, match="only failed"):
        manager.retry(retried.id)
