import uuid
from datetime import datetime, timezone

from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer

from smart_ai_gateway.config import get_settings

# All of these come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
QDRANT_URL = _cfg.qdrant_url
COLLECTION_NAME = _cfg.collection_name
SIMILARITY_THRESHOLD = _cfg.similarity_threshold

# Connect to the Qdrant Server
# Make sure the server is running on dockers / kubernetes
client = QdrantClient(url=QDRANT_URL)

# Load the embedding model once
embedding_model = SentenceTransformer(_cfg.embedding_model)

def generate_embedding(prompt: str) -> list[float]:
    """
    Generate a numerical embedding for the given prompt using the pre-loaded model.
    """
    return embedding_model.encode(prompt).tolist()

def search_cache(embedding: list[float]):
    """
    Search Qdrant collection for a similar cached resposne.
    Returns a dictionary on a hit, or None on a miss.
    """
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        limit=1,
        score_threshold=SIMILARITY_THRESHOLD,
        with_payload=True
    )
    # No matches found
    if not results.points:
        return None

    # Return the first match
    match = results.points[0]
    payload = match.payload or {}

    # Check if the payload contains a valid response
    if not isinstance(payload.get("response"), str):
        return None

    return {
        "response": payload["response"],
        "matched_prompt": payload.get("prompt"),
        "similarity_score": match.score
    }

def store_cache(prompt: str, response: str, embedding: list[float]):
    """
    Store a question embedding and its generated answer.
    """
    # To generate a unique ID for the point, we can use UUIDs. This ensures that each point has a unique identifier.
    point_id = str(uuid.uuid4())
    print(f"Storing cache for point: {point_id}")

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "prompt": prompt,
                    "response": response,
                    "created_at": datetime.now(
                        timezone.utc
                    ).isoformat()
                }
            )
        ],
        wait=True
    )

    return point_id