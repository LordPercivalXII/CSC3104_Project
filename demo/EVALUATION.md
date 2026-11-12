# What the existing evaluation demonstrates

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
- `bench.py --old-root` defaults to another machine's temporary directory. Set up
  the documented old commit `2007299` separately and supply its path; the script
  does not create that checkout automatically. The README's Python path is also
  machine-specific. Analysis/plot dependencies must be installed separately.
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
