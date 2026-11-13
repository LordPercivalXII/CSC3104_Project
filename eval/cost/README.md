# Cost and reliability benchmark for the semantic cache

Question: does the prompt cache save an SME money, and how often does it serve a wrong answer?

Pipeline: `record_real_answers.py` (real Gemini token sizes) -> `bench.py` (replays a synthetic
workload through the real FastAPI app, old vs new code) -> `analyze.py` (tables, CIs, plots).

All commands run from the repo root with the project venv (Git Bash):

```
PY=./smart_ai_gateway/.venv/Scripts/python
export HF_HUB_OFFLINE=1 PYTHONIOENCODING=utf-8
```

## Inputs (written by others)

* `data/intents.json`: intents, paraphrases, confusable ids, personal templates, time-sensitive prompts.
* `prices.json`: LLM and hosting prices (with source URLs and retrieval date).
* `fixtures/`: tiny fake versions used by the smoke test only.

## 1. Record real answers (spends API quota; run once)

```
$PY eval/cost/record_real_answers.py --limit 3 --rpm 8        # trial
$PY eval/cost/record_real_answers.py --extra 8 --rpm 8        # everything + 8 personal/time samples
```

Uses the gateway's own `llm.client`, model, system instruction, thinking level and max tokens (from
`gateway.config`), key from `.env`. Resumable (skips ids already in `data/real_answers.jsonl`). One JSONL line
per answer: tokens (prompt/output/thinking/total), finish reason, latency, model, timestamp.
`--dry-run` lists what would be recorded.

## 2. Run the benchmark

```
# main grid: 3 company sizes, 5 seeds, default popularity (s=1.0) and phrasing (50% paraphrased)
$PY eval/cost/bench.py --configs nocache,old,new --seeds 1-5 --sizes 10,50,200 --days 20

# sensitivity grid: one mid-size company, Zipf s and paraphrase share varied, 3 seeds
$PY eval/cost/bench.py --configs nocache,old,new --seeds 1-3 --sizes 50 --days 20 \
    --zipf-s 0.6,0.8,1.0,1.2 --paraphrase-share 0.2,0.5,0.8
```

Raw CSVs go to `results/raw/` (one per config/seed/s/paraphrase/sizes/days). Every new CSV has a `.meta.json`
sidecar containing source, input, harness and output hashes, settings, resolved model revisions (when available),
options and dependency versions. Existing files are skipped only when their provenance and CSV hash match.
Untracked legacy outputs or changed inputs fail with a message; use a fresh `--out` or explicitly `--force`.
`old` runs a temporary frozen `git archive` of commit `2007299` (`--old-ref` changes it; `--old-root` accepts an
explicit external baseline); `new` runs the working tree (`--new-root`), each in its own subprocess.
Other options: `--isolation shared` (all companies
in one cache), `--variation 0..1` (per-company popularity), `--lam`, `--personal-share`, `--time-share`,
`--env KEY=VAL` (gateway settings, e.g. `GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.9`).
Use a separate `--out` per experiment and a matching `analyze.py --raw`.

Inspect a workload alone: `$PY eval/cost/workload.py --seed 1 --sizes 10,50 --days 2` (assumptions are in its docstring).

## 3. Analyse

```
$PY eval/cost/analyze.py                      # reads results/raw, writes results/summary.{csv,md}, economics.csv, *.png
```

Options: `--fixed-only`, `--include-cluster-fee`, `--plot-hosting "e2-standard-2 us-central1"`, deployment shape
(`--gateway-replicas 3 --gateway-cpu 0.25 --gateway-mem-gb 0.75 ...`, defaults = `k8s/*.yaml` requests),
`--workdays-per-month 22`.

Outputs: `summary.csv` (rates, latency; mean and `_ci95` per column), `economics.csv` (per LLM price x hosting
option x config x size), `summary.md` (readable tables), `hit_wrong_rates.png`, `savings_vs_volume.png`,
`sensitivity.png`.

## 4. Smoke test (about 30 s, fixtures only)

```
$PY eval/cost/bench.py --configs nocache,old,new --seeds 1-2 --sizes 8 --days 1 \
  --intents eval/cost/fixtures/intents.json --recordings eval/cost/fixtures/real_answers.jsonl --out eval/cost/results/review_smoke/raw
$PY eval/cost/analyze.py --raw eval/cost/results/review_smoke/raw --results eval/cost/results/review_smoke --prices eval/cost/fixtures/prices.json
```

PowerShell equivalent (no API calls; requires the two embedding/verifier models already cached):

