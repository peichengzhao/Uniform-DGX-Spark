from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

from .config import Settings
from .services.accuracy import AccuracyBusy, AccuracyCompatibilityError, AccuracyTargetError
from .services.dataset_integration import (
    DatasetIntegrationAnalysis,
    DatasetIntegrationConfirm,
    DatasetIntegrationManager,
    DatasetIntegrationProposal,
)
from .services.evalscope_accuracy import EvalScopeAccuracyError
from .services.model_targets import ModelTargetError


def register_dataset_integration_routes(app: FastAPI, settings: Settings) -> None:
    router = APIRouter()
    user_path = "/api/v1/ai/sessions/{session_id}/datasets"
    tool_path = "/api/v1/ai/sessions/{session_id}/actions/datasets"

    def guard(
        request: Request, session_id: str, *, token: str | None = None, mutate: bool = False
    ) -> DatasetIntegrationManager:
        if not settings.accuracy_enabled or not settings.ai_sessions_enabled:
            raise HTTPException(status_code=403, detail="dataset integration is disabled")
        if mutate and not settings.accuracy_creation_enabled:
            raise HTTPException(status_code=403, detail="dataset integration is read-only")
        terminal = request.app.state.ai_terminal
        if token is not None and not terminal.validate_capability(session_id, token):
            raise HTTPException(status_code=403, detail="invalid dataset integration capability")
        try:
            session = terminal.store.get(session_id)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="integration session not found") from error
        if session.task_type != "dataset_integration":
            raise HTTPException(
                status_code=403, detail="this capability is not for dataset integration"
            )
        if mutate and session.archived_at:
            raise HTTPException(
                status_code=409, detail="restore the session before changing its plan"
            )
        return request.app.state.dataset_integrations

    def call(action: Callable[..., Any], *args: Any) -> Any:
        try:
            return action(*args)
        except LookupError as error:
            raise HTTPException(status_code=404, detail="integration resource not found") from error
        except (
            AccuracyCompatibilityError,
            AccuracyBusy,
            AccuracyTargetError,
            ModelTargetError,
        ) as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except EvalScopeAccuracyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(
                status_code=503, detail="integration environment unavailable"
            ) from error

    @router.get("/api/v1/accuracy/datasets/catalog")
    def catalog(request: Request) -> dict[str, Any]:
        if not settings.accuracy_enabled:
            raise HTTPException(status_code=403, detail="accuracy is disabled")
        return call(request.app.state.dataset_integrations.catalog)

    @router.get("/api/v1/accuracy/datasets/integrations")
    def integrations(request: Request) -> dict[str, Any]:
        if not settings.accuracy_enabled or not settings.ai_sessions_enabled:
            raise HTTPException(status_code=403, detail="dataset integration is disabled")
        sessions, total = request.app.state.ai_store.list(
            task_type="dataset_integration", limit=200
        )
        return {
            "items": [
                {
                    "session": session,
                    "workflow": call(request.app.state.dataset_integrations.get, session.id),
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
        request: Request, session_id: str, payload: DatasetIntegrationConfirm
    ) -> dict[str, Any]:
        if request.headers.get("X-AI-Capability"):
            raise HTTPException(status_code=403, detail="only the user can confirm integration")
        return call(
            guard(request, session_id, mutate=True).confirm, session_id, payload.proposal_id
        )

    @router.post(user_path + "/cancel")
    def cancel(request: Request, session_id: str) -> dict[str, Any]:
        return call(guard(request, session_id).cancel, session_id)

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
        payload: DatasetIntegrationProposal,
        token: str = Header(default="", alias="X-AI-Capability"),
    ) -> dict[str, Any]:
        return call(
            guard(request, session_id, token=token, mutate=True).propose, session_id, payload
        )

    @router.post(tool_path + "/analysis")
    def analysis(
        request: Request,
        session_id: str,
        payload: DatasetIntegrationAnalysis,
        token: str = Header(default="", alias="X-AI-Capability"),
    ) -> dict[str, Any]:
        return call(
            guard(request, session_id, token=token, mutate=True).record_analysis,
            session_id,
            payload.text,
        )

    app.include_router(router)
