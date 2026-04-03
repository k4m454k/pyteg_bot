from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from pytegbot_shared.config import YamlConfigSettingsSource


class TelegramSettings(BaseModel):
    bot_token: str = Field(min_length=10)


class ApiClientSettings(BaseModel):
    base_url: str = "http://api:8000"
    auth_token: str = Field(min_length=8)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    poll_interval_seconds: float = Field(default=5.0, gt=0)


class InlineSettings(BaseModel):
    debounce_seconds: float = Field(default=1.0, ge=0)
    cache_time_seconds: int = Field(default=0, ge=0)
    execution_timeout_seconds: int = Field(default=15, ge=1, le=300)


class MessageSettings(BaseModel):
    max_py_file_bytes: int = Field(default=5 * 1024 * 1024, ge=1_024, le=20 * 1024 * 1024)
    media_group_track_seconds: float = Field(default=300.0, gt=0)


class LoggingSettings(BaseModel):
    level: str = "INFO"


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    api: ApiClientSettings = Field(default_factory=ApiClientSettings)
    inline: InlineSettings = Field(default_factory=InlineSettings)
    message: MessageSettings = Field(default_factory=MessageSettings)
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
                    ("PYTEGBOT_BOT_CONFIG", "config/bot.yaml"),
                    ("PYTEGBOT_BOT_LOCAL_CONFIG", "config/bot.local.yaml"),
                ),
            ),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> BotSettings:
    return BotSettings()
