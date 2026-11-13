# What the existing evaluation demonstrates

Update for 2026-11-13: the revised runner now fails fast, requires exactly three Ready gateways,
restores selected settings, uses fresh run directories and defaults to mock-only scenarios.
The plotter now evaluates exact completion windows and labels them as completions. The cost
benchmark now archives a portable baseline and validates provenance before resuming. These fixes
do not retroactively validate or replace the saved historical measurements discussed below.
See [../eval/README.md](../eval/README.md) and [../REVIEW.md](../REVIEW.md) for the current handoff.

The `eval/` folder already generates traffic, records outcomes and creates graphs.
Its saved graphs demonstrate the particular workloads and configurations recorded
in their inputs. A graph does not establish that its labels, assumptions or
experimental controls are correct. These limitations were identified by reviewing
the code and saved files at commit `dacbdf7`; the original results are unchanged.

## Scheduler experiments: concurrent HTTP requests

1. `eval/run_experiments.sh` changes the deployed gateway's backend, shared/per-pod
   rate limit, provider and coalescing settings. It attempts to pin three pods.
2. `eval/loadtest.py` sends HTTP requests concurrently and records client send time,
   completion time, response latency, status, source and coalescing in CSVs.
3. E1 sends 18 requests from one heavy user followed by one request from each of
   three light users. It compares light-user delay under queueing configurations.
4. E3 sends 18 requests/minute for two minutes against different limiter setups.
   These scheduler experiments use the `/scheduler/chat` endpoint and mock answers.
5. The duplicate experiment sends the same question simultaneously, with sharing
   enabled/disabled. It uses Gemini and also leaves semantic caching enabled.
6. `eval/plot.py` reads the CSVs, writes the PNGs and `eval/results/summary.md`.

The checked-in scheduler data has 621 requests across 39 trials, all HTTP 200.
Its fairness and duplicate summary arithmetic agrees with the CSVs. These runs
record commit `f0c9cbb`, not the later `dacbdf7` cache changes.

### Limits to present accurately

- Two setup records report four pods although the experiment intends three. That
  weakens claims of an equal-capacity, controlled comparison.
- The rate-limit graph counts response completions, sampled at whole-second
  boundaries. It labels these as LLM starts in any 60 seconds. Exact rolling
  windows in the saved completions yield 19/10 rather than the reported 18/9.
  This does not establish a rate-limiter defect. Actual server call-start events
  are required to test the strict quota; SDK retries also need separate accounting.
- Duplicate-test savings combine in-flight sharing and subsequent cache hits.
  A successful non-coalesced response is not proof of exactly one Gemini HTTP
  attempt, since retries can occur inside the SDK.
- These runs do not establish sustained maximum throughput, HPA behaviour, cluster
  CPU/memory usage, or fairness for arbitrary real employee traffic.
- The runner changes deployment settings and clears test cache/rate-limit state.
  Running it is an experiment against the selected cluster, not a passive viewer.

## Cost/correctness experiments: sequential replay

1. `eval/cost/record_real_answers.py` calls Gemini for labelled intents and saves
   answers, token usage, finish reasons and latency in JSONL. This step uses API
   quota. Existing data contains 151 FAQ recordings plus eight extra samples.
2. `workload.py` generates seeded synthetic employees/questions over virtual days:
   popular FAQs, authored paraphrases, confusable intents, personal and time-based
   prompts. These are assumptions about traffic, not observed SME traffic.
3. `bench.py` compares no cache, an old checkout, and the current code. It runs the
   actual FastAPI endpoint with real embedding/verifier models, an in-memory
   Qdrant and replayed answers. Requests are sequential and cache time is virtual.
4. Each replayed answer carries an intent marker. The benchmark calls a hit
   correct when the returned marker matches the expected question's intent.
   This assesses cache selection, not the factual quality of Gemini's answer.
5. `analyze.py` calculates hit/wrong-hit rates, calls and tokens avoided, measured
   gateway CPU time, and price-based savings estimates, then writes CSVs/plots.
   Miss latency combines local processing with recorded Gemini latency; it is
   not a fresh live end-to-end deployment measurement.

### Limits to present accurately

- `eval/cost/RESULTS.md` describes full main/sensitivity runs whose raw outputs are
  absent from this checkout (`eval/cost/results/` is git-ignored). Their headline
  aggregate results cannot be recomputed from the available files alone.
- `fixtures/results/` contains tiny fake-data smoke tests. Those graphs are not
  the large research experiment or evidence of real Gemini quality/cost savings.
- Historical versions defaulted to another machine's temporary directory. The current runner archives
  commit `2007299` automatically (`--old-ref` overrides it); no manual baseline checkout is required.
  The current README uses a repository-relative Python path. Historical runner
  versions needed a manually prepared baseline path. Analysis/plot dependencies
  must be installed separately.
- This sequential test does not exercise multi-pod Redis coordination, request
  coalescing or autoscaling. The no-cache cost baseline is computed without the app.
- Cross-provider price comparisons reprice the recorded workload. They do not
  measure those providers' own output lengths, accuracy or response times.
- Old versus new changes several policies together. To show the threshold
  trade-off, run a separate sweep holding the other policies and workloads fixed.
- Record seeds, exact commit/image/configuration, dataset and prices with the
  exported raw results. Keep correctness experiments and live load measurements
  distinguishable in the presentation.

## How the live demo complements this

Locust and Grafana let the audience observe real concurrent requests, routing to
multiple gateway replicas, cache outcomes, model-function executions and queueing.
They do not replace labelled correctness tests. A strong presentation uses the
offline evaluation for the research comparison and the live demonstration to show
how the deployed mechanism behaves.

Locust's default `cache` scenario now reads the same `eval/cost/data/intents.json`
as the cost evaluation. It uses the default 92% FAQ, 5% personal and 3% time-sensitive
category mix, with 50% alternative wording within FAQ inputs where available and
Zipf exponent 1.0. `misses` and `scheduler` remain separate generated stress cases.
Sharing the corpus does not reproduce the offline workload's company/day model,
virtual time or sequential execution, and Locust does not score expected intent.
The mock demo generates mock answers; it does not use the evaluation's recorded
Gemini responses. A visible hit in Grafana proves reuse occurred, not correct reuse.
