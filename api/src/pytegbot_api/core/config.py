from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pytegbot_shared.config import YamlConfigSettingsSource


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    auth_token: str = Field(min_length=8)


class ExecutionSettings(BaseModel):
    docker_base_url: str = "unix://var/run/docker.sock"
    execution_image: str = "pytegbot-runner:local"
    max_concurrent_tasks: int = Field(default=2, ge=1, le=16)
    max_timeout_seconds: int = Field(
        default=20,
        ge=1,
        le=300,
        validation_alias=AliasChoices("max_timeout_seconds", "timeout_seconds"),
    )
    task_ttl_seconds: int = Field(default=1800, ge=60)
    cleanup_interval_seconds: int = Field(default=60, ge=5)
    memory_limit: str = "200m"
    nano_cpus: int = Field(default=200_000_000, ge=1)
    max_output_bytes: int = Field(default=262_144, ge=1_024, le=16_777_216)
    code_env_var: str = "PYTEGBOT_CODE_B64"


class LoggingSettings(BaseModel):
    level: str = "INFO"


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    server: ServerSettings = Field(default_factory=ServerSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(
                settings_cls,
                config_files=(
                    ("PYTEGBOT_API_CONFIG", "config/api.yaml"),
                    ("PYTEGBOT_API_LOCAL_CONFIG", "config/api.local.yaml"),
                ),
            ),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    return ApiSettings()
