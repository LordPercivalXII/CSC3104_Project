import time
import uuid
from datetime import datetime, timezone

from qdrant_client import QdrantClient, models
from sentence_transformers import CrossEncoder, SentenceTransformer

from smart_ai_gateway.config import get_settings
from smart_ai_gateway.services import cache_aging, cache_checks

# All of these come from smart_ai_gateway/gateway.config
_cfg = get_settings().semantic_cache
# Which LLM writes new answers; only its answers are served
_answered_by = get_settings().llm.answered_by
QDRANT_URL = _cfg.qdrant_url
COLLECTION_NAME = _cfg.collection_name

# Connect to the Qdrant Server
# Make sure the server is running on dockers / kubernetes
client = QdrantClient(url=QDRANT_URL)

# Load the embedding model once
embedding_model = SentenceTransformer(_cfg.embedding_model)

# Load the verifier (cross-encoder) once, only if the grey zone is in use
verifier = CrossEncoder(_cfg.verifier_model) if _cfg.verification_enabled else None

def generate_embedding(prompt: str) -> list[float]:
    """
    Generate a numerical embedding for the given prompt using the pre-loaded model.
    """
    return embedding_model.encode(prompt).tolist()

def cache_skip_reason(prompt: str) -> str | None:
    """
    Why this prompt must bypass the cache entirely (not searched, not stored), or None.
    """
    if _cfg.skip_time_sensitive:
        word = cache_checks.time_sensitive(prompt)
        if word:
            return f"time-sensitive ({word})"

    # The embedding model ignores everything past its token limit, so two long prompts that
    # only differ near the end would look identical
    tokens = len(embedding_model.tokenizer(prompt, verbose=False)["input_ids"])
    if tokens > embedding_model.max_seq_length:
        return f"too long for the embedding model ({tokens} > {embedding_model.max_seq_length} tokens)"

    return None

def select_match(prompt: str, candidates, now: float):
    """
    Decide which candidate, if any, answers this prompt.
    candidates: (similarity, payload, point_id) for the nearest stored questions.
    Returns (hit, reason): hit is a result dictionary or None; reason explains a miss.

    1. score = similarity, decayed by age when decay is on
    2. below verify_threshold: not the same question
    3. a rule check objects (different numbers, opposite words, ...): not the same question
    4. at or above similarity_threshold: hit
    5. in between (grey zone): hit only if the verifier agrees
    """
    direct, grey = [], []
    best_score, reason = None, "no cached entries"

    for similarity, payload, point_id in candidates:
        cached_prompt = payload.get("prompt")

        # Check if the payload contains a valid response
        if not isinstance(payload.get("response"), str) or not isinstance(cached_prompt, str):
            continue

        score = cache_aging.effective_score(similarity, payload, _cfg, now)
        if score is None:
            continue
        if best_score is None or score > best_score:
            best_score = score
            reason = f"best score {score:.3f} below verify_threshold"
        if score < _cfg.verify_threshold:
            continue

        conflict = cache_checks.conflict(prompt, cached_prompt)
        if conflict:
            reason = f"rule check: {conflict}"
            continue

        entry = (score, similarity, payload, point_id)
        if score >= _cfg.similarity_threshold:
            direct.append(entry)
        else:
            grey.append(entry)

    if direct:
        return _result(max(direct, key=lambda e: e[0]), "direct", None, now), None

    if grey and verifier is not None:
        # The verifier reads both questions together; one batch for all grey-zone candidates
        probabilities = verifier.predict([(prompt, e[2]["prompt"]) for e in grey])
        verified = max(zip(probabilities, grey), key=lambda pair: pair[0])
        probability = float(verified[0])
        if probability >= _cfg.verifier_threshold:
            return _result(verified[1], "verified", probability, now), None
        reason = f"verifier rejected (p={probability:.2f})"

    return None, reason

def _result(entry, match_type: str, verifier_score: float | None, now: float) -> dict:
    score, similarity, payload, point_id = entry
    created = payload.get(cache_aging.CREATED_AT_TS)
    return {
        "point_id": point_id,
        "response": payload["response"],
        "matched_prompt": payload["prompt"],
        "similarity_score": similarity,
        "effective_score": score,
        "match_type": match_type,
        "verifier_score": verifier_score,
        "age_seconds": now - created if isinstance(created, (int, float)) else None,
        "hit_count": payload.get(cache_aging.HIT_COUNT, 0),
    }

def search_cache(prompt: str, embedding: list[float], now: float | None = None):
    """
    Search Qdrant collection for a cached resposne to the same question that has not aged out.
    Returns a dictionary on a hit, or None on a miss.
    """
    now = time.time() if now is None else now

    # Expired entries are filtered out by Qdrant itself (see cache_aging.fresh_filter).
    # No score_threshold here, so a miss can still log how close the best entry was.
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=cache_aging.fresh_filter(_cfg, now, _answered_by),
        limit=_cfg.search_candidates,
        with_payload=True
    )

    hit, reason = select_match(
        prompt,
        [(match.score, match.payload or {}, match.id) for match in results.points],
        now
    )

    # Near-miss logging: these lines show which thresholds are costing hits
    if hit is None:
        print(f"Cache miss: {reason}")
    else:
        verified = f", verifier p={hit['verifier_score']:.2f}" if hit["verifier_score"] is not None else ""
        print(f"Cache hit ({hit['match_type']}): score {hit['effective_score']:.3f}{verified}")
    return hit

def record_hit(point_id, hit_count: int, now: float | None = None):
    """
    Mark a cached answer as just served: refreshes its last-hit time (which keeps it alive
    when sliding_ttl is on) and counts the hit.
    The count is read-then-written, so two pods hitting the same entry at once can lose
    an increment. It is only a statistic, so that is accepted.
    """
    now = time.time() if now is None else now
    try:
        client.set_payload(
            collection_name=COLLECTION_NAME,
            payload={
                cache_aging.LAST_HIT_AT_TS: now,
                cache_aging.HIT_COUNT: hit_count + 1
            },
            points=[point_id],
            wait=False
        )
    except Exception as exc:
        # The answer was already returned; a missed refresh only shortens this entry's life
        print(f"Could not record cache hit for point {point_id}: {exc}")

def store_cache(prompt: str, response: str, embedding: list[float], now: float | None = None):
    """
    Store a question embedding and its generated answer.
    """
    now = time.time() if now is None else now

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
                    "created_at": datetime.fromtimestamp(
                        now, timezone.utc
                    ).isoformat(),
                    **cache_aging.entry_fields(_cfg, now, _answered_by)
                }
            )
        ],
        wait=True
    )

    return point_id
