# Evaluation and live demonstration

These tools answer different questions:

| Tool | What it measures | Main limit |
| --- | --- | --- |
| `cost/bench.py` | Cache reuse and wrong-intent reuse across labelled synthetic request streams | Sequential offline replay, authored workload, local exact Qdrant |
| `cost/analyze.py` | Avoided token cost under recorded usage and price/hosting assumptions | Estimates, not measured invoices; saved tokens are counterfactual |
| `loadtest.py` / `run_experiments.sh` | Concurrent request wait, round-robin fairness, shared limits and coalescing | Requires a running cluster and controlled configuration |
| `../demo/` | Live traffic, cache hit/miss outcomes, inference executions and latency | Mock inference by default; no monetary savings claim |

The demo's default Locust `cache` scenario reads the same `cost/data/intents.json`
as the offline cost benchmark, rather than maintaining a separate FAQ list. It
samples the default FAQ/personal/time categories and canonical/paraphrase wording,
but runs them concurrently in real time against the selected gateway. It does not
reproduce simulated company-days, replay recorded answers or score answer intent.
The `misses` and `scheduler` stress scenarios still use generated numeric questions.

The cost pipeline is not a live browser traffic simulation. `cost/bench.py` runs
sequential requests through the actual FastAPI endpoint in-process, with real
embedding/verifier models, in-memory Qdrant and replayed recorded answers. It
writes request CSVs and provenance. `cost/analyze.py` then writes tables and static
PNG graphs, including hit/wrong-hit rates and estimated monetary savings. There
are visuals after analysis, but no interactive live dashboard in that pipeline.

For a research article, use the labelled cache experiment to study the savings/correctness
trade-off, and use the concurrency experiments to evaluate distributed operation. A cache
answer with the correct intent label can still contain a factual mistake from the original LLM.
Validate on held-out employee-like questions and report workload assumptions before generalising
to SMEs. Historical CSVs and graphs under `results/` have not been rerun for the current fixes.
The historical full cost runs described by `cost/RESULTS.md` are absent from this checkout;
the checked-in fixture results are small artificial smoke tests.

## Scheduler experiments

Start the existing Kubernetes application first and stop unrelated load. From Git Bash with
the project venv activated:

```bash
bash eval/run_experiments.sh e1
bash eval/run_experiments.sh e3
# Explicit opt-in: real Gemini calls and quota consumption
bash eval/run_experiments.sh dup
```

With no argument, the runner runs mock-only `e1` and `e3`. It targets `kind-llm-gateway`
(`CONTEXT` overrides it), fixes the HPA and Deployment at three replicas, and requires exactly
three Running/Ready gateways before each trial. It gives queued requests a 600-second deadline,
uses an isolated scheduler prefix, fixes sliding-window mode, and fails on failed load-generator
requests. It restores the selected environment entries, original replica count and HPA bounds
on success, error or interrupt. Restoration failures are reported; inspect the saved patch if one occurs.
Do not mix old and revised scheduler workers while requests are queued.

Fresh output goes to `eval/runs/<UTC timestamp>-<PID>/`. Override `RESULTS` only when you
intend to append to that directory. The runner prints the matching graph command:

```bash
python eval/plot.py --results eval/runs/<run-directory>
```

E3 graphs use successful response **completion** times. Their rolling-window peaks include
fractional event boundaries, but are not proof of quota compliance at provider **start** times.
Duplicate-call counts inferred from successful response flags omit failed executions and SDK retries.
Use the gateway inference counter as additional evidence, with its documented retry limitations.

See [cost/README.md](cost/README.md) for the offline cost benchmark and
[../demo/README.md](../demo/README.md) for mock and Gemini startup commands.
