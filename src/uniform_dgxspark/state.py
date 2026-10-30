from __future__ import annotations

from enum import StrEnum


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    DOWNLOADING_MODEL = "downloading_model"
    PULLING_IMAGE = "pulling_image"
    STARTING_CONTAINER = "starting_container"
    LOADING_MODEL = "loading_model"
    HEALTH_CHECKING = "health_checking"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    DeploymentStatus.STOPPED,
    DeploymentStatus.FAILED,
    DeploymentStatus.CANCELLED,
}

RECOVERABLE_STATUSES = {
    DeploymentStatus.PREFLIGHT,
    DeploymentStatus.DOWNLOADING_MODEL,
    DeploymentStatus.PULLING_IMAGE,
    DeploymentStatus.STARTING_CONTAINER,
    DeploymentStatus.LOADING_MODEL,
    DeploymentStatus.HEALTH_CHECKING,
    DeploymentStatus.STOPPING,
}

ALLOWED_TRANSITIONS: dict[DeploymentStatus, set[DeploymentStatus]] = {
    DeploymentStatus.QUEUED: {
        DeploymentStatus.PREFLIGHT,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.PREFLIGHT: {
        DeploymentStatus.DOWNLOADING_MODEL,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.DOWNLOADING_MODEL: {
        DeploymentStatus.PULLING_IMAGE,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.PULLING_IMAGE: {
        DeploymentStatus.STARTING_CONTAINER,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.STARTING_CONTAINER: {
        DeploymentStatus.LOADING_MODEL,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.LOADING_MODEL: {
        DeploymentStatus.HEALTH_CHECKING,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.HEALTH_CHECKING: {
        DeploymentStatus.RUNNING,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.RUNNING: {
        DeploymentStatus.STOPPING,
        DeploymentStatus.CANCELLED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.STOPPING: {
        DeploymentStatus.STOPPED,
        DeploymentStatus.FAILED,
    },
    DeploymentStatus.STOPPED: {DeploymentStatus.QUEUED},
    DeploymentStatus.FAILED: {DeploymentStatus.QUEUED},
    DeploymentStatus.CANCELLED: {DeploymentStatus.QUEUED},
}


class InvalidTransition(ValueError):
    pass


def ensure_transition_allowed(current: DeploymentStatus, target: DeploymentStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition from {current} to {target}")
