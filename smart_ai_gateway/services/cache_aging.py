"""
Aging rules for the semantic cache.

Every cached point carries numeric timestamps (epoch seconds) next to its prompt and response:
    created_at_ts    when the answer was stored
    last_hit_at_ts   when it was last served from the cache (equals created_at_ts until then)
    hit_count        how many times it has been served
    embedding_model  the model that made the vector; vectors from different models cannot be
                     compared, so entries from any other model count as expired
    answered_by      the LLM that wrote the answer ([llm] answered_by in config.py); answers
                     from another LLM (or the mock) count as expired too
    paraphrase_of    only on extra wordings of a cached question: the id of the entry whose
                     answer this point repeats (see semantic_cache.store_paraphrase)

The settings that drive these rules are in the [semantic_cache] section of gateway.config.
This module has no embedding model, so the sweeper can import it without loading PyTorch.
"""
from datetime import datetime

from qdrant_client import QdrantClient, models

from smart_ai_gateway.config import SemanticCacheSettings

CREATED_AT_TS = "created_at_ts"
LAST_HIT_AT_TS = "last_hit_at_ts"
HIT_COUNT = "hit_count"
EMBEDDING_MODEL = "embedding_model"
ANSWERED_BY = "answered_by"
PARAPHRASE_OF = "paraphrase_of"

# The only embedding model and LLM used before entries were tagged with theirs
LEGACY_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LEGACY_ANSWERED_BY = "mock"

# Payload fields that are filtered on, and so get a Qdrant payload index
INDEXED_FIELDS = {
    CREATED_AT_TS: models.PayloadSchemaType.FLOAT,
    LAST_HIT_AT_TS: models.PayloadSchemaType.FLOAT,
    EMBEDDING_MODEL: models.PayloadSchemaType.KEYWORD,
    ANSWERED_BY: models.PayloadSchemaType.KEYWORD,
    PARAPHRASE_OF: models.PayloadSchemaType.KEYWORD,
}


def entry_fields(cfg: SemanticCacheSettings, now: float, answered_by: str) -> dict:
    """
    The aging fields a newly stored point starts with.
    """
    return {
        CREATED_AT_TS: now,
        LAST_HIT_AT_TS: now,
        HIT_COUNT: 0,
        EMBEDDING_MODEL: cfg.embedding_model,
        ANSWERED_BY: answered_by,
    }


def _same_models(cfg: SemanticCacheSettings, answered_by: str) -> list[models.FieldCondition]:
    return [
        models.FieldCondition(key=EMBEDDING_MODEL, match=models.MatchValue(value=cfg.embedding_model)),
        models.FieldCondition(key=ANSWERED_BY, match=models.MatchValue(value=answered_by)),
    ]


def _ttl_field(cfg: SemanticCacheSettings) -> str:
    # Sliding TTL measures age from the last hit, a fixed TTL from creation
    return LAST_HIT_AT_TS if cfg.sliding_ttl else CREATED_AT_TS


def fresh_filter(cfg: SemanticCacheSettings, now: float, answered_by: str) -> models.Filter:
    """
    Filter that keeps only points still young enough to be served, embedded by the current
    embedding model and answered by the current LLM.
    Points without the timestamp fields do not match, so entries stored before aging
    existed are never served once ttl_seconds or max_age_seconds is set.
    """
    conditions = _same_models(cfg, answered_by)
    if cfg.ttl_seconds:
        conditions.append(models.FieldCondition(
            key=_ttl_field(cfg), range=models.Range(gte=now - cfg.ttl_seconds)))
    if cfg.max_age_seconds:
        conditions.append(models.FieldCondition(
            key=CREATED_AT_TS, range=models.Range(gte=now - cfg.max_age_seconds)))
    return models.Filter(must=conditions)


def _other_models(cfg: SemanticCacheSettings, now: float, answered_by: str,
                  purge_other_models: bool) -> models.Filter:
    """
    Points embedded by another model or answered by another LLM that may be deleted now.
    They are never served, but unless purge_other_models is set they are kept until they
    have gone other_model_grace_seconds without a hit, so switching models back and forth
    (e.g. provider = mock for a demo) does not wipe the cache (REVIEW C15).
    """
    # Also matches points with no tags; must_not of both = either tag differs
    other = models.Filter(must_not=[models.Filter(must=_same_models(cfg, answered_by))])
    if purge_other_models or not cfg.other_model_grace_seconds:
        return other
    # last_hit_at_ts equals created_at_ts until the first hit, so this is "since the last
    # hit, or since it was stored". Points without the timestamp cannot be aged: delete them.
    idle = models.Filter(should=[
        models.FieldCondition(key=LAST_HIT_AT_TS,
                              range=models.Range(lt=now - cfg.other_model_grace_seconds)),
        models.IsEmptyCondition(is_empty=models.PayloadField(key=LAST_HIT_AT_TS)),
    ])
    return models.Filter(must=[other, idle])


