"""Reads gateway.config.

Every tunable setting lives in that file; this module only loads, checks and types it.
There are deliberately no defaults here: a missing key is an error, so the file is
always the complete list of what can be changed.

Order of precedence for each value:
    1. environment variable  GW_<SECTION>_<KEY>
       (for [semantic_cache] qdrant_url, QDRANT_URL is also accepted)
    2. the config file: $GATEWAY_CONFIG, else gateway.config next to this module
"""
from __future__ import annotations

import configparser
import dataclasses
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "gateway.config"
CONFIG_PATH_ENV = "GATEWAY_CONFIG"
ENV_PREFIX = "GW"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """The configuration is missing a value or has an invalid one."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


@dataclass(frozen=True)
class SemanticCacheSettings:
    qdrant_url: str
    collection_name: str
    similarity_threshold: float
    embedding_model: str
    embedding_dim: int

    def __post_init__(self):
        _require(bool(self.qdrant_url.strip()), "semantic_cache.qdrant_url must not be empty")
        _require(bool(self.collection_name.strip()), "semantic_cache.collection_name must not be empty")
        _require(0.0 <= self.similarity_threshold <= 1.0,
                 "semantic_cache.similarity_threshold must be between 0 and 1")
        _require(self.embedding_dim > 0, "semantic_cache.embedding_dim must be positive")


@dataclass(frozen=True)
class GatewaySettings:
    prompt_max_length: int
    user_id_max_length: int

    def __post_init__(self):
        _require(self.prompt_max_length > 0, "gateway.prompt_max_length must be positive")
        _require(self.user_id_max_length > 0, "gateway.user_id_max_length must be positive")


@dataclass(frozen=True)
class SchedulerSettings:
    mock_llm_response_template: str

    def __post_init__(self):
        _require("{prompt}" in self.mock_llm_response_template,
                 "scheduler.mock_llm_response_template must contain {prompt}")


@dataclass(frozen=True)
class Settings:
    semantic_cache: SemanticCacheSettings
    gateway: GatewaySettings
    scheduler: SchedulerSettings


_SECTIONS = {
    "semantic_cache": SemanticCacheSettings,
    "gateway": GatewaySettings,
    "scheduler": SchedulerSettings,
}

# Older environment variable names that are still honoured.
_LEGACY_ENV = {("semantic_cache", "qdrant_url"): "QDRANT_URL"}


def _convert(raw: str, type_name: str, where: str):
    raw = raw.strip()
    try:
        if type_name == "str":
            return raw
        if type_name == "int":
            return int(raw)
        if type_name == "float":
            return float(raw)
        if type_name == "bool":
            if raw.lower() in _TRUE:
                return True
            if raw.lower() in _FALSE:
                return False
            raise ValueError("expected true or false")
    except ValueError as exc:
        raise ConfigError(f"{where}: cannot read {raw!r} as {type_name} ({exc})") from None
    raise ConfigError(f"{where}: unsupported type {type_name}")  # a bug in this module


def load_settings(path: Optional[os.PathLike] = None, env: Optional[Mapping[str, str]] = None) -> Settings:
    """Load and validate settings.

    path: config file to read. Default: $GATEWAY_CONFIG, else gateway.config in this package.
    env:  environment to read overrides from. Default: os.environ.
    """
    env = os.environ if env is None else env
    config_path = Path(path or env.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)

    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    parser = configparser.ConfigParser(interpolation=None)  # no % handling: values are literal
    try:
        parser.read(config_path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"{config_path}: {exc}") from None

    built = {}
    for section, cls in _SECTIONS.items():
        values = {}
        for field in dataclasses.fields(cls):
            where = f"{section}.{field.name}"
            env_name = f"{ENV_PREFIX}_{section}_{field.name}".upper()
            legacy = _LEGACY_ENV.get((section, field.name))
            if env_name in env:
                raw, where = env[env_name], f"{where} (from ${env_name})"
            elif legacy and legacy in env:
                raw, where = env[legacy], f"{where} (from ${legacy})"
            elif parser.has_option(section, field.name):
                raw = parser.get(section, field.name)
            else:
                raise ConfigError(f"{config_path}: missing [{section}] {field.name}")
            type_name = field.type if isinstance(field.type, str) else field.type.__name__
            values[field.name] = _convert(raw, type_name, where)
        built[section] = cls(**values)
    return Settings(**built)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The settings for this process, loaded once."""
    return load_settings()
