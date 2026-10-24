import sys
from pathlib import Path

from qdrant_client import QdrantClient, models

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.config import get_settings  # noqa: E402

# Settings come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
QDRANT_URL = _cfg.qdrant_url
COLLECTION_NAME = _cfg.collection_name

# Connect to the Qdrant server
client = QdrantClient(url=QDRANT_URL)

# Check that the collection does not already exist
if not client.collection_exists(COLLECTION_NAME):
    # Create a collection for embeddings of the configured size
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(size=_cfg.embedding_dim, distance=models.Distance.COSINE),
    )
    print(f"Collection {COLLECTION_NAME} created successfully.")

else:
    print(f"Collection {COLLECTION_NAME} already exists.")

# Verify the collection
collection = client.get_collection(COLLECTION_NAME)

print(f"Collection status: {collection.status}")
print(f"Stored points: {collection.points_count}")

client.close()

