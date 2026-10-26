"""
Delete semantic-cache entries that have aged out (see [semantic_cache] in gateway.config).

Expired entries are already never served; this only frees their storage in Qdrant.

    python smart_ai_gateway/scripts/sweep_expired_cache.py          # once (Kubernetes CronJob)
    python smart_ai_gateway/scripts/sweep_expired_cache.py --loop   # every sweep_interval_seconds (docker compose)
"""

import argparse
import sys
import time
from pathlib import Path

from qdrant_client import QdrantClient

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.config import get_settings  # noqa: E402
from smart_ai_gateway.services import cache_aging  # noqa: E402

# Settings come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
_answered_by = get_settings().llm.answered_by


def sweep_once(client: QdrantClient):
    deleted = cache_aging.sweep_expired(client, _cfg, time.time(), _answered_by)
    print(f"Swept {deleted} expired cache entries (aged out, or from another embedding model or LLM).")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--loop", action="store_true", help="keep sweeping every sweep_interval_seconds")
args = parser.parse_args()

client = QdrantClient(url=_cfg.qdrant_url)

if not args.loop:
    sweep_once(client)
else:
    print(f"Sweeping every {_cfg.sweep_interval_seconds} s.")
    while True:
        try:
            sweep_once(client)
        except Exception as exc:
            # Qdrant restarting or briefly unreachable: try again next round
            print(f"Sweep failed: {exc}")
        time.sleep(_cfg.sweep_interval_seconds)

client.close()
