from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pytegbot_api.core.config import ApiSettings, get_settings
from pytegbot_api.services.task_manager import ExecutionTaskManager

bearer_scheme = HTTPBearer(auto_error=False)


def get_task_manager(request: Request) -> ExecutionTaskManager:
    return request.app.state.task_manager


def get_settings_dependency() -> ApiSettings:
    return get_settings()


async def require_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: ApiSettings = Depends(get_settings_dependency),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )

    if credentials.credentials != settings.server.auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
        )

