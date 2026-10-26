"""
Measure how well the cache tells "same question" from "different question", using the
labelled pairs in cache_matching_pairs.py and the settings in gateway.config.
Runs the real models and matching code; no Qdrant needed.

    python smart_ai_gateway/scripts/evaluate_cache_matching.py              # current settings
    python smart_ai_gateway/scripts/evaluate_cache_matching.py --baseline   # also the original setup
    python smart_ai_gateway/scripts/evaluate_cache_matching.py --verbose    # list every decision

Hits = "same" pairs answered from the cache (higher is better).
Wrong = "different" pairs answered from the cache: a wrong answer served (lower is better).
"""

import argparse
import sys
import time
from pathlib import Path

from sentence_transformers import SentenceTransformer

# Make "smart_ai_gateway" importable when this file is run directly as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from smart_ai_gateway.scripts.cache_matching_pairs import PAIRS  # noqa: E402
from smart_ai_gateway.services import cache_aging                # noqa: E402
from smart_ai_gateway.services import semantic_cache as sc       # noqa: E402

# The cache before these improvements: MiniLM embeddings, one 0.90 threshold, no checks
BASELINE_MODEL = cache_aging.LEGACY_EMBEDDING_MODEL
BASELINE_THRESHOLD = 0.90


def similarities(model: SentenceTransformer) -> list[float]:
    cached = model.encode([a for a, _, _ in PAIRS], normalize_embeddings=True)
    new = model.encode([b for _, b, _ in PAIRS], normalize_embeddings=True)
    return [float(x @ y) for x, y in zip(cached, new)]


def evaluate_current() -> list[tuple[bool, str]]:
    """
    (hit?, how it was decided) for every pair, using the gateway's own matching code.
    """
    now = time.time()
    decisions = []
    for (cached, new, _), similarity in zip(PAIRS, similarities(sc.embedding_model)):
        skip = sc.cache_skip_reason(new)
        if skip:
            decisions.append((False, f"skipped: {skip}"))
            continue
        payload = {"prompt": cached, "response": "-", **cache_aging.entry_fields(sc._cfg, now, sc._answered_by)}
        hit, reason = sc.select_match(new, [(similarity, payload, 0)], now)
        if hit:
            verified = f" p={hit['verifier_score']:.2f}" if hit["verifier_score"] is not None else ""
            decisions.append((True, f"{hit['match_type']} sim={similarity:.3f}{verified}"))
        else:
            decisions.append((False, f"sim={similarity:.3f}, {reason}"))
    return decisions


def evaluate_baseline() -> list[tuple[bool, str]]:
    model = SentenceTransformer(BASELINE_MODEL)
    return [(s >= BASELINE_THRESHOLD, f"sim={s:.3f}") for s in similarities(model)]


def report(name: str, decisions: list[tuple[bool, str]], verbose: bool):
    same = [hit for (hit, _), (_, _, is_same) in zip(decisions, PAIRS) if is_same]
    different = [hit for (hit, _), (_, _, is_same) in zip(decisions, PAIRS) if not is_same]
    hits, wrong = sum(same), sum(different)
    precision = hits / (hits + wrong) if hits + wrong else 1.0

    print(f"\n{name}")
    print(f"  hits:      {hits:2d} / {len(same)} same-question pairs ({hits / len(same):.0%})")
    print(f"  wrong:     {wrong:2d} / {len(different)} different-question pairs")
    print(f"  precision: {precision:.0%} of cache answers were correct")

    for (hit, how), (cached, new, is_same) in zip(decisions, PAIRS):
        mistake = hit != is_same
        if verbose or (mistake and not is_same):
            label = ("WRONG ANSWER" if not is_same else "missed") if mistake else ("hit" if hit else "rejected")
            print(f"    [{label:12s}] {new!r} vs cached {cached!r}: {how}")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--baseline", action="store_true", help="also evaluate the original MiniLM / 0.90 setup")
parser.add_argument("--verbose", action="store_true", help="show every decision, not only wrong answers")
args = parser.parse_args()

cfg = sc._cfg
print(f"{len(PAIRS)} labelled pairs")
if args.baseline:
    report(f"Baseline: {BASELINE_MODEL}, threshold {BASELINE_THRESHOLD}, no checks",
           evaluate_baseline(), args.verbose)
verifier = (f"verifier {cfg.verifier_model} p>={cfg.verifier_threshold} from {cfg.verify_threshold}"
            if cfg.verification_enabled else "no verifier")
report(f"Current: {cfg.embedding_model}, direct hit >= {cfg.similarity_threshold}, {verifier}, rule checks",
       evaluate_current(), args.verbose)
