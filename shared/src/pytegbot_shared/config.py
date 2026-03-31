from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        config_files: tuple[tuple[str, str], ...],
    ) -> None:
        super().__init__(settings_cls)
        self._config_files = tuple(
            (env_var, Path(default_path))
            for env_var, default_path in config_files
        )

    @staticmethod
    def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = YamlConfigSettingsSource._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _resolve_paths(self) -> list[Path]:
        return [
            Path(os.getenv(env_var, default_path))
            for env_var, default_path in self._config_files
        ]

    def _read_data(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self._resolve_paths():
            if not path.exists():
                continue

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"YAML config must contain a mapping at top level: {path}")
            merged = self._deep_merge(merged, loaded)
        return merged

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        data = self._read_data()
        return data.get(field_name), field_name, False

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        return value

    def __call__(self) -> dict[str, Any]:
        return self._read_data()
