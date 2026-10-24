
"""
Clear the semantic cache in the Qdrant collection.
This script connects to the Qdrant server, deletes all points in the specified collection
"""

import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.config import get_settings  # noqa: E402

# Settings come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
client = QdrantClient(url=_cfg.qdrant_url)

COLLECTION_NAME = _cfg.collection_name

# Delete all points while keeping the collection
client.delete(
    collection_name=COLLECTION_NAME,
    points_selector=models.FilterSelector(
        filter=models.Filter(must=[])
    ),
    wait=True
)

# Verify the remaining number of points
count = client.count(
    collection_name=COLLECTION_NAME,
    exact=True
).count

print(f"Cache cleared. Remaining points: {count}")

client.close()
