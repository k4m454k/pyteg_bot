from __future__ import annotations

import time

from fastapi.testclient import TestClient

from pytegbot_shared.models import TaskStatus

from .conftest import build_large_python_code, create_task, wait_for_terminal_task


def test_healthcheck_is_available(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_task_requires_bearer_token(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        json={"code": 'print("unauthorized")', "source": "api"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token."


def test_executes_small_python_code(
    client: TestClient,
    auth_headers: dict[str, str],
    task_timeout_seconds: int,
) -> None:
    task_id = create_task(
        client,
        auth_headers,
        code='print("small-ok")',
        timeout_seconds=task_timeout_seconds,
    )

    task = wait_for_terminal_task(client, auth_headers, task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.exit_code == 0
    assert task.output == "small-ok"
    assert task.started_at is not None
    assert task.finished_at is not None


def test_executes_large_python_code_via_file_transport(
    client: TestClient,
    auth_headers: dict[str, str],
    task_timeout_seconds: int,
) -> None:
    code = build_large_python_code(
        target_bytes=1024 * 1024,
        final_line='print("large-ok")\n',
    )

    task_id = create_task(
        client,
        auth_headers,
        code=code,
        timeout_seconds=task_timeout_seconds,
    )

    task = wait_for_terminal_task(client, auth_headers, task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.exit_code == 0
    assert task.output == "large-ok"


def test_can_cancel_running_task(
    client: TestClient,
    auth_headers: dict[str, str],
    task_timeout_seconds: int,
) -> None:
    task_id = create_task(
        client,
        auth_headers,
        code="import time\ntime.sleep(30)\nprint('done')\n",
        timeout_seconds=task_timeout_seconds,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/v1/tasks/{task_id}", headers=auth_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"queued", "running"}:
            break
        time.sleep(0.1)

    cancel_response = client.post(f"/v1/tasks/{task_id}/cancel", headers=auth_headers)
    assert cancel_response.status_code == 200, cancel_response.text

    task = wait_for_terminal_task(client, auth_headers, task_id)
    assert task.status == TaskStatus.CANCELLED
    assert task.cancel_requested is True


def test_returns_png_artifact_and_supports_download(
    client: TestClient,
    auth_headers: dict[str, str],
    task_timeout_seconds: int,
) -> None:
    code = """
from PIL import Image

img = Image.new("RGB", (32, 32), "red")
img.save("artifact.png")
print("artifact-saved")
""".strip()

    task_id = create_task(
        client,
        auth_headers,
        code=code,
        timeout_seconds=task_timeout_seconds,
    )

    task = wait_for_terminal_task(client, auth_headers, task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.output == "artifact-saved"
    assert len(task.artifacts) == 1

    artifact = task.artifacts[0]
    assert artifact.filename == "artifact.png"
    assert artifact.media_type == "image/png"
    assert artifact.size_bytes > 0

    response = client.get(
        f"/v1/tasks/{task.task_id}/artifacts/{artifact.artifact_id}",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
