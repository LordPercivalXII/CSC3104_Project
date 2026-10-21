
import os

from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient, models

SIMILARITY_THRESHOLD = 0.9  # Adjust this threshold based on your needs

# Qdrant configuration
QDRANT_URL = os.getenv(
    "QDRANT_URL",
    "http://localhost:6333"
)
COLLECTION_NAME = "semantic_cache"

# Connect to Qdrant
client = QdrantClient(url=QDRANT_URL)

if not client.collection_exists(COLLECTION_NAME):
    raise RuntimeError(
        "Collection not found. Run scripts/setup_qdrant.py first."
    )

# Load the embedding model
model = SentenceTransformer(
    "sentence-transformers/all-MiniLM-L6-v2"
)


# ---------------------------------
# Store a question and answer
# ---------------------------------

original_prompt = "What is Docker?"

cached_answer = (
    "Docker is a containerization platform that allows "
    "applications and their dependencies to be packaged "
    "and run in containers."
)

# Convert the question to a numerical vector
original_embedding = model.encode(original_prompt).tolist()

print("Embedding dimensions:", len(original_embedding))

# Store the vector and payload in Qdrant
client.upsert(
    collection_name=COLLECTION_NAME,
    points=[
        models.PointStruct(
            id=1, # Unique identifier of the Qdrant point
            vector=original_embedding, # the numerical embedding
            payload={
                "prompt": original_prompt,
                "response": cached_answer
            }
        )
    ],
    wait=True
)

print("Stored original question in Qdrant.")


# ---------------------------------
# Search with new questions
# ---------------------------------

test_prompts = [
    "Can you explain Docker to me?",
    "How do I cook pasta?"
]

for prompt in test_prompts:
    print("\nSearching for:", prompt)

    # Generate embedding for the new prompt
    query_embedding = model.encode(prompt).tolist()

    # Search the Qdrant collection
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_embedding,
        limit=1,
        with_payload=True
    )

    if (results.points and results.points[0].score >= SIMILARITY_THRESHOLD):
        match = results.points[0]
        print("CACHE HIT!")
        print("Matched prompt:", match.payload["prompt"])
        print("Similarity score:", round(match.score, 4))
        print("Cached answer:", match.payload["response"])
    else:
        print("CACHE MISS!")
        print("No sufficiently similar cached answer found.")
        print("An LLM API call would be required.")


client.close()
