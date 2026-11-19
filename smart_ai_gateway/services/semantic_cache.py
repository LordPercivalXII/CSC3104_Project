import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
# The short timeout means a slow or unreachable Qdrant fails fast; main.py then treats the
# request as a cache miss and asks the LLM (REVIEW C8).
client = QdrantClient(url=QDRANT_URL, timeout=_cfg.qdrant_timeout_seconds)

# Load the embedding model once
embedding_model = SentenceTransformer(_cfg.embedding_model)

# Load the verifier (cross-encoder) once, only if the grey zone is in use
verifier = CrossEncoder(_cfg.verifier_model) if _cfg.verification_enabled else None

# FastAPI runs these functions on several threadpool threads at once, but one model object
# (and its Hugging Face tokenizer, which can raise "Already borrowed") must not be used by
# two threads at the same time. One lock per model: an embedding can still run while the
# verifier is busy (REVIEW C14).
_embedding_lock = threading.Lock()
_verifier_lock = threading.Lock()

# Several threads may append to the same JSON-lines file
_jsonl_lock = threading.Lock()


def generate_embedding(prompt: str) -> list[float]:
    """
    Generate a numerical embedding for the given prompt using the pre-loaded model.
    """
    with _embedding_lock:
        return embedding_model.encode(prompt).tolist()


# --- Eligibility: which prompts may be cached at all (REVIEW C11) -----------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Digits with spaces, dashes or brackets between them; 8 or more digits = a phone number.
# Not part of a longer word, so error codes like 0x80070005 do not count.
_PHONE = re.compile(r"(?<!\w)\+?\(?\d[\d\s()-]{6,}\d(?!\w)")
# Employee, ticket or account numbers: 6+ digits, or 2-5 letters then 4+ digits (EMP12345)
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")
_PREFIXED_ID = re.compile(r"\b[A-Za-z]{2,5}-?\d{4,}\b")
# "I am Sarah", "I'm Tan": the name must start with a capital letter, so "I am unable to log
# in" is fine. (?i:...) makes only the "I am" part case-insensitive.
_SELF_INTRO = re.compile(r"\b(?i:i am|i'm)\s+[A-Z][a-z]+\b")


def _personal_data(prompt: str) -> str | None:
    # What in the prompt identifies a person, or None
    if _EMAIL.search(prompt):
        return "contains an email address"
    for match in _PHONE.finditer(prompt):
        if sum(c.isdigit() for c in match.group(0)) >= 8:
            return "contains a phone number"
    if _LONG_NUMBER.search(prompt) or _PREFIXED_ID.search(prompt):
        return "contains an ID number"
    if _SELF_INTRO.search(prompt):
        return "names the person asking (I am ...)"
    return None


def _personal_phrase(prompt: str) -> str | None:
    # The first personal_phrases entry found as whole words, or None
    return cache_checks.personal_phrase(prompt, _cfg.personal_phrase_list)


def cache_skip_reason(prompt: str) -> str | None:
    """
    Why this prompt must bypass the cache entirely (not searched, not stored), or None.
    The cache is only for short, general questions: time-sensitive, long, pasted or personal
    prompts are answered fresh by the LLM every time and never stored.
    """
    # Personal context takes precedence when several bypass rules apply.
    phrase = _personal_phrase(prompt)
    if phrase:
        return f"personal context ({phrase})"

    if _cfg.skip_time_sensitive:
        word = cache_checks.time_sensitive(prompt)
        if word:
            return f"time-sensitive ({word})"

    if _cfg.cache_max_prompt_chars and len(prompt) > _cfg.cache_max_prompt_chars:
        return f"too long to cache ({len(prompt)} > {_cfg.cache_max_prompt_chars} characters)"

    if "```" in prompt:
        return "contains a code block"

    lines = prompt.count("\n") + 1
    if _cfg.cache_max_prompt_lines and lines > _cfg.cache_max_prompt_lines:
        return f"pasted text ({lines} lines > {_cfg.cache_max_prompt_lines})"

    if _cfg.skip_personal_data:
        found = _personal_data(prompt)
        if found:
            return f"personal data: {found}"

    # The embedding model ignores everything past its token limit, so two long prompts that
    # only differ near the end would look identical
    with _embedding_lock:
        tokens = len(embedding_model.tokenizer(prompt, verbose=False)["input_ids"])
    if tokens > embedding_model.max_seq_length:
        return f"too long for the embedding model ({tokens} > {embedding_model.max_seq_length} tokens)"

    return None


# --- Matching -----------------------------------------------------------------

def select_match(prompt: str, candidates, now: float):
    """
    Decide which candidate, if any, answers this prompt.
    Returns (hit, reason): hit is a result dictionary or None; reason explains a miss.
    See select_match_detailed for the steps.
    """
    hit, details = select_match_detailed(prompt, candidates, now)
    return hit, details["reason"]


