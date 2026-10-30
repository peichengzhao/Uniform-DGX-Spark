from __future__ import annotations

import sys

import pytest

from uniform_dgxspark.services.docker_runtime import (
    DockerCommandCancelled,
    SubprocessCommandRunner,
)


def test_streaming_command_reports_output() -> None:
    lines: list[str] = []
    result = SubprocessCommandRunner().stream(
        [sys.executable, "-c", "print('Pulling layer'); print('Download complete')"],
        timeout=5,
        output_callback=lines.append,
        cancel_check=lambda: False,
    )

    assert result.returncode == 0
    assert lines == ["Pulling layer", "Download complete"]


def test_streaming_command_can_be_cancelled() -> None:
    checks = 0

    def cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    with pytest.raises(DockerCommandCancelled):
        SubprocessCommandRunner().stream(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout=20,
            output_callback=lambda line: None,
            cancel_check=cancel,
        )
