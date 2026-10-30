from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ..ai_store import AISessionStore
from ..state import DeploymentStatus
from ..store import DeploymentNotFound, DeploymentStore


class ModelTargetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedModelTarget:
    target_type: Literal["deployment", "ai_session"]
    target_id: str
    label: str
    model_name: str
    service_url: str
    snapshot: dict[str, Any]


class ModelTargetResolver:
    """Resolve a persisted model target to a verified loopback service snapshot."""

    def __init__(
        self,
        deployments: DeploymentStore,
        ai_sessions: AISessionStore,
    ) -> None:
        self.deployments = deployments
        self.ai_sessions = ai_sessions

    def resolve(
        self,
        target_type: Literal["deployment", "ai_session"],
        target_id: str,
    ) -> ResolvedModelTarget:
        if target_type == "deployment":
            return self._deployment(target_id)
        if target_type == "ai_session":
            return self._ai_session(target_id)
        raise ModelTargetError(f"unsupported model target type: {target_type}")

    def _deployment(self, target_id: str) -> ResolvedModelTarget:
        deployment = self.deployments.get(target_id)
        if deployment.status != DeploymentStatus.RUNNING:
            raise ModelTargetError("deployment is not running")
        model_name = deployment.parameters.served_model_name or deployment.model_id
        return ResolvedModelTarget(
            target_type="deployment",
            target_id=deployment.id,
            label=deployment.display_name or deployment.model_id,
            model_name=model_name,
            service_url=f"http://127.0.0.1:{deployment.parameters.port}",
            snapshot={
                "target_type": "deployment",
                "target_id": deployment.id,
                "status": deployment.status.value,
                "model_id": deployment.model_id,
                "revision": deployment.revision,
                "served_model_name": deployment.parameters.served_model_name,
                "port": deployment.parameters.port,
            },
        )

    def _ai_session(self, target_id: str) -> ResolvedModelTarget:
        session = self.ai_sessions.get(target_id)
        result = self.ai_sessions.get_result(target_id)
        if result.status != "verified":
            raise ModelTargetError("AI deployment result is not verified")
        model_name = session.target_model_id or session.display_name
        if session.deployment_id:
            try:
                deployment = self.deployments.get(session.deployment_id)
                model_name = deployment.parameters.served_model_name or deployment.model_id
            except DeploymentNotFound:
                pass
        return ResolvedModelTarget(
            target_type="ai_session",
            target_id=session.id,
            label=session.display_name,
            model_name=model_name,
            service_url=f"http://127.0.0.1:{result.port}",
            snapshot={
                "target_type": "ai_session",
                "target_id": session.id,
                "session_status": session.status,
                "result_status": result.status,
                "target_model_id": session.target_model_id,
                "linked_deployment_id": session.deployment_id,
                "container_name": result.container_name,
                "image": result.image,
                "port": result.port,
            },
        )