def select_match_detailed(prompt: str, candidates, now: float):
    """
    Decide which candidate, if any, answers this prompt.
    candidates: (similarity, payload, point_id) for the nearest stored questions.
    Returns (hit, details): hit is a result dictionary or None. details describes the
    decision for hits and misses alike (for the decision log):
        stage           direct_hit, verified_hit, rule_rejected, verifier_rejected,
                        below_threshold, or miss (no usable cached entries)
        reason          the decision in words (printed on a miss)
        top_similarity  cosine similarity of the closest usable candidate
        best_score      the highest effective score (similarity after age decay)
        verifier_score  the verifier's best probability, when it ran
        rule_reason     the first rule check that rejected a candidate
        verify_ms       time the verifier took, when it ran

    1. score = similarity, decayed by age when decay is on
    2. below verify_threshold: not the same question
    3. a rule check objects (different numbers, opposite words, ...): not the same question
    4. at or above similarity_threshold: hit
    5. in between (grey zone): hit only if the verifier agrees
    """
    direct, grey = [], []
    details = {
        "stage": "miss", "reason": "no cached entries", "top_similarity": None, "best_score": None,
        "verifier_score": None, "rule_reason": None, "verify_ms": None,
    }

    for similarity, payload, point_id in candidates:
        cached_prompt = payload.get("prompt")

        # Check if the payload contains a valid response
        if not isinstance(payload.get("response"), str) or not isinstance(cached_prompt, str):
            continue

        if details["top_similarity"] is None or similarity > details["top_similarity"]:
            details["top_similarity"] = similarity

        score = cache_aging.effective_score(similarity, payload, _cfg, now)
        if score is None:
            continue
        if details["best_score"] is None or score > details["best_score"]:
            details["best_score"] = score
            details["reason"] = f"best score {score:.3f} below verify_threshold"
        if score < _cfg.verify_threshold:
            continue

        conflict = cache_checks.conflict(prompt, cached_prompt)
        if conflict:
            details["reason"] = f"rule check: {conflict}"
            if details["rule_reason"] is None:
                details["rule_reason"] = conflict
            continue

        entry = (score, similarity, payload, point_id)
        if score >= _cfg.similarity_threshold:
            direct.append(entry)
        else:
            grey.append(entry)

    if direct:
        details["stage"] = "direct_hit"
        return _result(max(direct, key=lambda e: e[0]), "direct", None, now), details

    if grey and verifier is not None:
        # The verifier reads both questions together; one batch for all grey-zone candidates
        started = time.perf_counter()
        with _verifier_lock:
            probabilities = verifier.predict([(prompt, e[2]["prompt"]) for e in grey])
        details["verify_ms"] = round((time.perf_counter() - started) * 1000, 1)
        verified = max(zip(probabilities, grey), key=lambda pair: pair[0])
        probability = float(verified[0])
        details["verifier_score"] = probability
        if probability >= _cfg.verifier_threshold:
            details["stage"] = "verified_hit"
            return _result(verified[1], "verified", probability, now), details
        details["stage"] = "verifier_rejected"
        details["reason"] = f"verifier rejected (p={probability:.2f})"
    elif grey:
        # Grey-zone candidates but the verifier is off: below similarity_threshold
        details["stage"] = "below_threshold"
    elif details["rule_reason"] is not None:
        details["stage"] = "rule_rejected"
    elif details["best_score"] is not None:
        details["stage"] = "below_threshold"

    return None, details


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
        # Needed to store this prompt as another wording of the same answer
        "created_at_ts": created if isinstance(created, (int, float)) else None,
        "paraphrase_of": payload.get(cache_aging.PARAPHRASE_OF),
    }


def search_cache(prompt: str, embedding: list[float], now: float | None = None):
    """
    Search Qdrant collection for a cached response to the same question that has not aged out.
    Returns a dictionary on a hit, or None on a miss.
    """
    hit, _ = search_cache_detailed(prompt, embedding, now)
    return hit


def search_cache_detailed(prompt: str, embedding: list[float], now: float | None = None):
    """
    Like search_cache, but returns (hit, details): details are select_match_detailed's,
    plus search_ms (time Qdrant took). Raises if Qdrant cannot be reached.
    """
    now = time.time() if now is None else now

    # Expired entries are filtered out by Qdrant itself (see cache_aging.fresh_filter).
    # No score_threshold here, so a miss can still log how close the best entry was.
    started = time.perf_counter()
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=cache_aging.fresh_filter(_cfg, now, _answered_by),
        limit=_cfg.search_candidates,
        with_payload=True
    )
    search_ms = round((time.perf_counter() - started) * 1000, 1)

    hit, details = select_match_detailed(
        prompt,
        [(match.score, match.payload or {}, match.id) for match in results.points],
        now
    )
    details["search_ms"] = search_ms

    # Near-miss logging: these lines show which thresholds are costing hits
    if hit is None:
        print(f"Cache miss: {details['reason']}")
    else:
        verified = f", verifier p={hit['verifier_score']:.2f}" if hit["verifier_score"] is not None else ""
        print(f"Cache hit ({hit['match_type']}): score {hit['effective_score']:.3f}{verified}")
    return hit, details


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


# --- Storing --------------------------------------------------------------------

