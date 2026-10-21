"""Cache settings, read from environment variables so a Kubernetes ConfigMap can set them."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CacheConfig:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    index_name: str = field(default_factory=lambda: os.getenv("CACHE_INDEX", "idx:cache"))
    key_prefix: str = field(default_factory=lambda: os.getenv("CACHE_PREFIX", "cache:"))
    # Cosine similarity a neighbour must reach to count as a hit. This is the value you sweep.
    similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("CACHE_SIMILARITY_THRESHOLD", "0.90"))
    )
    # Seconds before a cached answer expires. 0 disables expiry.
    ttl_seconds: int = field(default_factory=lambda: int(os.getenv("CACHE_TTL_SECONDS", "86400")))
    # Both fields below are read by cache.py but were missing from this class, so every
    # lookup raised AttributeError. Added during packaging.
    # Lower-case / collapse whitespace / drop trailing punctuation before hashing the prompt.
    normalise_prompts: bool = field(
        default_factory=lambda: os.getenv("CACHE_NORMALISE_PROMPTS", "true").strip().lower() in ("1", "true", "yes", "on")
    )
    # Restart the TTL each time an entry is served. Off = fixed lifetime, so a popular
    # answer still expires and gets regenerated (better for freshness/correctness).
    refresh_ttl_on_hit: bool = field(
        default_factory=lambda: os.getenv("CACHE_REFRESH_TTL_ON_HIT", "false").strip().lower() in ("1", "true", "yes", "on")
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    )
    embedding_dim: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIM", "384")))
    # HNSW graph parameters. Defaults are fine for a project-sized cache.
    hnsw_m: int = 16
    hnsw_ef_construction: int = 200