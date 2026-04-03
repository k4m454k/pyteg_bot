from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pytegbot_api.app import create_app
from pytegbot_api.core.config import ApiSettings
from pytegbot_api.dependencies import get_settings_dependency
from pytegbot_shared.models import ExecutionTaskResponse

RUNNER_IMAGE = "pytegbot-runner:local"
TEST_CONFIG_PATH = Path(__file__).parent / "config" / "tests.yaml"


def _resolve_docker_base_url() -> str:
    candidate = os.environ.get("PYTEGBOT_TEST_DOCKER_BASE_URL") or os.environ.get("DOCKER_HOST")
    if candidate:
        return candidate

    try:
        result = subprocess.run(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{(index .Endpoints \"docker\").Host}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unix:///var/run/docker.sock"

    discovered = result.stdout.strip()
    return discovered or "unix:///var/run/docker.sock"


def _ensure_runner_image() -> None:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", RUNNER_IMAGE],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        pytest.fail(f"Docker is not available: {exc}")

    if result.returncode != 0:
        pytest.fail(
            f"Runner image {RUNNER_IMAGE!r} was not found. Run `make build_test` first."
        )


@pytest.fixture(scope="session")
def docker_base_url() -> str:
    _ensure_runner_image()
    return _resolve_docker_base_url()


@pytest.fixture()
def api_settings(tmp_path: Path, docker_base_url: str, monkeypatch: pytest.MonkeyPatch) -> ApiSettings:
    artifact_storage_dir = tmp_path / "artifacts"
    artifact_storage_dir.mkdir(parents=True, exist_ok=True)

    override_config_path = tmp_path / "api.tests.override.yaml"
    override_config_path.write_text(
        yaml.safe_dump(
            {
                "execution": {
                    "docker_base_url": docker_base_url,
                    "execution_image": RUNNER_IMAGE,
                    "artifact_storage_dir": str(artifact_storage_dir),
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("PYTEGBOT_API_CONFIG", str(TEST_CONFIG_PATH))
    monkeypatch.setenv("PYTEGBOT_API_LOCAL_CONFIG", str(override_config_path))
    return ApiSettings()


@pytest.fixture()
def client(api_settings: ApiSettings) -> TestClient:
    app = create_app(api_settings)
    app.dependency_overrides[get_settings_dependency] = lambda: api_settings
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(api_settings: ApiSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_settings.server.auth_token}"}


@pytest.fixture()
def task_timeout_seconds(api_settings: ApiSettings) -> int:
    return api_settings.execution.max_timeout_seconds


def create_task(
    client: TestClient,
    auth_headers: dict[str, str],
    *,
    code: str,
    timeout_seconds: int,
    source: str = "api",
) -> str:
    response = client.post(
        "/v1/tasks",
        headers=auth_headers,
        json={
            "code": code,
            "source": source,
            "timeout_seconds": timeout_seconds,
        },
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    return payload["task_id"]


def build_large_python_code(
    *,
    target_bytes: int,
    final_line: str,
) -> str:
    filler_line = "# filler payload to cross the env threshold\n"
    parts: list[str] = []
    total = 0
    while total < target_bytes:
        parts.append(filler_line)
        total += len(filler_line.encode("utf-8"))
    parts.append(final_line)
    return "".join(parts)


def wait_for_terminal_task(
    client: TestClient,
    auth_headers: dict[str, str],
    task_id: str,
    *,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 0.2,
) -> ExecutionTaskResponse:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, object] | None = None

    while time.monotonic() < deadline:
        response = client.get(f"/v1/tasks/{task_id}", headers=auth_headers)
        assert response.status_code == 200, response.text
        last_payload = response.json()
        task = ExecutionTaskResponse.model_validate(last_payload)
        if task.is_finished:
            return task
        time.sleep(poll_interval_seconds)

    pytest.fail(f"Task {task_id} did not finish in time. Last payload: {last_payload!r}")
