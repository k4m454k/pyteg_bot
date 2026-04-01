from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from pytegbot_api.api.system import router as system_router
from pytegbot_api.api.tasks import router as tasks_router
from pytegbot_api.core.config import ApiSettings, get_settings
from pytegbot_api.services.artifact_store import ArtifactStore
from pytegbot_api.services.docker_executor import DockerCodeExecutor
from pytegbot_api.services.task_manager import ExecutionTaskManager
from pytegbot_api.services.task_store import InMemoryTaskStore


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    store = InMemoryTaskStore(ttl_seconds=resolved_settings.execution.task_ttl_seconds)
    artifact_store = ArtifactStore(
        base_dir=Path(resolved_settings.execution.artifact_storage_dir)
    )
    executor = DockerCodeExecutor(resolved_settings.execution)
    manager = ExecutionTaskManager(
        settings=resolved_settings.execution,
        store=store,
        artifact_store=artifact_store,
        executor=executor,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.task_manager = manager
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="PyTegBot API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(system_router)
    app.include_router(tasks_router)
    return app
