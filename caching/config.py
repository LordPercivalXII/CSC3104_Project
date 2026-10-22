"""Cache settings, read from environment variables so a Kubernetes ConfigMap can set them."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CacheConfig:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    key_prefix: str = field(default_factory=lambda: os.getenv("CACHE_PREFIX", "cache:"))
    # Seconds before a cached answer expires. 0 disables expiry.
    ttl_seconds: int = field(default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "86400")))
    # Whether a hit pushes the expiry back (sliding TTL) or the entry dies at a fixed time.
    refresh_ttl_on_hit: bool = field(
        default_factory=lambda: os.getenv("CACHE_REFRESH_TTL_ON_HIT", "false").lower() == "true"
    )
    # Ignore case, extra whitespace and trailing punctuation when matching prompts.
    normalise_prompts: bool = field(
        default_factory=lambda: os.getenv("CACHE_NORMALISE_PROMPTS", "true").lower() == "true"
    )