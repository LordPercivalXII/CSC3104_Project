"""Runtime switches for the gateway, read from environment variables.

Everything defaults to the simplest behaviour (no caches, no scheduler) so
`uvicorn main:app` works on a laptop with nothing else running.
docker-compose.yml turns the full pipeline on.

Cache-specific settings (REDIS_URL, CACHE_TTL_SECONDS, CACHE_SIMILARITY_THRESHOLD,
...) stay in caching/config.py; QDRANT_URL is read by the semantic cache module.
"""
import os
from dataclasses import dataclass, field

CACHE_MODES = ("none", "exact", "semantic", "both")
_TRUE = {"1", "true", "yes", "on"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class GatewaySettings:
    # none | exact (Redis) | semantic (Qdrant) | both (Redis first, then Qdrant)
    cache_mode: str = field(
        default_factory=lambda: os.getenv("CACHE_MODE", "none").strip().lower()
    )
    # Send cache misses through the per-user fair-share queue before the LLM.
    fair_share_enabled: bool = field(default_factory=lambda: _flag("FAIR_SHARE_ENABLED"))
    # Used when a /chat request does not say who is asking.
    default_user_id: str = field(default_factory=lambda: os.getenv("DEFAULT_USER_ID", "anonymous"))
    default_team_id: str = field(default_factory=lambda: os.getenv("DEFAULT_TEAM_ID", "default"))
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "mock-llm"))

    def __post_init__(self):
        if self.cache_mode not in CACHE_MODES:
            raise ValueError(
                f"CACHE_MODE must be one of {CACHE_MODES}, got {self.cache_mode!r}"
            )

    @property
    def use_exact(self) -> bool:
        return self.cache_mode in ("exact", "both")

    @property
    def use_semantic(self) -> bool:
        return self.cache_mode in ("semantic", "both")
