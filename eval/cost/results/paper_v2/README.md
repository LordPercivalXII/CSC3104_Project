# Paper v2: final semantic-cache evaluation snapshot

Prepared 2026-11-20 for the final report. It supersedes [paper_v1](../paper_v1/README.md), which measured an
earlier version of the gateway. Every number in the report's evaluation (Section IV-B/IV-C, Fig. 3) and
in the evaluation slides comes from this folder. Raw records are preserved as observed.

## What is included

| Directory | Experiment | Raw CSV/metadata pairs |
| --- | --- | ---: |
| [main](main/summary.md) | No cache, original (`old`) and revised (`new`) gateway; 10/50/200 employees, 20 days, seeds 1-5 | 15 |
| [threshold_0.90](threshold_0.90/summary.md) | Revised gateway, direct threshold 0.90, 50 employees, 20 days, seeds 1-5 | 5 |
| [threshold_0.95](threshold_0.95/summary.md) | Same at 0.95 | 5 |
| [threshold_0.97](threshold_0.97/summary.md) | Same at 0.97 (the shipped default) | 5 |
| [threshold_0.99](threshold_0.99/summary.md) | Same at 0.99 | 5 |
| [sens](sens/summary.md) | Workload sensitivity: no cache, old and new; 50 employees, Zipf s 0.6/0.8/1.0/1.2 x paraphrase share 0.2/0.5/0.8, seeds 1-3 | 108 |

Unless a directory says otherwise: Zipf s = 1.0, 50% of FAQ questions reworded, 5% personal and 3%
time-sensitive prompts, 3 questions per employee per day, verification floor 0.75, verifier probability 0.50.
These are in-process HTTP replays through the real FastAPI application with the real embedding and verifier
models, in-memory Qdrant, virtual cache time and recorded Gemini 3.5 Flash-Lite answers. No new Gemini calls.
`nocache` is a recorded-token counterfactual, not a separate deployed service.

**How the runs were made.** The `new` runs (main, thresholds, sens) and the `old` sensitivity runs were made
on 19-20 Nov 2026 inside the project's Docker image (`smart-gateway:dev`, Linux, `OMP_NUM_THREADS=1`) from the
frozen submission code. The `old` and `nocache` main runs were copied unchanged from paper_v1: the original
gateway's code (commit `2007299`) did not change, and its content hash in the metadata is identical to the
old sensitivity runs. Those paper_v1 runs were made on Windows; latency for `old` main is therefore from a
different host than `new` main and should not be compared at the millisecond level.

## Main finding

Rounded means across five seeds; confidence intervals are in [main/summary.md](main/summary.md).

| Employees | Old hit rate | New hit rate | Old wrong / hits | New wrong / hits | New tokens avoided |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 24.8% | 25.0% | 1.05% | 0.27% | 25.3% |
| 50 | 47.3% | 49.0% | 1.48% | 0.18% | 49.2% |
| 200 | 66.7% | 69.1% | 2.54% | 0.35% | 69.8% |

Pooled over 78,363 requests per configuration: old 48,143 hits with 1,146 wrong; new **49,830 hits with
162 wrong** (49,668 correct). That is an **85.9% reduction in wrong reuse while serving 2,671 more correct
answers**. No request failed. Median latency: cache hit 60-100 ms, LLM answer about 3.4 s (recorded).

## Direct-threshold sweep

Pooled exact counts from the same 15,013 requests per threshold:

| Direct threshold | Hits | Wrong hits | HTTP errors |
| ---: | ---: | ---: | ---: |
| 0.90 | 7,419 | 21 | 0 |
| 0.95 | 7,380 | 11 | 0 |
| 0.97 | 7,380 | 11 | 0 |
| 0.99 | 7,380 | 11 | 0 |

0.95, 0.97 and 0.99 give identical per-request outcomes: above 0.95 the verifier and rule checks, not the
threshold alone, decide correctness. The data do not single out one of those three; 0.97 keeps a margin.
The paper_v1 0.90 HTTP 504 outlier did not recur.

## Workload sensitivity

Twelve workloads x three seeds at 50 employees (summary and per-workload intervals in [sens/summary.md](sens/summary.md),
chart [sens/sensitivity.png](sens/sensitivity.png)):

- Revised gateway correct hits: 26.2% (s 0.6, 80% reworded) to 64.9% (s 1.2, 20% reworded); higher when a few
  questions dominate, lower when more questions are reworded. Wrong hits stayed under 1% of hits in every workload.
- Against the original on the same seeded requests: **fewer wrong hits in 12 of 12 workloads** (70-97% fewer;
  pooled 949 -> 142, 85.0%) and **more correct hits in 12 of 12** (+1.4 to +5.7 percentage points).
- 0 request errors in all 108 runs.

## Threshold-only baseline (GPTCache-style)

[baseline_threshold_only](baseline_threshold_only/) replays the same 50-employee workload (seeds 1-5, 15,013 requests)
through a cache that reuses the nearest answer whenever cosine similarity reaches one threshold: the decision rule GPTCache
uses by default. Verifier, rule checks and eligibility filter are off (`eval/cost/baseline_threshold_only.py`; the gateway
code is unchanged, the rule checks are disabled in the harness only). Compared with the final pipeline on the same requests
(`threshold_0.97`):

| Cache | Answered from cache | Wrong hits | Wrong / hits |
| --- | ---: | ---: | ---: |
| Final pipeline (0.97, verifier, rules, eligibility) | 49.2% | 11 | 0.15% |
| Threshold-only 0.90 | 47.9% | 292 | 4.06% |
| Threshold-only 0.80 | 61.7% | 801 | 8.64% |

At 0.90, 199 of the 292 wrong hits were personal or time-sensitive questions that the eligibility filter would not cache.
No request failed.

## Honest limits

- **Synthetic workload.** Questions are authored FAQ intents and paraphrases. A wrong hit means the returned
  intent label differs from the asked one; answer factual quality is not checked.
- **Rules tuned on the same corpus.** Two rule checks were added after an earlier run exposed the confusions:
  payroll versus supplier-invoice payments, and having acted on versus reporting a suspicious message. Results
  for those intents are therefore optimistic. The two hand-labelled pair sets were also used for tuning.
- **Misspellings and close requests.** 76 of the 162 remaining wrong hits in `main` come from a misspelled keyword slipping past the rule
  checks; most of the rest confuse closely related requests (changing vs resetting a password, muting vs
  unmuting). Both are known limitations, not fixed.
- **Not an ablation.** Old versus new changes several policies together.
- **Money.** Prices in `prices.json` were retrieved 2026-11-10 and include unverified secondary sources;
  hosting and savings are modelled estimates, not cloud bills.
- **Models.** Exact embedding/verifier revision IDs were not exposed by the loaded models.

## Provenance and verification

[snapshot_manifest.json](snapshot_manifest.json) fingerprints the README, comparison files, the main and
threshold reports and their raw CSV/metadata pairs, plus the analysis sources. The `sens` folder is outside
the snapshot tool's fixed scope; its CSVs carry their own `.meta.json` fingerprints (CSV hash, code content
hash, harness hash). From the project root, with `$gatewayPython` set to the project venv interpreter:

```powershell
& $gatewayPython eval/cost/snapshot.py --check --root eval/cost/results/paper_v2
& $gatewayPython eval/cost/analyze.py --raw eval/cost/results/paper_v2/sens/raw --results eval/cost/results/paper_v2/sens
```

The check verifies artifact hashes and LF-normalized sources, not model weights, intent labels or prices.