def store_cache(prompt: str, response: str, embedding: list[float], now: float | None = None,
                extra_payload: dict | None = None):
    """
    Store a question embedding and its generated answer. Returns the new point's id.
    extra_payload: more payload fields, which may override the aging fields (paraphrases).
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
                    **cache_aging.entry_fields(_cfg, now, _answered_by),
                    **(extra_payload or {})
                }
            )
        ],
        wait=True
    )

    return point_id


def _paraphrase_filter(root_id) -> models.Filter:
    # Every extra wording that points at this answer
    return models.Filter(must=[
        models.FieldCondition(key=cache_aging.PARAPHRASE_OF, match=models.MatchValue(value=str(root_id)))
    ])


def store_paraphrase(prompt: str, embedding: list[float], hit: dict, now: float | None = None):
    """
    After the verifier confirmed that prompt asks the same as a cached question, store prompt
    as another wording of the same answer, so the next person who words it this way gets a
    direct hit without the verifier. Returns the new point's id, or None if not stored.

    The new point copies the answer and points at the original entry with paraphrase_of
    (always the original, never another paraphrase). It keeps the original's created_at_ts,
    so max_age_seconds and decay still count from when the answer was written; only its
    last-hit time is new. Not stored when max_paraphrases is reached or when an almost
    identical wording (paraphrase_duplicate_similarity) is already cached.
    Two requests at the same moment can both pass the cap check and store one extra point;
    like the hit count, that is accepted.
    """
    if _cfg.max_paraphrases <= 0:
        return None
    now = time.time() if now is None else now
    root_id = hit.get("paraphrase_of") or hit["point_id"]

    # Skip if this wording (or one almost identical) is already stored
    nearest = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=cache_aging.fresh_filter(_cfg, now, _answered_by),
        limit=1,
        with_payload=False
    ).points
    if nearest and nearest[0].score >= _cfg.paraphrase_duplicate_similarity:
        print(f"Paraphrase not stored: an entry with similarity {nearest[0].score:.3f} exists")
        return None

    existing = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=_paraphrase_filter(root_id),
        exact=True
    ).count
    if existing >= _cfg.max_paraphrases:
        print(f"Paraphrase not stored: answer {root_id} already has {existing} wordings")
        return None

    extra = {cache_aging.PARAPHRASE_OF: str(root_id)}
    if hit.get("created_at_ts") is not None:
        extra[cache_aging.CREATED_AT_TS] = hit["created_at_ts"]
    return store_cache(prompt, hit["response"], embedding, now=now, extra_payload=extra)


# --- "This answer was wrong" feedback -------------------------------------------

def append_jsonl(path: Path, record: dict):
    """
    Append one JSON object as a line to path, creating its folder if needed.
    A path of "-" writes the line to standard output instead, where Kubernetes keeps it
    (kubectl logs) and a log collector can gather it from every replica.
    """
    path = Path(path)
    line = json.dumps(record, ensure_ascii=False, default=str)
    if str(path) == "-":
        with _jsonl_lock:
            print(line, flush=True)
        return
    with _jsonl_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")


def parse_point_id(text: str):
    """
    A Qdrant point id from text (a UUID, or a whole number for old entries), or None.
    """
    text = text.strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        pass
    return int(text) if text.isdigit() else None


def report_wrong_match(point_id: str, prompt: str, reason: str | None, feedback_path: Path,
                       now: float | None = None):
    """
    A user said the cached answer served for prompt was wrong. Deletes the entry together
    with every other wording of the same answer (they all share the answer that was wrong),
    and appends the question pair to feedback_path for the evaluation set.
    Returns what was done, or None if no such entry exists.

    Deleting (rather than marking the entry as blocked) keeps the read path unchanged: no
    extra filter on every search, and the sweeper needs no new rule. The next person who asks
    gets a fresh LLM answer, which is cached under their own wording.
    """
    now = time.time() if now is None else now
    parsed = parse_point_id(point_id)
    if parsed is None:
        return None
    points = client.retrieve(collection_name=COLLECTION_NAME, ids=[parsed], with_payload=True)
    if not points:
        return None
    payload = points[0].payload or {}

    # The original entry and all its paraphrases
    root = parse_point_id(str(payload.get(cache_aging.PARAPHRASE_OF) or parsed))
    family = models.Filter(should=[
        models.HasIdCondition(has_id=[i for i in dict.fromkeys([parsed, root]) if i is not None]),
        _paraphrase_filter(root if root is not None else parsed),
    ])
    deleted = client.count(collection_name=COLLECTION_NAME, count_filter=family, exact=True).count
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(filter=family),
        wait=True
    )

    record = {
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "point_id": str(parsed),
        "prompt": prompt,
        "cached_prompt": payload.get("prompt"),
        "cached_response": payload.get("response"),
        "paraphrase_of": payload.get(cache_aging.PARAPHRASE_OF),
        "reason": reason,
        "deleted_points": deleted,
        # Ready to copy into cache_matching_pairs.py as (cached, new, is_same=False)
        "label": "different",
    }
    append_jsonl(feedback_path, record)
    print(f"Feedback: removed {deleted} cache entries for point {parsed}")
    # The cached prompt is another employee's wording, so it is logged but not returned
    return {"point_id": str(parsed), "deleted_points": deleted}