def expired_filter(cfg: SemanticCacheSettings, now: float, answered_by: str,
                   purge_other_models: bool = False) -> models.Filter:
    """
    Filter that matches points the sweeper should delete: the opposite of fresh_filter,
    including points that are missing a timestamp field, and points embedded by another
    model or answered by another LLM once their grace period is over (at once with
    purge_other_models).
    """
    conditions = []
    if cfg.ttl_seconds:
        conditions.append(models.FieldCondition(
            key=_ttl_field(cfg), range=models.Range(lt=now - cfg.ttl_seconds)))
    if cfg.max_age_seconds:
        conditions.append(models.FieldCondition(
            key=CREATED_AT_TS, range=models.Range(lt=now - cfg.max_age_seconds)))
    if conditions:
        for key in dict.fromkeys([CREATED_AT_TS, _ttl_field(cfg)]):
            conditions.append(models.IsEmptyCondition(is_empty=models.PayloadField(key=key)))
    conditions.append(_other_models(cfg, now, answered_by, purge_other_models))
    return models.Filter(should=conditions)


def decay_factor(age_seconds: float, half_life_seconds: int) -> float:
    """
    0.5 for every half-life of age. 1.0 when decay is off.
    """
    if not half_life_seconds:
        return 1.0
    return 0.5 ** (max(age_seconds, 0.0) / half_life_seconds)


def effective_score(similarity: float, payload: dict, cfg: SemanticCacheSettings, now: float) -> float | None:
    """
    The score a match competes with: its similarity, decayed by age when decay is on.
    Returns None when decay is on but the point's age is unknown.
    """
    if not cfg.decay_half_life_seconds:
        return similarity
    created = payload.get(CREATED_AT_TS)
    if not isinstance(created, (int, float)):
        return None
    return similarity * decay_factor(now - created, cfg.decay_half_life_seconds)


def ensure_indexes(client: QdrantClient, collection_name: str) -> list[str]:
    """
    Create the payload indexes the aging filters use, if they are missing.
    Returns the fields that were indexed now.
    """
    existing = client.get_collection(collection_name).payload_schema or {}
    created = []
    for field, schema in INDEXED_FIELDS.items():
        if field not in existing:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=schema,
                wait=True
            )
            created.append(field)
    return created


def sweep_expired(client: QdrantClient, cfg: SemanticCacheSettings, now: float, answered_by: str,
                  purge_other_models: bool = False) -> int:
    """
    Delete expired points. Returns how many were deleted.
    Safe to run from several places at once: it only ever deletes already-expired points.
    purge_other_models: also delete other models' entries that are still in their grace period.
    """
    expired = expired_filter(cfg, now, answered_by, purge_other_models)
    count = client.count(
        collection_name=cfg.collection_name,
        count_filter=expired,
        exact=True
    ).count
    if count:
        client.delete(
            collection_name=cfg.collection_name,
            points_selector=models.FilterSelector(filter=expired),
            wait=True
        )
    return count


def backfill_timestamps(client: QdrantClient, cfg: SemanticCacheSettings) -> tuple[int, int]:
    """
    Give points stored before aging existed their numeric timestamps, taken from their
    ISO "created_at" text, and tag them as embedded by LEGACY_EMBEDDING_MODEL and answered by
    the mock LLM (they are only served while the gateway uses both). Points without a readable created_at are
    left alone (the sweeper removes them). Returns (updated, skipped).
    """
    missing = models.Filter(must=[
        models.IsEmptyCondition(is_empty=models.PayloadField(key=CREATED_AT_TS))
    ])
    updated = skipped = 0
    offset = None

    while True:
        points, offset = client.scroll(
            collection_name=cfg.collection_name,
            scroll_filter=missing,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False
        )
        for point in points:
            payload = point.payload or {}
            try:
                created = datetime.fromisoformat(payload["created_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                skipped += 1
                continue
            fields = {
                CREATED_AT_TS: created,
                LAST_HIT_AT_TS: created,
                HIT_COUNT: payload.get(HIT_COUNT, 0),
                EMBEDDING_MODEL: payload.get(EMBEDDING_MODEL, LEGACY_EMBEDDING_MODEL),
                ANSWERED_BY: payload.get(ANSWERED_BY, LEGACY_ANSWERED_BY),
            }
            client.set_payload(
                collection_name=cfg.collection_name,
                payload=fields,
                points=[point.id],
                wait=True
            )
            updated += 1
        if offset is None:
            return updated, skipped
