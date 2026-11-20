"""
Clear the semantic cache in the Qdrant collection.
This script connects to the Qdrant server and deletes all points in the configured
collection (the collection itself is kept).

Deleting is permanent, so it only runs when asked to:
    python smart_ai_gateway/services/clear_semantic_cache.py          # asks you to type "yes"
    python smart_ai_gateway/services/clear_semantic_cache.py --yes    # no question (scripts)
Importing this module deletes nothing.
"""

import argparse
import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.config import get_settings  # noqa: E402


def clear_cache(client: QdrantClient, collection_name: str) -> int:
    """
    Delete every point in the collection. Returns how many points remain (0 when it worked).
    """
    # Delete all points while keeping the collection
    client.delete(
        collection_name=collection_name,
        points_selector=models.FilterSelector(
            filter=models.Filter(must=[])
        ),
        wait=True
    )

    # Verify the remaining number of points
    return client.count(
        collection_name=collection_name,
        exact=True
    ).count


def main(argv=None, ask=input) -> bool:
    """
    Returns True when the cache was cleared. ask is the confirmation prompt (input() by default).
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="clear without asking for confirmation")
    args = parser.parse_args(argv)

    # Settings come from smart_ai_gateway/gateway.config
    cfg = get_settings().semantic_cache

    if not args.yes:
        try:
            answer = ask(f"Delete ALL cached answers in '{cfg.collection_name}' at {cfg.qdrant_url}? Type yes: ")
        except EOFError:
            # No terminal to ask on (e.g. run from another script): never delete by default
            answer = ""
        if answer.strip().lower() != "yes":
            print("Not cleared. Pass --yes to clear without being asked.")
            return False

    client = QdrantClient(url=cfg.qdrant_url, timeout=cfg.qdrant_timeout_seconds)
    try:
        remaining = clear_cache(client, cfg.collection_name)
    finally:
        client.close()
    print(f"Cache cleared. Remaining points: {remaining}")
    return True


if __name__ == "__main__":
    main()
