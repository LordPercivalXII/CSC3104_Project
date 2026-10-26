"""
One-off migration: give cache entries stored before aging existed the numeric timestamps
the aging filters need, taken from their "created_at" text.

Without this, those entries are simply never served again once ttl_seconds or
max_age_seconds is set, and the sweeper deletes them. Run this only to keep them.

    python smart_ai_gateway/scripts/backfill_cache_timestamps.py
"""

import sys
from pathlib import Path

from qdrant_client import QdrantClient

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.config import get_settings  # noqa: E402
from smart_ai_gateway.services import cache_aging  # noqa: E402

# Settings come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
client = QdrantClient(url=_cfg.qdrant_url)

updated, skipped = cache_aging.backfill_timestamps(client, _cfg)
print(f"Backfilled {updated} entries. Skipped {skipped} without a readable created_at.")

client.close()
