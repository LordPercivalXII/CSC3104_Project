"""
Delete semantic-cache entries that have aged out (see [semantic_cache] in gateway.config).

Expired entries are already never served; this only frees their storage in Qdrant.
Entries from another embedding model or LLM are kept for other_model_grace_seconds after
their last hit, in case the gateway is switched back; --purge-other-models deletes them now.

    python smart_ai_gateway/scripts/sweep_expired_cache.py          # once (Kubernetes CronJob)
    python smart_ai_gateway/scripts/sweep_expired_cache.py --loop   # every sweep_interval_seconds (docker compose)
    python smart_ai_gateway/scripts/sweep_expired_cache.py --purge-other-models
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


def sweep_once(client: QdrantClient, purge_other_models: bool = False) -> int:
    deleted = cache_aging.sweep_expired(client, _cfg, time.time(), _answered_by, purge_other_models)
    print(f"Swept {deleted} expired cache entries (aged out, or from another embedding model or LLM).")
    return deleted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--loop", action="store_true", help="keep sweeping every sweep_interval_seconds")
    parser.add_argument("--purge-other-models", action="store_true",
                        help="also delete entries from another embedding model or LLM that are "
                             "still within other_model_grace_seconds")
    args = parser.parse_args(argv)

    client = QdrantClient(url=_cfg.qdrant_url, timeout=_cfg.qdrant_timeout_seconds)
    try:
        if not args.loop:
            sweep_once(client, args.purge_other_models)
            return
        print(f"Sweeping every {_cfg.sweep_interval_seconds} s.")
        while True:
            try:
                sweep_once(client, args.purge_other_models)
            except Exception as exc:
                # Qdrant restarting or briefly unreachable: try again next round
                print(f"Sweep failed: {exc}")
            time.sleep(_cfg.sweep_interval_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
