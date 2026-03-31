from __future__ import annotations

from fastapi import APIRouter, Depends

from pytegbot_api.dependencies import get_task_manager
from pytegbot_api.services.task_manager import ExecutionTaskManager
from pytegbot_shared.models import HealthResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def healthcheck(
    manager: ExecutionTaskManager = Depends(get_task_manager),
) -> HealthResponse:
    return await manager.health()

