from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .accuracy_store import AccuracyRunNotFound
from .ai_store import AISessionNotFound
from .config import Settings
from .services.accuracy import AccuracyBusy, AccuracyCompatibilityError, AccuracyTargetError
from .services.ai_accuracy import (
    AIAccuracyAnalysis,
    AIAccuracyConfirmation,
    AIAccuracyManager,
    AIAccuracyProposal,
)
from .services.evalscope_accuracy import EvalScopeAccuracyError
from .services.model_targets import ModelTargetError
from .store import DeploymentNotFound


def register_ai_accuracy_routes(app: FastAPI, settings: Settings) -> None:
    router = APIRouter()
    user_path = "/api/v1/ai/sessions/{session_id}/accuracy"
    tool_path = "/api/v1/ai/sessions/{session_id}/actions/accuracy"

    def guard(
        request: Request, session_id: str, *, token: str | None = None, mutate: bool = False
    ) -> AIAccuracyManager:
        if not settings.accuracy_enabled:
            raise HTTPException(status_code=403, detail="accuracy evaluation is disabled")
        if mutate and (not settings.accuracy_creation_enabled or not settings.ai_sessions_enabled):
            raise HTTPException(status_code=403, detail="AI accuracy task creation is disabled")
        terminal = request.app.state.ai_terminal
        if token is not None and not terminal.validate_capability(session_id, token):
            raise HTTPException(status_code=403, detail="invalid AI accuracy capability")
        try:
            session = terminal.store.get(session_id)
        except AISessionNotFound as error:
            raise HTTPException(status_code=404, detail="AI accuracy session not found") from error
        if session.task_type != "accuracy_evaluation":
            raise HTTPException(status_code=403, detail="this capability is not for accuracy")
        if mutate and session.archived_at:
            raise HTTPException(status_code=409, detail="restore the AI session before replanning")
        return request.app.state.ai_accuracy

    def call(action: Callable[..., Any], *args: Any) -> Any:
        try:
            return action(*args)
        except (AISessionNotFound, AccuracyRunNotFound, DeploymentNotFound) as error:
            raise HTTPException(status_code=404, detail="accuracy resource not found") from error
        except (
            AccuracyCompatibilityError,
            AccuracyBusy,
            AccuracyTargetError,
            ModelTargetError,
        ) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except EvalScopeAccuracyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(
                status_code=503, detail="accuracy environment/storage unavailable"
            ) from error

    @router.get("/api/v1/accuracy/ai/sessions")
    def workflows(request: Request) -> dict[str, Any]:
        if not settings.accuracy_enabled or not settings.ai_sessions_enabled:
            raise HTTPException(status_code=403, detail="AI accuracy is disabled")
        sessions, total = request.app.state.ai_store.list(task_type="accuracy_evaluation")
        return {
            "items": [
                {
                    "session": session,
                    "workflow": call(request.app.state.ai_accuracy.get, session.id),
                }
                for session in sessions
            ],
            "total": total,
        }

    @router.get(user_path)
    def status(request: Request, session_id: str) -> dict[str, Any]:
        return call(guard(request, session_id).get, session_id)

    @router.post(user_path + "/confirm")
    def confirm(
        request: Request, session_id: str, payload: AIAccuracyConfirmation
    ) -> dict[str, Any]:
        # A capability has no right to approve its own proposal, even with an API key.
        if request.headers.get("X-AI-Capability"):
            raise HTTPException(status_code=403, detail="only the user can confirm the plan")
        manager = guard(request, session_id, mutate=True)
        result = call(manager.confirm, session_id, payload.proposal_id)
        return result

    @router.post(user_path + "/cancel")
    def cancel(request: Request, session_id: str) -> dict[str, Any]:
        return call(guard(request, session_id).cancel, session_id)

    @router.get(user_path + "/report", response_class=PlainTextResponse)
    def report(request: Request, session_id: str) -> PlainTextResponse:
        content = call(guard(request, session_id).report, session_id)
        return PlainTextResponse(
            content,
            headers={
                "Content-Disposition": f'attachment; filename="accuracy-ai-{session_id}.md"',
                "Cache-Control": "no-store",
            },
        )

    @router.get(tool_path + "/inspect")
    def inspect(
        request: Request, session_id: str, token: str = Header(default="", alias="X-AI-Capability")
    ) -> dict[str, Any]:
        return call(guard(request, session_id, token=token).inspect, session_id)

    @router.get(tool_path + "/status")
    def tool_status(
        request: Request, session_id: str, token: str = Header(default="", alias="X-AI-Capability")
    ) -> dict[str, Any]:
        return call(guard(request, session_id, token=token).get, session_id)

    @router.post(tool_path + "/plan")
    def plan(
        request: Request,
        session_id: str,
        payload: AIAccuracyProposal,
        token: str = Header(default="", alias="X-AI-Capability"),
    ) -> dict[str, Any]:
        return call(
            guard(request, session_id, token=token, mutate=True).propose, session_id, payload
        )

    @router.post(tool_path + "/start")
    def start(
        request: Request, session_id: str, token: str = Header(default="", alias="X-AI-Capability")
    ) -> dict[str, Any]:
        return call(guard(request, session_id, token=token, mutate=True).start, session_id)

    @router.post(tool_path + "/cancel")
    def tool_cancel(
        request: Request, session_id: str, token: str = Header(default="", alias="X-AI-Capability")
    ) -> dict[str, Any]:
        return call(guard(request, session_id, token=token).cancel, session_id)

    @router.post(tool_path + "/analysis")
    def analysis(
        request: Request,
        session_id: str,
        payload: AIAccuracyAnalysis,
        token: str = Header(default="", alias="X-AI-Capability"),
    ) -> dict[str, Any]:
        return call(guard(request, session_id, token=token).record_analysis, session_id, payload)

    @router.get(tool_path + "/logs")
    def logs(
        request: Request, session_id: str, token: str = Header(default="", alias="X-AI-Capability")
    ) -> dict[str, Any]:
        manager = guard(request, session_id, token=token)
        state = call(manager.get, session_id)
        run = state["full_run"] or state["probe_run"]
        return (
            call(manager.accuracy.logs, run["id"]).model_dump(mode="json") if run else {"logs": ""}
        )

    app.include_router(router)
