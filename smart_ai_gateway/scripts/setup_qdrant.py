import os
from qdrant_client import QdrantClient, models

# Importing qdrant and setting up the Qdrant client
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = "semantic_cache"

# Connect to the Qdrant server
client = QdrantClient(url=QDRANT_URL)

# Check that the collection does not already exist
if not client.collection_exists(COLLECTION_NAME):
    # Create a collection for 384-dimensional embeddings
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE),
    )
    print(f"Collection {COLLECTION_NAME} created successfully.")

else:
    print(f"Collection {COLLECTION_NAME} already exists.")

# Verify the collection
collection = client.get_collection(COLLECTION_NAME)

print(f"Collection status: {collection.status}")
print(f"Stored points: {collection.points_count}")

client.close()

