# Paper v1: semantic-cache evaluation snapshot

Prepared 2026-11-15. Raw research records are preserved as observed, including one failed request.
Start with [comparison.png](comparison.png) and [comparison.json](comparison.json), then the reports below.

## What is included

| Directory | Experiment | Raw CSV/metadata pairs |
| --- | --- | ---: |
| [main](main/summary.md) | No cache, old and current gateway; 10/50/200 employees, 20 simulated days, seeds 1-5 | 15 |
| [threshold_0.90](threshold_0.90/summary.md) | Current gateway, direct threshold 0.90, 50 employees, 20 days, seeds 1-5 | 5 |
| [threshold_0.95](threshold_0.95/summary.md) | Same threshold workload at 0.95 | 5 |
| [threshold_0.97](threshold_0.97/summary.md) | Same threshold workload at 0.97 | 5 |
| [threshold_0.99](threshold_0.99/summary.md) | Same threshold workload at 0.99 | 5 |

Every threshold keeps the grey-zone floor at 0.75 and verifier probability threshold at 0.50.
Sampling uses Zipf exponent 1.0, 50% FAQ paraphrases, 5% personal and 3% temporal requests.
These are sequential in-process HTTP experiments with real local embedding/verifier models,
in-memory Qdrant, virtual cache time and replayed recordings. They made no new Gemini calls.
`nocache` is a calculated recorded-token counterfactual, not a separate deployed HTTP service.

## Main finding

These are rounded **means across five seeds**, not pooled count ratios; confidence intervals are in [main/summary.md](main/summary.md).

| Employees | Old hit rate | Current hit rate | Old wrong / hits | Current wrong / hits |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 24.8% | 24.0% | 1.05% | 0.28% |
| 50 | 47.3% | 46.4% | 1.48% | 0.19% |
| 200 | 66.7% | 65.2% | 2.54% | 0.39% |

Pooled over 78,363 requests per configuration, old/current wrong hits were **1,146/169**:
an **85.3% reduction** in this workload. Correct hits were **46,997/46,903**; total hits were
48,143/47,072. Thus most of the lower hit count came from removing wrong reuse, with 94 fewer
correct hits as well. All main requests completed successfully. Old versus current changes several
policies together; this is not a single-feature ablation or proof of general employee-traffic accuracy.

## Direct-threshold comparison

These are **pooled exact counts**, each from the same 15,013 requests; seed means/intervals remain in each summary.
A wrong hit means the returned intent label differs from the requested label, not that an answer's factual quality was checked.

| Direct threshold | Hits | Wrong hits | Replay inference calls | HTTP errors |
| ---: | ---: | ---: | ---: | ---: |
| 0.90 | 7,030 | 21 | 7,982 | 1 |
| 0.95 | 7,003 | 11 | 8,010 | 0 |
| 0.97 | 7,003 | 11 | 8,010 | 0 |
| 0.99 | 7,003 | 11 | 8,010 | 0 |

The 0.95, 0.97 and 0.99 runs have identical per-request hit/miss, returned-intent and correctness
outcomes. Direct-versus-verified classification differs slightly. These data do **not** establish a
unique optimal threshold among those three. The 0.90 run shows more wrong reuse and needs the
failure qualification below. Main's 50-employee cohort and this sweep use different generated request
streams (different company indices); compare thresholds within the sweep, not against main's 50 row.

## Preserved failure and interpretation limits

At 0.90, seed 2, request index 303 (`fin_card_lost`) returned HTTP 504 before inference. Its local wall
time was 19,787,346.837 ms (about 5 h 29 min), with only 0.01562 CPU seconds. Host suspension or a
stall is plausible but not proved. Preserve the record; do not attribute the delay to threshold policy.
Derived reports use corrected accounting: failed requests receive no avoided-inference or token-cost
savings credit. The raw CSV is unchanged. Do not rank threshold performance using this outlier.

The study uses authored synthetic questions. Approximately 8% of requests have synthesized personal/
temporal usage samples rather than their own recorded answers. Actual timing combines measured local
processing with recorded Gemini latency. Recordings are tagged `gemini-3.5-flash-lite`; repricing those
tokens for other models does not measure those models' own response length, quality or latency.
`prices.json` is dated 2026-11-10 and includes secondary, derived and unverified prices. Verify official
prices before monetary claims. Hosting and savings are modelled estimates, not measured cloud bills.

## Provenance and verification

The audit verified all **35 CSV/metadata pairs**, hashes, request counts and seed sets. Main request
streams match across configurations; threshold streams match across thresholds. Current source is
commit `6476b9a`, and the old baseline is `2007299`. Exact embedding/verifier revision IDs were not
exposed by the loaded models and are marked unavailable. This limits exact model reproducibility.
[snapshot_manifest.json](snapshot_manifest.json) records the selected artifact and source fingerprints.
From the project root, with `$gatewayPython` set to the project venv interpreter, verify the bundle:

```powershell
& $gatewayPython eval/cost/snapshot.py --check --root eval/cost/results/paper_v1
```

The check verifies raw metadata/artifact hashes and LF-normalized sources, not model weights, intent labels or price validity.

## Share and continue

Review in GitHub Desktop: `.gitignore`, `.gitattributes`, `eval/cost/analyze.py`, `eval/cost/snapshot.py`,
`tests/test_eval_accounting.py`, `REVIEW.md`, `eval/README.md`, `eval/cost/RESULTS.md`, and this snapshot's
README, comparison PNG/JSON, manifest and five experiment directories above. Exclude `pilot`, old smoke/
debug runs and `eval/runs/`. Review this set in GitHub Desktop, commit it, then use Push origin to share it.

Next: review raw wrong-hit examples, confirm the interrupted 0.90 seed in a fresh folder, validate the
chosen policy on held-out labelled questions, verify official prices, then prepare paper figures and
the live Kubernetes demonstration. To confirm the affected seed without overwriting this snapshot:

```powershell
& $gatewayPython eval/cost/bench.py --configs new --seeds 2 --sizes 50 --days 20 --env GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.90 --env GW_SEMANTIC_CACHE_VERIFY_THRESHOLD=0.75 --env GW_SEMANTIC_CACHE_VERIFIER_THRESHOLD=0.50 --out eval/cost/results/confirmation_threshold_0.90_seed2/raw
& $gatewayPython eval/cost/analyze.py --raw eval/cost/results/confirmation_threshold_0.90_seed2/raw --results eval/cost/results/confirmation_threshold_0.90_seed2
```
