
"""
Clear the semantic cache in the Qdrant collection.
This script connects to the Qdrant server, deletes all points in the specified collection
"""

from qdrant_client import QdrantClient, models

client = QdrantClient(url="http://localhost:6333")

COLLECTION_NAME = "semantic_cache"

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
