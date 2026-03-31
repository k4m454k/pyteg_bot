from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from pytegbot_api.dependencies import get_task_manager, require_api_token
from pytegbot_api.services.task_manager import ExecutionTaskManager
from pytegbot_shared.models import (
    ExecutionTaskAccepted,
    ExecutionTaskCreateRequest,
    ExecutionTaskResponse,
)

router = APIRouter(
    prefix="/v1/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_api_token)],
)


@router.post(
    "",
    response_model=ExecutionTaskAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_task(
    payload: ExecutionTaskCreateRequest,
    manager: ExecutionTaskManager = Depends(get_task_manager),
) -> ExecutionTaskAccepted:
    task = await manager.create_task(
        code=payload.code,
        source=payload.source,
        timeout_seconds=payload.timeout_seconds,
    )
    return ExecutionTaskAccepted(
        task_id=task.task_id,
        status=task.status,
        timeout_seconds=task.timeout_seconds,
    )


@router.get("/{task_id}", response_model=ExecutionTaskResponse)
async def get_task(
    task_id: str,
    manager: ExecutionTaskManager = Depends(get_task_manager),
) -> ExecutionTaskResponse:
    task = await manager.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task


@router.post("/{task_id}/cancel", response_model=ExecutionTaskResponse)
async def cancel_task(
    task_id: str,
    manager: ExecutionTaskManager = Depends(get_task_manager),
) -> ExecutionTaskResponse:
    task = await manager.cancel_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    return task
