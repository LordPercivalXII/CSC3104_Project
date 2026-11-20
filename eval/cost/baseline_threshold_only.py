"""Threshold-only semantic cache baseline (the GPTCache-style decision rule), on the same benchmark.

Runs eval/cost/bench.py unchanged, as a single in-process job, with the gateway reduced to
"embed the prompt, reuse the nearest cached answer if its cosine similarity >= threshold":

  * verifier off       --env GW_SEMANTIC_CACHE_VERIFY_THRESHOLD = the similarity threshold
  * eligibility off    --env GW_SEMANTIC_CACHE_SKIP_TIME_SENSITIVE=false, SKIP_PERSONAL_DATA=false,
                       CACHE_MAX_PROMPT_CHARS=0, CACHE_MAX_PROMPT_LINES=0
  * rule checks off    patched here (cache_checks.conflict always returns None); the gateway
                       code itself is not modified, so its provenance fingerprint is unchanged.

Example (inside the gateway image, repo mounted at /src):
  python eval/cost/baseline_threshold_only.py --configs new --code-root /src --seeds 1-5 --sizes 50 \
      --days 20 --env GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.80 --env GW_SEMANTIC_CACHE_VERIFY_THRESHOLD=0.80 \
      --env GW_SEMANTIC_CACHE_SKIP_TIME_SENSITIVE=false --env GW_SEMANTIC_CACHE_SKIP_PERSONAL_DATA=false \
      --env GW_SEMANTIC_CACHE_CACHE_MAX_PROMPT_CHARS=0 --env GW_SEMANTIC_CACHE_CACHE_MAX_PROMPT_LINES=0 \
      --out eval/cost/results/baseline_threshold_only/t0.80/raw
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench  # noqa: E402

_original_setup = bench._setup_app


def _setup_without_rules(a):
    main, sc, llm = _original_setup(a)
    from smart_ai_gateway.services import cache_checks
    cache_checks.conflict = lambda prompt, cached_prompt: None   # threshold-only: no rule checks
    assert sc.cache_checks.conflict("move 5 to 10", "move 10 to 5") is None
    print("[baseline] rule checks disabled; verifier/eligibility set by --env", flush=True)
    return main, sc, llm


bench._setup_app = _setup_without_rules
if "--child" not in sys.argv:
    sys.argv.append("--child")   # run in this process so the patch applies
bench.main()
