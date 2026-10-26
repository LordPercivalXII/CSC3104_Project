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
    ttl_seconds: int
    sliding_ttl: bool
    max_age_seconds: int
    sweep_interval_seconds: int
    decay_half_life_seconds: int
    search_candidates: int
    verify_threshold: float
    verifier_model: str
    verifier_threshold: float
    skip_time_sensitive: bool

    def __post_init__(self):
        _require(bool(self.qdrant_url.strip()), "semantic_cache.qdrant_url must not be empty")
        _require(bool(self.collection_name.strip()), "semantic_cache.collection_name must not be empty")
        _require(0.0 <= self.similarity_threshold <= 1.0,
                 "semantic_cache.similarity_threshold must be between 0 and 1")
        _require(0.0 <= self.verify_threshold <= self.similarity_threshold,
                 "semantic_cache.verify_threshold must be between 0 and similarity_threshold")
        _require(0.0 <= self.verifier_threshold <= 1.0,
                 "semantic_cache.verifier_threshold must be between 0 and 1")
        _require(not self.verification_enabled or bool(self.verifier_model.strip()),
                 "semantic_cache.verifier_model must not be empty while verification is on")
        _require(self.embedding_dim > 0, "semantic_cache.embedding_dim must be positive")
        _require(self.ttl_seconds >= 0, "semantic_cache.ttl_seconds must be 0 or more")
        _require(self.max_age_seconds >= 0, "semantic_cache.max_age_seconds must be 0 or more")
        _require(self.sweep_interval_seconds > 0, "semantic_cache.sweep_interval_seconds must be positive")
        _require(self.decay_half_life_seconds >= 0, "semantic_cache.decay_half_life_seconds must be 0 or more")
        _require(self.search_candidates > 0, "semantic_cache.search_candidates must be positive")

    @property
    def verification_enabled(self) -> bool:
        # The grey zone between the two thresholds is checked by the verifier model
        return self.verify_threshold < self.similarity_threshold


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


LLM_PROVIDERS = ("gemini", "mock")
THINKING_LEVELS = ("", "minimal", "low", "medium", "high")


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    system_instruction: str
    thinking_level: str
    max_output_tokens: int
    timeout_seconds: int
    max_retries: int

    def __post_init__(self):
        _require(self.provider in LLM_PROVIDERS, f"llm.provider must be one of {', '.join(LLM_PROVIDERS)}")
        _require(self.provider == "mock" or bool(self.model.strip()), "llm.model must not be empty")
        _require(self.thinking_level in THINKING_LEVELS,
                 "llm.thinking_level must be empty or one of minimal, low, medium, high")
        _require(self.max_output_tokens > 0, "llm.max_output_tokens must be positive")
        _require(self.timeout_seconds > 0, "llm.timeout_seconds must be positive")
        _require(self.max_retries >= 0, "llm.max_retries must be 0 or more")

    @property
    def answered_by(self) -> str:
        # Stored with every cached answer, so answers from another LLM are never served
        return "mock" if self.provider == "mock" else f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Settings:
    semantic_cache: SemanticCacheSettings
    gateway: GatewaySettings
    scheduler: SchedulerSettings
    llm: LLMSettings


_SECTIONS = {
    "semantic_cache": SemanticCacheSettings,
    "gateway": GatewaySettings,
    "scheduler": SchedulerSettings,
    "llm": LLMSettings,
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