```powershell
$env:HF_HUB_OFFLINE = "1"
.\smart_ai_gateway\.venv\Scripts\python.exe eval/cost/bench.py --configs nocache,old,new --seeds 1-2 --sizes 8 --days 1 --intents eval/cost/fixtures/intents.json --recordings eval/cost/fixtures/real_answers.jsonl --out eval/cost/results/review_smoke/raw
```

The fixture recordings and prices are artificial. This checks the pipeline, not cost savings for a real SME.
`RESULTS.md` contains a historical report from 2026-11-10, before the review fixes. Its full raw runs are
not present in this checkout; the checked-in fixture CSVs cannot reproduce those headline numbers.
Re-run the main and sensitivity grids on the revised code before using updated numerical conclusions.

## Methodology

* **Replayed LLM.** The scheduler's `inference_fn` is replaced by a replay of the recorded real answer for the
  request's intent, with its recorded tokens and latency. Intents with no recording get a synthetic answer
  and tokens drawn from the empirical distribution of the recordings (flagged `synthetic`; the share is in
  `summary.csv`). Each answer ends in `[ref:<intent_id>]`, so the intent behind any served answer is known.
* **Scoring.** A request is a hit if no LLM call happened. A hit is correct iff the served answer's intent equals
  the asked intent. Personal and time-sensitive requests have unique intents, so any hit on them is wrong
  (also reported as `wrong_rate`). `confusable_error` marks wrong hits that landed on a listed confusable intent.
  This labels whether an answer was reused for the intended question; it does not verify the factual accuracy
  of the original LLM answer. A correct intent label is not a general answer-quality score.
* **Wrong-answer rate** is reported per request and per hit. Savings count only correct hits.
* **Money.** Saved = tokens the request would have used minus tokens actually sent, priced per `prices.json`
  (thinking tokens billed as the entry says). Overhead = measured gateway CPU-seconds x hosting $/vCPU-s
  (marginal) + fixed monthly cost of the always-on deployment (nodes needed for the k8s requests incl. a node
  reserve). The fixed cost already pays for the CPU, so adding both is conservative (`--fixed-only` removes it).
  Break-even = fixed / (saved per request - CPU cost per request).
  Applying another model's price to these same token counts is a pricing scenario, not a benchmark of that
  model. Model-specific answer length, reasoning tokens, latency and accuracy require separate recordings.
* **Statistics.** Metrics per run, then mean +/- 95% CI (t-distribution) across seeds. Same seeds produce the same
  workload for every config, so old/new are compared on identical request streams (paired by seed).
* **Virtual time.** The cache's clock follows the workload's timestamps, so TTL, sliding TTL and max age behave as
  they would over D days without waiting.
* **Latency.** Hits: measured gateway wall time. Misses: gateway time + recorded real Gemini latency.

## Threats to validity

1. **Synthetic workload.** Questions come from a hand-built intent list with Zipf popularity and authored
   paraphrases. Real employees write messier text, ask long-tail questions, and the popularity curve is
   unmeasured. The sensitivity sweep over s and paraphrase share bounds this, it does not remove it.
2. **Confusable pairs are author-chosen.** The wrong-answer rate depends on how many near-duplicates intents.json
   contains, and on how adversarial they are; it is not a rate for real traffic.
3. **Single-candidate effects.** Each intent has one canonical answer; the cache cannot be "right in one phrasing
   and wrong in another" beyond what intent matching captures. Answers that depend on the asker are not modelled
   except through personal prompts.
4. **Hit rate extrapolation.** Rates measured over D simulated days (cold start included) are assumed to hold for
   a month. Weekends and growth of the question set are not modelled.
5. **Pricing.** Token prices are list prices for the paid tier from `prices.json` (some secondary sources, and a
   promotional price on one model). The free tier makes the LLM cost zero, so there is nothing to save there; the
   gateway then pays off only through latency/rate limits. Context caching discounts, batch tiers and Gemini
   rate-limit costs are ignored.
6. **Hosting is estimated, not measured.** CPU-seconds are measured on a developer laptop with one torch thread
   and the test-client baseline subtracted, not on a cloud vCPU. The k8s requests may under-provision real
   latency. Storage, egress, monitoring, engineering time and Redis/Qdrant growth are not costed.
7. **Sequential requests.** No concurrency, so request coalescing and queueing delay are not exercised; real
   misses would also wait in the fair scheduler.
8. **In-memory Qdrant** uses exact search. A server's approximate HNSW search can change candidate recall as
   well as latency. This benchmark does not establish production Qdrant throughput or latency.
9. **Per-company cache.** The gateway has no tenant notion; the default per-company isolation assumes one
   deployment per company. `--isolation shared` shows cross-company sharing (and its privacy risk).
10. **Old vs new** differ in more than the matching rules (token accounting, cacheable flag); the benchmark
    compares the whole products, not single changes.
