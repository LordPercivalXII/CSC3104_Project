# Smart AI Gateway: project status, handoff and review

## Follow-up fixes — 2026-11-15 (uncommitted)

A re-review of `e90bc6f` against this file found two remaining defects. Both are now fixed:

- **N3, time-sensitive over-triggers (was only partly fixed).** The 2026-11-13 cues "this morning",
  "on Monday", "last night" and a bare "now" flagged 80 of the 906 FAQ phrasings in
  `eval/cost/data/intents.json` (old code: 67). In the paper_v1 run they cost 730 correct hits.
  `cache_checks.time_sensitive()` now separates *state* cues ("current", "latest", "at the moment",
  "next fire drill"), which always bypass the cache, from *date* cues. A date cue in a first-person
  account ("I came in this morning and my laptop…") is treated as context unless the prompt asks
  about a shared schedule or state (open/closed, free, outage, menu, on duty, deadline, how many…).
  Bare "now" is filler; "right now" still counts. Result: 12 of 906 FAQ phrasings flagged, and all
  20 time-sensitive prompts are still caught. These rules were checked against the benchmark corpus,
  so rerun results on it are optimistic. Validate on held-out real questions.
- **A coalesced follower failed at the leader's deadline.** When the leader timed out, `maintain`,
  `take` and `complete` failed every follower, even ones that joined later with time left. A shared
  answer arriving after the leader's deadline was also thrown away. Now only the leader's caller
  receives the timeout (`caller_done`). The shared call keeps running until the latest follower
  deadline, and stops once the last follower leaves. Both backends: `redis_scripts.expire_leader`
  and `MemoryCoordinator._expire_leader`.

Tests: **293 passed, 103 subtests** (new cases in `test_scheduler_reliability.py` and
`test_review_cache_regressions.py`). The venv also needs `prometheus-client` (`pip install -r
requirements.txt`): `main.py` imports `demo.telemetry`.

Known and left as is: S6 paced limiter is opt-in (`LLM_RATE_LIMIT_MODE=paced`). `/ready` fails when
Redis is down, which also stops cache hits through the Service (documented in `k8s/README.md`).
`_result_loop` polls one `HGET` per pending job every 0.2 s.

## Evaluation handoff — 2026-11-15

The owner completed the main and four threshold experiments. The curated
[paper_v1 snapshot](eval/cost/results/paper_v1/README.md) now includes all 35 raw CSVs and their
metadata, per-experiment reports and a combined comparison. The raw records pass hash, seed/count
and paired-stream checks. The recorded application source is `6476b9a`; the old baseline is `2007299`.
Earlier implementation fixes are already in `6476b9a`. This new snapshot/analysis change is prepared
for review in GitHub Desktop; no commit or push has been made by the assistant.

The revised gateway reduced pooled wrong cache reuse from 1,146 to 169 out of 78,363 requests per
configuration. Thresholds 0.95, 0.97 and 0.99 returned identical cache outcomes on the sweep's paired
streams. Do not call one uniquely optimal. Threshold 0.90 has one preserved HTTP 504 after a prolonged
wall-time gap (seed 2, request index 303); its cause is unknown. Corrected `analyze.py` gives failed
delivery no beneficial avoided-call/token/cost credit and charges actual inference waste. Only derived
reports were regenerated; no raw record was changed or silently dropped.

`.gitignore` allows only the five final experiment directories and root comparison/manifest/README.
Pilot, old smoke/debug outputs and `eval/runs/` remain ignored. `.gitattributes` preserves snapshot
bytes so checksums survive Git checkout. Verify with `python eval/cost/snapshot.py --check` before
sharing. Exact model revisions remain unavailable, and pricing/hosting entries still require official
verification before monetary paper claims. See the snapshot README for the next research steps.

Validation after the accounting fix: **286 tests and 89 subtests passed**. The 99-file snapshot is about
112 MiB; the largest file is 7.35 MiB. The manifest checks all 98 non-manifest artifacts and all 35 raw
CSV/metadata pairs. Git's clean filter preserves raw CSV bytes, and the credential-pattern scan found
no matches. No live LLM calls, Kubernetes deployment, Git staging, commit or push was performed.

## Implementation handoff — 2026-11-13 (historical)

This section supersedes the historical status tables below. Work remains uncommitted on
`docker-k8s-deployment`; no deployment, live Gemini requests, authentication or tenant features were added.
The owner explicitly deferred identity and tenant isolation.

| Findings | Current implementation |
|---|---|
| N1 | Leave-category conflict checks, including maternity/paternity misspellings and annual/sick leave |
| N2–N3 | Broader temporal cues with narrow exceptions for stable procedures |
| N4 | Contextual manager/boss checks allow general procedures while bypassing individual decisions |
| S3, S7–S9 | Atomic Redis admission/coalescing and dequeue-to-lease; fenced completion, retained results, recovery limits, pending records without TTL |
| S4 | Whole-route deadline, disconnect cancellation, global/per-user admission limits; bounded shutdown |
| S5 | A queued shared call can use the earliest participant's round-robin turn |
| S6 | Sliding-window behavior retained as default; optional `LLM_RATE_LIMIT_MODE=paced` prevents initial bursts |
| S10 | Cancellation detaches followers and preserves a leader only while other callers need it |
| S11–S12 | Generic API errors, rate-limited failures return 429 with Retry-After; Redis submit/cancel run off the event loop |
| X2 | `/ready` checks the Qdrant collection and Redis; Kubernetes readiness uses it; `/health` remains liveness |
| X3 | Terminal in-memory job records are pruned; each worker still correctly uses one stable result queue |
| Evaluation | Portable frozen baseline, provenance and output hashes, offline imports, safe pytest collection, exact completion-window plotting |
| S1, S2, C10 | Deferred: caller identity is still supplied by the client; no company isolation |

Reliability is **at least once**, not exactly once: recovery after a lost provider outcome may repeat inference.
Redis itself must retain its non-expiring pending/lease state; this is not a guarantee against Redis data loss.
Old and new scheduler workers should not be mixed during a rollout: stop load, drain requests, then roll out.
`/scheduler/chat` now has the same input limits and deadline, but remains a development endpoint without auth.

Scheduler settings are environment variables: `SCHEDULER_REQUEST_TIMEOUT_SECONDS=120`,
`SCHEDULER_LEASE_SECONDS=30`, `SCHEDULER_MAX_ATTEMPTS=2`, `SCHEDULER_MAX_PENDING=1000`,
`SCHEDULER_MAX_PENDING_PER_USER=100`, `SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS=5`,
`REDIS_SOCKET_TIMEOUT_SECONDS=3`. Limits in Redis mode are shared across gateways; memory mode is per process.
Inference already running in a Python thread cannot be forcibly stopped; cancelled callers no longer receive its
result, while an uncancelled coalesced follower can still receive the shared answer.

The full offline suite currently passes **279 tests and 89 subtests**, using fake models, in-memory Qdrant
and fakeredis. New tests inject lost notifications, expired leases/status, late completion, cancellation,
queue saturation, provider errors, disconnected clients, provenance changes and plotting boundary cases.
No live Redis server or Kubernetes failure drill has been run for these fixes. Compose configurations,
Kubernetes kustomizations, Bash syntax and Python compilation also validate.

The end-to-end fixture smoke test completed six runs (no cache/old/new, two seeds), with 51 requests
per configuration and no HTTP errors. It used the real embedding/verifier models with artificial fixture
answers and prices. Fresh CSVs, provenance, tables and plots are under
`eval/cost/results/review_verified/` (git-ignored). Both cache versions reused 33 of the 51 fixture answers;
this small smoke test is not evidence of an accuracy improvement or real SME savings. Model commit IDs
were not exposed by the locally loaded objects and are explicitly marked unavailable in the metadata.

The historical cost/accuracy figures below describe older code, and the full raw outputs are absent here.
Do not cite them as measurements of these fixes. The new pipeline smoke run uses artificial fixture
recordings and prices; it cannot establish SME savings. Re-run the full main/sensitivity grids on the revised
code and label real employee data separately before drawing a research conclusion.

The live demo exercises the actual HTTP/cache/scheduler/distributed paths with mock inference by default.
The cost benchmark evaluates cache reuse against authored intent labels sequentially with virtual time.
Neither verifies that a Gemini answer is factually correct; neither substitutes for a real workload study.

Locust's default cache scenario now reads the canonical `eval/cost/data/intents.json` in both deployment
modes: 151 FAQ intents, 755 paraphrases, 25 personal templates and 20 temporal prompts. Its default input
shares match the evaluation (92% FAQ, 5% personal, 3% temporal; half the FAQ inputs use paraphrases),
with seeded Zipf popularity. The stdlib live sampler does not reproduce the offline NumPy stream,
company/day model or virtual clock. It does not add correctness scoring or replay the recorded answers.
Stress scenarios remain separate. Dataset identity and sampling settings are logged at Locust startup.
The pinned Locust image loaded the mounted data successfully with networking disabled; no live load or
cluster deployment was performed for this change. Apply the monitoring manifests and restart Locust to
use the revised corpus in an existing deployment (commands in `demo/README.md`).

---

## Historical handoff (retained for context)

*Last updated 2026-11-11. Base commit `2007299` on branch `docker-k8s-deployment`. **All work described here is uncommitted** in the working tree. Read this file first; it is written so a person or an AI model can continue the project without the original conversation.*

**Contents**
- [0. Read this first](#0-read-this-first): goal, constraints, status
- [1. What changed](#1-what-changed-since-commit-2007299)
- [2. How to run things](#2-how-to-run-things)
- [3. Results so far](#3-results-so-far): cache accuracy before vs after; cost, speed and quota
- [4. Next steps, prioritised](#4-next-steps-prioritised)
- [5. Pitfalls](#5-pitfalls-learned-the-hard-way)
- [6. Findings register](#6-findings-register): every review finding with its status
- [7. Test coverage](#7-test-coverage-now-and-gaps)
- [8. History](#8-history-of-this-work)

---

## 0. Read this first

### What the project is

A CSC3104 cloud computing module project. A FastAPI gateway sits between a company's employees and an LLM (Gemini `gemini-3.5-flash-lite`):

1. **Semantic cache (Qdrant).** Each prompt is embedded with `BAAI/bge-small-en-v1.5`. A stored question with cosine similarity of **0.97 or more** is served straight away. Matches between 0.75 and 0.97 are re-checked by a cross-encoder verifier (`cross-encoder/quora-roberta-base`, p ≥ 0.50). Every match must also pass rule checks for numbers, negation, opposite words and product names.
2. **On a miss,** the prompt goes through a **fair-share round-robin scheduler** (per `user_id`) to Gemini, and the answer is cached.
3. **Redis** (when `SCHEDULER_BACKEND=redis`) holds the scheduler queue shared by all pods, the cluster-wide LLM rate limiter, and the sharing of one LLM call between identical simultaneous questions. **Redis is not a prompt cache.** Exact and near-exact repeats are both answered by Qdrant.
4. It runs on Docker Compose or Kubernetes (`k8s/`).

### The owner's goals

- The module needs a **research article**. Its main question: *does a prompt cache like this save an SME money?* It must be backed by **actual statistics and results**, not estimates.
- The cache should be reliable. Wrong answers, where one employee is served an answer meant for a different question, are the costly error.

### Constraints: follow these

| Rule | Detail |
|---|---|
| **No commits without asking** | The owner asked for all changes to stay uncommitted. Don't commit, branch, stash or reset unless they ask. Read-only git is fine. |
| **Tenant scoping is deferred** | The owner chose not to implement multi-company isolation (finding C10) yet. Don't build it unasked; keep it listed as a limitation. |
| **Reviews report first** | When asked to *review*, report findings with file and line, and change code only when asked to fix. |
| **Article honesty** | Results must be measured and reproducible (`eval/cost/`). State assumptions and limitations. Don't present extrapolations as measurements. |
| **Secrets** | `GEMINI_API_KEY` lives in `.env`, which is git-ignored. Never print it or commit it. |

### Status at a glance

| Area | Status |
|---|---|
| Semantic cache fixes (C1-C9, C11-C15, X1) | ✅ Done and tested |
| Accuracy improvements (rule patch, 0.97 threshold, eligibility filter, `/feedback`, paraphrase storage, decision log) | ✅ Done and tested |
| Gemini token-usage capture and truncated-answer handling | ✅ Done |
| Cost and reliability benchmark (harness, dataset, real Gemini recordings, full results) | ✅ Done; see `eval/cost/RESULTS.md` |
| Test suite | ✅ **201 passed** (13 files in `tests/`) |
| Follow-ups found by the benchmark (maternity/paternity, time-sensitive detector) | ⏳ Open, section 4 |
| Scheduler findings S1-S12 | ⏳ Open; not worked on |
| Tenant scoping (C10) | ⏸ Deferred by the owner |
| Committing the work | ⏸ Waiting for the owner; decide what to commit from the 201 MB `eval/cost/results/` |

---

## 1. What changed since commit `2007299`

Run `git status` and `git diff --stat` for the exact list: 19 modified files and about 10 new paths. By area:

### 1.1 Rule checks: `smart_ai_gateway/services/cache_checks.py`

`conflict(prompt, cached_prompt)` rejects a cached match when a detail that changes the answer differs. `time_sensitive(prompt)` flags questions whose answer depends on when they're asked.

**New:** a `_normalise()` step that runs before every check. It:
- turns curly quotes into straight ones;
- turns hyphens between letters into spaces ("sign-in" becomes "sign in");
- canonicalises Wi-Fi, e-mail and 2FA/MFA/two-factor;
- maps "turn/switch … on/off" to enable/disable;
- reads the number words one to twelve as digits.

**Lists extended:**
- `_NAMES`: product, platform, weekday and month names, treated like identifiers.
- `_OPPOSITES`: 12 new pairs.
- `_NEGATIONS`: "no", "none", "nothing" and similar.
- `_TIME_WORDS`: "current", "recent", "recently" and "newest".

**Removed:** the bare on/off opposite pair, which misfired on the preposition "on".

**Number order:** for short prompts (`_SHORT_PROMPT_TOKENS = 12`), numbers are compared as an ordered list, so "move 5 to 10" conflicts with "move 10 to 5".

**Lock vs unlock:** `_LOCKED_STATE` and `_without_locked_state()` let "my account is locked" match "unlock my account". "lock my screen" vs "unlock my screen" still conflicts.

**Plurals:** `_stem()`, so "VPN" and "VPNs" match.

### 1.2 Cache service: `smart_ai_gateway/services/semantic_cache.py`

**Eligibility filter:** `cache_skip_reason()` decides whether a prompt is neither searched nor stored, checking in this order:
1. time-sensitive;
2. longer than `cache_max_prompt_chars` (300);
3. a code fence;
4. more than `cache_max_prompt_lines` (3) lines;
5. personal data (`_personal_data`: emails, phone numbers, IDs, "I'm <Name>");
6. `personal_phrases` (`_personal_phrase`);
7. the token limit.

**Thread safety:** `_embedding_lock` and `_verifier_lock` around model calls (C14).

**Decision details:** `select_match_detailed()` and `search_cache_detailed()` return `(hit, details)` for every lookup, misses included. `select_match()` and `search_cache()` keep their old return shapes.

**Paraphrase storage:** `store_paraphrase()`. After a verifier-confirmed hit, the new phrasing is stored as an extra point:
- its payload has `paraphrase_of` = the original point id;
- it keeps the original's `created_at_ts`, so it can't outlive `max_age_seconds`;
- at most `max_paraphrases` per answer, skipped at ≥ 0.99 similarity to an existing point.

**Feedback:** `report_wrong_match()` deletes an entry and all its paraphrases, then appends the pair to `feedback_path`.

**Qdrant:** the client gets a `qdrant_timeout_seconds` timeout.

### 1.3 Aging and sweeping: `cache_aging.py`, `scripts/sweep_expired_cache.py`, `services/clear_semantic_cache.py`

- **Grace period:** `_other_models()` deletes entries from another embedding model or LLM only after `other_model_grace_seconds` (7 days) without a hit. `--purge-other-models` on the sweep script skips the grace period (C15).
- **Clear script:** `clear_semantic_cache.py` no longer deletes on import. It needs `--yes`, or typing "yes".

### 1.4 API: `main.py`

**`POST /chat`:**
- A Qdrant error is treated as a miss and the LLM answers; the stage is logged as `cache_error` (C8).
- Storing runs as a background task (`_store_in_background`), and only when the scheduler result says `cacheable` (C9).
- Cache-hit responses now include `point_id`. Miss responses include `usage` (token counts).
- Request limits come from config (X1).

**`POST /feedback`** takes `{"point_id", "prompt", "reason"?}`. It returns:
- **200** with the number of deleted points;
- **404** for an unknown or invalid id;
- **503** when Qdrant is unreachable.

**Decision log:** `_log_decision` writes one JSON line per `/chat` request to `decision_log_path`. The prompt text is not logged.
- **Fields:** stage, scores, rule reason, point_id, source, coalesced, cacheable, stored, usage, and per-phase latency.
- **Stages:** `skipped`, `direct_hit`, `verified_hit`, `rule_rejected`, `verifier_rejected`, `below_threshold`, `miss`, `cache_error`.

**`static/script.js`:** a "Wrong answer?" link on cache-hit answers calls `/feedback`.

### 1.5 LLM and scheduler: `smart_ai_gateway/services/llm.py`, `scheduler/scheduler_worker.py`

**`generate_answer()` returns an `LLMAnswer`** with `text`, `cacheable`, `finish_reason` and `usage`.
- `usage` holds `prompt_tokens`, `output_tokens`, `thinking_tokens`, `total_tokens`, `model` and `estimated`.
- `cacheable` is true only when the finish reason is STOP. A truncated (MAX_TOKENS) or safety-stopped answer is still returned to the user, but isn't cached.
- The mock provider returns estimated usage.

**Scheduler:** the result dict gains `cacheable`, `usage` and `finish_reason`. These are JSON-safe for Redis mode, and coalesced followers receive them too. A plain-string inference function still works.

### 1.6 Config: `smart_ai_gateway/gateway.config`, `smart_ai_gateway/config.py`

`similarity_threshold` is now **0.97** (it was 0.95). New keys, each overridable as `GW_<SECTION>_<KEY>`:

| Section | Key | Default |
|---|---|---|
| `[semantic_cache]` | `cache_max_prompt_chars` | 300 |
| | `cache_max_prompt_lines` | 3 |
| | `skip_personal_data` | true |
| | `personal_phrases` | (list) |
| | `qdrant_timeout_seconds` | 3 |
| | `max_paraphrases` | 5 |
| | `paraphrase_duplicate_similarity` | 0.99 |
| | `feedback_path` | `data/feedback.jsonl` |
| | `other_model_grace_seconds` | 604800 |
| `[gateway]` | `decision_log_enabled` | true |
| | `decision_log_path` | `data/decisions.jsonl` |

Relative paths resolve from the project root.

### 1.7 Tests and housekeeping

- **New test files:** `tests/test_chat_api.py` (14), `tests/test_cache_features.py` (38) and `tests/test_llm_usage.py` (12). `tests/test_cache_matching.py` went from 23 to 34 tests.
- **Changed tests:** `tests/test_semantic_cache_aging.py` sets `other_model_grace_seconds=0`, and `tests/test_llm.py` reads `.text`.
- **`pytest.ini`** (`testpaths = tests`). `smart_ai_gateway/scripts/test_semantic_cache.py` was renamed to `try_semantic_cache.py` (it needs a live Qdrant), so plain `pytest` works again.
- **Evaluation pairs:** `smart_ai_gateway/scripts/cache_matching_pairs.py` gained a second labelled set (`HELD_OUT_PAIRS`: 25 same and 41 different). `evaluate_cache_matching.py` has `--set original|extra|all`. Thresholds were chosen on both sets, so neither is a clean test set; the benchmark workload is.
- **`.gitignore`** now ignores `/data/`: only the repo-root folder, which holds the runtime decision and feedback logs. It is anchored on purpose. An unanchored `data/` would also hide `eval/cost/data/`, the benchmark inputs the article needs. Check with `git check-ignore -v eval/cost/data/intents.json`, which should print nothing.
- **Test count:** the 201 tests are 98 in the new or changed files listed above, plus 103 in the existing files. That makes 13 files in `tests/` with `test_` functions; six more files there are manual scripts (section 7).

### 1.8 Benchmark: `eval/cost/` (all new)

| File | Purpose |
|---|---|
| `README.md` | How to run each step, the method, and threats to validity |
| `RESULTS.md` | **Article-ready results**: findings, tables, statistics, limitations |
| `workload.py` | Seeded synthetic SME traffic: Poisson volume, Zipf popularity, paraphrases, personal and time-sensitive prompts. Its docstring lists the assumptions |
| `replay.py` | Replay LLM: answers misses from real recorded Gemini answers and tokens, and tags each answer with its intent so hits can be scored |
| `record_real_answers.py` | Calls real Gemini once per intent with the gateway's settings, reads the key from `.env`, throttled and resumable |
| `bench.py` | Runs `nocache` / `old` / `new` through the real `/chat` with in-memory Qdrant. `old` runs from a `git archive` of `2007299` in a subprocess |
| `analyze.py`, `extras.py` | Mean ± 95% CI over seeds, economics, plots, wrong examples, stage breakdown |
| `prices.json` | LLM and hosting prices with sources, retrieved 2026-11-10 |
| `data/intents.json` | 151 labelled intents × 5 paraphrases, including 46 confusable groups, plus personal and time-sensitive templates |
| `data/real_answers.jsonl` | 159 real Gemini answers with token counts and latency |
| `fixtures/` | Tiny fake inputs for the 30-second smoke test (`README.md`) |
| `results/main/`, `results/sens/` | Analysed results: `summary.md`, `summary.csv`, `economics.csv` and plots, for the main and sensitivity grids |
| `results/wrong_examples.csv`, `wrong_summary.md` | Every wrong cache hit, old and new, with its scores |
| `results/stage_breakdown.csv`, `.md` | How requests were decided (new code), and which filter rule skipped which FAQ |
| `results/raw_main/`, `raw_sens/`, `decisions/`, `logs/` | Per-request raw CSVs, gateway decision logs, run logs: most of the **201 MB** |

---

## 2. How to run things

Windows, Git Bash, from `D:\CSC3104_Project`. Neither Docker nor a Qdrant server is needed for any of these.

```bash
PY=.venv/Scripts/python                 # venv with all deps (torch CPU); recreate: python -m venv .venv && $PY -m pip install -r requirements.txt -r requirements-dev.txt
export HF_HUB_OFFLINE=1 PYTHONIOENCODING=utf-8 GW_LLM_PROVIDER=mock   # models are cached; mock LLM for tests

$PY -m pytest -q                                                  # expect: 201 passed
$PY smart_ai_gateway/scripts/evaluate_cache_matching.py           # expect: 0 wrong on both sets; hits 17/32 and 23/25
$PY -m uvicorn main:app --reload                                  # run the app (unset GW_LLM_PROVIDER and set GEMINI_API_KEY for real answers)
```

To reproduce the benchmark, see `eval/cost/README.md` for the full commands. Unset `GW_LLM_PROVIDER` only for `record_real_answers.py`. In short:

```bash
# Cheap sanity check first: the ~30 s fixture smoke test described in eval/cost/README.md
$PY eval/cost/record_real_answers.py --extra 8        # real Gemini; ~20 min at 8 calls/min; already done, resumable
$PY eval/cost/bench.py --configs nocache,old,new --seeds 1-5 --sizes 10,50,200 --days 20 --out eval/cost/results/raw_main --decision-log-dir eval/cost/results/decisions/main
$PY eval/cost/analyze.py --help                       # analyze raw_main into results/main and raw_sens into results/sens; see the README for the exact arguments
$PY eval/cost/extras.py --help                        # builds wrong_examples.csv and stage_breakdown.*
```

- **Time:** about 25 min per (config, seed) for the main grid.
- **Memory:** run **at most 5 processes at once**. Each loads about 1.2 GB of models, and 10 at once crashed with MemoryError.
- **Old code:** the "old" config needs a `git archive 2007299` extract. Pass it with `--old-root <path>`. The default in `bench.py` (`DEFAULT_OLD`) is a temporary session folder (`…\scratchpad\old_code`) that will eventually be cleaned up, so recreate it, outside the repo, with `mkdir ../old_2007299 && git archive 2007299 | tar -x -C ../old_2007299`.

---

## 3. Results so far

Full details: `eval/cost/RESULTS.md`. "Old" means the code at commit `2007299`; "new" means the current working tree.

### 3.1 Cache accuracy: before vs after

**Labelled test pairs.** Each pair is two questions labelled either *same* (the cache should serve the stored answer) or *different* (it must not). **Hits** = same pairs served from the cache, so higher is better. **Wrong** = different pairs served from the cache, so lower is better; this is the costly error. Run `evaluate_cache_matching.py` to reproduce.

| Version | Original set: hits | Original set: wrong | Extra set: hits | Extra set: wrong |
|---|---|---|---|---|
| Original prototype (MiniLM, 0.90, no rule checks) | 3 / 32 | 1 / 26 | – | – |
| **Old** (`2007299`: bge-small, 0.95, verifier, rules) | 15 / 32 | 0 / 26 | 19 / 25 | **2 / 41** |
| **New** (rule fixes, 0.97 threshold) | **17 / 32** | **0 / 26** | **23 / 25** | **0 / 41** |
| Change, old → new | +2 hits | – | +4 hits | **−2 wrong** |

- **Original set:** 32 same and 26 different pairs, in `cache_matching_pairs.py`.
- **Extra set:** 25 same and 41 different, written in the review to target known weaknesses; mostly hard negatives.
- Both sets were used to choose the new settings, so treat these numbers as a regression check. The independent measure is the benchmark below.

**The two wrong answers the old version served on the extra set:**
1. "send a calendar invite in Outlook" was matched to "accept a calendar invite in Outlook" (similarity 0.952, a direct hit). The new 0.97 threshold sends it to the verifier, which rejects it.
2. "can I take unpaid leave" was matched to "paid leave" (verifier p = 0.73). The new paid/unpaid opposite rule rejects it.

**Specific rule-check failures, old vs new.** Each row was run through `conflict()`:

| Question pair | Old | New |
|---|---|---|
| "turn **off** notifications on my phone" vs "turn **on** …" | ❌ matched | ✅ rejected (enable / disable) |
| "**upgrade** to Windows 11" vs "**downgrade** …" | ❌ matched | ✅ rejected |
| "reset my **Outlook** password" vs "… **Teams** password" | ❌ matched | ✅ rejected (product name) |
| "I **can’t** access the shared drive" (curly apostrophe) vs "I can access …" | ❌ matched | ✅ rejected (negation) |
| "I have **no** access …" vs "I have access …" | ❌ matched | ✅ rejected (negation) |
| "how do I **sign-in**" vs "how do I **sign out**" | ❌ matched | ✅ rejected |
| "move **5 to 10**" vs "move **10 to 5**" | ❌ matched | ✅ rejected (number order) |
| "turn on **2FA**" vs "enable **two factor** authentication" | ❌ wrongly rejected | ✅ matched |
| "connect to the **VPN**" vs "connect to **VPNs**" | ❌ wrongly rejected | ✅ matched |
| "my account is **locked**" vs "**unlock** my account" | ❌ wrongly rejected | ✅ matched |
| "who is the **current** IT manager" (time-sensitive?) | ❌ not flagged | ✅ flagged, never cached |

**Benchmark: realistic traffic, not used for tuning.** 78,363 requests per version in total: 5 seeds, each covering companies of 10, 50 and 200 staff over 20 working days.

| | 10 staff | 50 staff | 200 staff |
|---|---|---|---|
| Hit rate, old → new | 24.8 → 23.9% | 47.3 → 46.4% | 66.7 → 65.6% |
| Wrong answers per 100 hits, old → new | 1.05 → 1.06 | 1.48 → 1.27 | 2.54 → **1.85** |

| Wrong answers, all sizes | Old | New | Change |
|---|---|---|---|
| FAQ question served another question's answer | 696 | **378** | **−46%** |
| … of which on a look-alike ("confusable") question | 669 | 342 | −49% |
| Time-sensitive question served from the cache | 450 | 454 | no change (next step 2) |
| Personal question served from the cache | 0 | 0 | – |

- **Trade-off:** the new version gives up about 1 percentage point of hit rate (0.8-1.2 pp) in exchange for 46% fewer wrong FAQ answers.
- **Significance:** the reduction is significant at 50 staff (paired t = −2.87, df = 4, p ≈ 0.045). At 200 staff it points the same way but isn't significant (t = −1.67), because one seed dominates.
- **What remains:** two thirds of the new version's wrong FAQ answers (254 of 378) are one pair, **maternity vs paternity leave** (next step 1).
- **Old version's errors:** almost all of them (662 of 696) came through the verifier on look-alike questions.

### 3.2 Cost, speed and quota

| | 10 staff | 50 staff | 200 staff |
|---|---|---|---|
| Questions answered from the cache | 23.9% | 46.4% | 65.6% |
| Saved per 1,000 requests, Flash-Lite | $0.07 | $0.13 | $0.18 |
| Saved per 1,000 requests, Gemini 3.1 Pro | $0.33 | $0.63 | $0.89 |
| Cached answer, median latency | 47 ms | 62 ms | 89 ms |
| LLM call, median latency | about 3.4 s | about 3.4 s | about 3.8 s |

- **A dedicated deployment doesn't pay off at SME scale.** One AWS t3.medium node costs $38.54/month. It breaks even at about **220k requests/month with Flash-Lite** (roughly 3,300 staff), or about **44-51k requests/month with premium models** (roughly 660-770 staff).
- **The marginal cost is negligible.** Gateway CPU costs $0.0004-0.0015 per 1,000 requests, so co-hosted on existing infrastructure the cache saves money from the first request.
- **Quota.** At the 200-staff hit rate of 65.6%, a fixed free-tier quota serves about **2.9 times as many employees**.

---

## 4. Next steps, prioritised

| # | Task | Size | Status |
|---|---|---|---|
| 1 | Add maternity/paternity and other sibling-term rules | Small | ⏳ |
| 2 | Fix the time-sensitive detector (misses and over-triggers) | Medium | ⏳ |
| 3 | Re-run the benchmark after 1 and 2; update `RESULTS.md` | ~70 min, mostly unattended | ⏳ |
| 4 | Ask the owner what to commit | Small | ⏸ Owner |
| 5 | Normalise mixed line endings | Tiny | ⏳ |
| 6 | Trim `personal_phrases` ("my manager" over-skips) | Small | ⏳ |
| 7 | Scheduler findings S1-S12 | Medium-Large | ⏳ Confirm scope first |
| 8 | Tenant scoping (C10) | Large | ⏸ Only if the owner asks |
| 9 | Calibrate thresholds on real traffic | Medium | ⏳ |
| 10 | Domain-tuned verifier | High | ⏳ |
| 11 | Hybrid dense + keyword (BM25) search | Medium-High | ⏳ Do last |

**Details**

1. **Sibling terms.** Add `maternity`/`paternity` (and similar pairs: `annual`/`sick`, and phrasings like "take me off" for remove) to `_OPPOSITES` or `_NAMES` in `cache_checks.py`, with tests. *Why:* this removes about two thirds of the remaining wrong FAQ answers (finding N1).
2. **Time-sensitive detector.** In `cache_checks.py`, update `_TIME_WORDS`, `_TIME_PHRASE` and `time_sensitive()`.
   - *Misses:* "at the moment"; "next <event>" ("next fire drill", "next all-hands"); "this Saturday/afternoon"; "on Sunday"; "is X down/open right now".
   - *Over-triggers:* "now that…"; "into next year"; "current password"; "this month's payslip". Together these skip 7.25% of FAQ requests.
   - *Test against:* the `time_sensitive` list in `eval/cost/data/intents.json`, and the FAQ skips in `eval/cost/results/stage_breakdown.md` (findings N2, N3).
3. **Re-run the benchmark.** The current numbers are for the code before fixes 1 and 2. Commands are in section 2; keep at most 5 processes at once.
4. **What to commit.**
   - *Suggested:* the code, tests, `eval/cost/*.py`, `README.md`, `RESULTS.md`, `eval/cost/data/`, `prices.json`, `fixtures/`, `results/main`, `results/sens` and the summary files.
   - *Leave out or compress:* `results/raw_*` and `results/decisions/`, which are most of the 201 MB.
   - *Before committing:* check that `git status --ignored` doesn't hide anything you mean to commit.
5. **Line endings.** `main.py`, `scripts/sweep_expired_cache.py` and `services/clear_semantic_cache.py` contain some LF lines. `.gitattributes` normalises them on commit, so this is cosmetic.
6. **`personal_phrases`.** "my manager" skips general questions such as "how do I ask my manager for leave" (0.66% of FAQ requests; finding N4). That's a safe miss, but it costs hits.
7. **Scheduler.** S3 (jobs lost or hanging forever) and S4 (no end-to-end timeout) are the most serious. S1 (forgeable `user_id`) needs authentication. Not requested yet.
8. **Tenant scoping.** Needed for the multi-company SaaS idea. The cost case also improves with shared infrastructure (`RESULTS.md` §2.3). Pairs with S1 and S2.
9. **Calibrate on real traffic.** The decision log records similarity, verifier score and stage per request. Collect real employee traffic, label a sample, and re-tune `similarity_threshold`, `verify_threshold` and `verifier_threshold`. *Why:* every evaluation set so far is hand-written, which is the biggest unknown in the article.
10. **Domain-tuned verifier.** Fine-tune the cross-encoder on helpdesk pairs (the `/feedback` log and `cache_matching_pairs.py` are training data), or use a small LLM as a judge only in the 0.75-0.97 grey zone. *Why:* the Quora model limits hits. It is cautious on short paraphrases ("payslip" vs "salary statement", p = 0.00), yet it still matched "maternty leave" to paternity (p = 0.91).
11. **Hybrid search.** Qdrant sparse vectors lower the similarity of minimal pairs, but this mostly duplicates the rule layer.

---

## 5. Pitfalls learned the hard way

- **Line numbers.** Files use **CRLF** line endings, and some tools miscount lines. Two reviewers cited wrong line numbers. Use `grep -n` or `cat -n`, and prefer function names over line numbers.
- **Hugging Face.** Set `HF_HUB_OFFLINE=1`. The models are in `~/.cache/huggingface`, and without it every start makes network checks. Windows shows harmless symlink warnings.
- **Memory.** On the 12-core laptop, at most about 5 parallel processes can each load the models.
- **Decision log.** It writes to `data/decisions.jsonl` by default. Benchmarks and tests must redirect it (`bench.py --decision-log-dir`; the tests use temporary folders) to keep the repo clean.
- **Gemini free tier.** 8 calls/min for 159 calls hit no rate limits. Free-tier prompts may be used by Google, so don't send real employee data.
- **Antigravity (`agy`), for delegating to Gemini.** Run it from an **empty** working folder, not from inside the repo. Inside a code project it may try shell commands, which headless mode denies. Pass the prompt file's folder with `--add-dir`.

---

## 6. Findings register

These are the original review findings from 2026-11-10, with their current status: **✅ Fixed**, **⏳ Open**, **⏸ Deferred**. Locations are given as function or constant names, because line numbers drift. Find them with `grep -n`.

### 6.1 Scheduler, queue and rate limiter: none worked on yet

**Verdict from the review:** the round robin is correct in both backends, the Lua scripts are atomic, the rate limiter is a true sliding window on Redis's clock, and the eval numbers match the code. The gaps are in failure handling and in multi-tenant fairness.

Each open finding keeps the full detail from the original review, so whoever picks it up can reproduce it. "Checked" means: *ran it* (reproduced by running code), *read it* (confirmed by reading the code), or *reported* (one reviewer, not independently re-checked).

| ID | Severity | Issue | Status |
|---|---|---|---|
| S1 | High | `user_id` can be forged | ⏳ Open |
| S2 | High (SMEs) | Fairness isn't per company | ⏳ Open |
| S3 | High | Jobs can be lost; callers hang forever | ⏳ Open |
| S4 | Med | No end-to-end timeout | ⏳ Open |
| S5 | Med | Coalescing skips the follower's fair turn | ⏳ Open |
| S6 | Med | Rate limiter allows a full burst | ⏳ Open |
| S7 | Med | `submit` isn't atomic | ⏳ Open |
| S8 | Low-Med | Job status can expire while queued | ⏳ Open |
| S9 | Low | Coalescing stops on long backlogs | ⏳ Open |
| S10 | Low | Cancelled followers linger | ⏳ Open |
| S11 | Low | Rate limit returns 503, leaks error text | ⏳ Open |
| S12 | Low | Blocking Redis calls on the event loop | ⏳ Open |

#### Details

**S1: `user_id` can be forged** (High, checked: read it)
- **Where:** `main.py` `ChatRequest`, `chat`, `scheduler_chat`
- **Problem:** `user_id` comes from the request body with no authentication. `/scheduler/chat` has no validation at all.
- **Failure scenario:** A heavy user sends each prompt under a new id (`bob1`, `bob2`…). Each id gets its own rotation slot, so this user takes most LLM turns and fairness is defeated.
- **Fix:** Take identity (employee and company) from an auth token or API key and ignore the body's `user_id`. Disable `/scheduler/chat` outside development.

**S2: Fairness isn't per company** (High (SMEs), checked: read it)
- **Where:** `scheduler/fair_scheduler.py`; `scheduler/redis_backend.py` ENQUEUE/DEQUEUE scripts
- **Problem:** A flat round robin over `user_id` across **all** companies. Ids from different companies collide.
- **Failure scenario:** Company X has 50 active employees and company Y has 2: X gets about 96% of LLM turns. Two employees called "alice" at different companies share one queue.
- **Fix:** Two-level round robin: rotate over companies, then over users within each company (`rotation`, `rotation:<tenant>`, `queue:<tenant>:<user>`). Optionally weight companies by plan or seats. Linked to C10.

**S3: Jobs can be lost; callers hang forever** (High, checked: read it)
- **Where:** `redis_backend.py` DEQUEUE (`LPOP`); `scheduler_worker.py` `_process_next`; `main.py` `wait_for_llm`
- **Problem:** No lease or visibility timeout. A job is popped off the queue for good before it's processed, and `wait_for_llm` waits with no timeout.
- **Failure scenario:** (a) A pod is killed during a Gemini call (up to about 60 s with retry, while pods get 30 s to shut down), so the job is lost. (b) `publish_result` raises on a Redis blip, and `_process_loop` swallows the error. Either way, the caller (often on another pod) and any coalesced followers **hang forever**.
- **Fix:** Move the job into a `processing` sorted set with a deadline, in the same Lua script as the dequeue. A reaper puts expired jobs back on the queue. Make the publish idempotent. Wrap the wait in `asyncio.wait_for(..., timeout)` and return 504.

**S4: No end-to-end timeout** (Med, checked: read it)
- **Where:** `main.py` `wait_for_llm`, `chat`
- **Problem:** No deadline, and no cancellation when the client disconnects.
- **Failure scenario:** With a 10/min limit, a 200-request backlog takes 20 min. Clients give up, but every abandoned request still spends a Gemini call.
- **Fix:** `asyncio.wait_for` plus `future.cancel()` on timeout. Optionally poll `request.is_disconnected()`. Cap the queue per user or company and return 429 when it's full.

**S5: Coalescing skips the follower's fair turn** (Med, checked: ran it)
- **Where:** `scheduler_worker.py` `submit` (dedup / `join_or_lead`)
- **Problem:** A coalesced follower joins the leader as soon as the leader is submitted, while the leader may still be deep in **its owner's** queue.
- **Failure scenario:** User A queues a0-a4, then "shared q". User B asks "shared q" and user C asks "own q". Reproduced LLM order: `a0, own q, a1, a2, a3, a4, shared q`. C is answered at 0.09 s; B waits behind all of A's backlog (0.34 s).
- **Fix:** Only join leaders that are already PROCESSING. Alternatively, when a follower joins, put a "promote" marker in the follower's own queue so the shared call runs at the earliest member's turn.

**S6: Rate limiter allows a full burst** (Med, checked: read it; matches the eval numbers)
- **Where:** `scheduler/rate_limiter.py`; `redis_backend.py` RATE_LIMIT script
- **Problem:** The sliding window allows a burst of the full limit. Round robin can only reorder requests that haven't been sent yet.
- **Failure scenario:** In experiment E1, the heavy user's 18 requests use all 9 slots in the first few seconds. Light users arriving 5 s later wait until the window frees up: the measured **54 s** in `eval/results/summary.md`.
- **Fix:** Space calls out evenly with a token bucket or GCRA (one slot every 60/limit seconds, small burst). Light users would then wait seconds, not a minute.

**S7: `submit` isn't atomic** (Med, checked: read it)
- **Where:** `scheduler_worker.py` `submit`
- **Problem:** `create`, `join_or_lead` and `add_request` are separate Redis calls.
- **Failure scenario:** If `add_request` fails, the caller gets a 500, but the in-flight key stays for 300 s. Every identical question in that time joins a leader that was never queued and hangs.
- **Fix:** Do all three in one Lua script, or clean up (`finish`) in an `except` block.

**S8: Job status can expire while queued** (Low-Med, checked: read it)
- **Where:** `redis_backend.py` `JOB_TTL_SECONDS`; `k8s/redis.yaml` (`volatile-lru`); `scheduler_worker.py` `claim` call
- **Problem:** Job status hashes have a 1 h TTL and can be evicted under `volatile-lru`, while queue entries never expire.
- **Failure scenario:** A job queued for more than 1 h (backlog over 600 at 10/min), or whose hash was evicted, fails `claim()` and is silently dropped. The caller hangs (S3).
- **Fix:** Refresh the TTL on dequeue. Treat a missing hash as an error and publish it to the caller.

**S9: Coalescing stops on long backlogs** (Low, checked: reported)
- **Where:** `redis_backend.py` `INFLIGHT_TTL_SECONDS`
- **Problem:** The in-flight TTL (300 s) starts at submit, not when the job starts.
- **Failure scenario:** When the backlog is over 5 min, identical questions silently stop being coalesced. This causes extra LLM calls, not wrong answers.
- **Fix:** Refresh the TTL when the leader is claimed.

**S10: Cancelled followers linger** (Low, checked: reported)
- **Where:** `scheduler_worker.py` `_on_future_done`, `_answer_waiters`
- **Problem:** A follower that cancels stays in the waiters list, and its status is later overwritten from CANCELLED to COMPLETED.
- **Failure scenario:** Wrong statuses; the leader still calls the LLM even if every follower has left.
- **Fix:** Remove the waiter on cancel and check its status before overwriting.

**S11: Rate limit returns 503, leaks error text** (Low, checked: read it)
- **Where:** `main.py` `wait_for_llm`
- **Problem:** A Gemini rate limit becomes **503** with `Retry-After: 60`, not 429. The provider's raw error text is returned to employees.
- **Failure scenario:** Clients can't tell overload from an outage. Internal error text leaks to end users.
- **Fix:** Return 429 when `rate_limited`, otherwise 502/503 with a generic message. Log the detail.

**S12: Blocking Redis calls on the event loop** (Low, checked: read it)
- **Where:** `main.py` `chat` → `scheduler_worker.submit`
- **Problem:** `submit` makes about 3 blocking Redis round-trips on the event loop.
- **Failure scenario:** Redis latency stalls every request on that pod.
- **Fix:** `await run_in_threadpool(scheduler_worker.submit, ...)` or use `redis.asyncio`.

**Checked and sound in the scheduler:** FIFO order within a user's queue; no starvation for honest users (the worst wait is "active users − 1" turns); a claim and a cancel can't both win; on leader failure every follower gets the error promptly; failed answers are never cached; results are routed back to the right pod; Redis uses AOF and a volume, so queued jobs survive a Redis restart.

### 6.2 Semantic cache and LLM path

| ID | Sev | Issue (short) | Status, and where the fix is (current code) |
|---|---|---|---|
| C1 | High | On/off rule defeated by the preposition "on" | ✅ `cache_checks._normalise` (`_TURN` maps to enable/disable); bare on/off removed |
| C2 | High | Missing opposites (upgrade/downgrade, paid/unpaid…) | ✅ `_OPPOSITES` extended; `_IRREGULAR_FORMS` (sent) |
| C3 | High | Plain product names not checked (Outlook vs Teams) | ✅ `_NAMES` list in `_is_identifier` |
| C4 | Med | Curly apostrophe breaks negation | ✅ `_normalise` |
| C5 | Med | Missing negations ("no", "none"…) | ✅ `_NEGATIONS` |
| C6 | Med | Hyphenated sign-in/log-in | ✅ `_normalise` (hyphen to space) |
| C7 | Med | Time-sensitive words incomplete | ✅ Partly: "current/recent/newest" added. ⏳ The benchmark found more gaps and false positives; see next step 2 |
| C8 | Med | A Qdrant failure gives 500, and a store failure loses the answer | ✅ `main.py` `chat` falls back to the LLM; `_store_in_background`; `qdrant_timeout_seconds` |
| C9 | Med | Truncated answers cached | ✅ `llm.LLMAnswer.cacheable`; `/chat` stores only if cacheable |
| C10 | Med (High for multi-company) | No tenant scoping; one company's answers can reach another | ⏸ **Deferred by the owner**. Documented limitation |
| C11 | Med | Personal or pasted prompts cached and shared | ✅ `semantic_cache.cache_skip_reason` eligibility filter. ⏳ It over-skips some FAQs (next step 6) |
| C12 | Low | Numbers compared as sets | ✅ Ordered comparison for short prompts; number words read as digits |
| C13 | Low | False misses (2FA vs two-factor, VPN vs VPNs, locked vs unlock) | ✅ `_normalise`, `_stem`, `_without_locked_state`. A known remaining false miss: "one drive" vs "OneDrive" |
| C14 | Low | Model objects called from several threads | ✅ `_embedding_lock` and `_verifier_lock` |
| C15 | Low | Sweeper wipes other-model entries immediately; clear script deletes on import | ✅ `cache_aging._other_models` grace period; `--purge-other-models`; `clear_semantic_cache.py --yes` |
| X1 | Low | Configured request limits unused | ✅ `ChatRequest` reads `settings.gateway` |
| X2 | Low | `/health` always reports healthy, and both probes use it | ⏳ Open: make readiness check Redis and Qdrant |
| X3 | Low | In-memory backend result queues are never removed | ⏳ Open, unverified (`scheduler/job_store.py`) |

#### Details for the cache-side items not yet fixed

**C10: no tenant scoping** (deferred by the owner; checked: read it)
- **Where:** `main.py` `chat`; `semantic_cache.py` `search_cache_detailed` and `store_cache` (no tenant field in the payload or the filter); `scheduler_worker.py` `_dedup_key` (the coalescing key is the prompt only).
- **Failure scenario:** company A's employee asks "At Acme our leave policy is 20 days, how many can I carry over?". Company B's similar question can be answered with Acme's details. Identical prompts from two companies can also share one in-flight LLM call.
- **Fix:** add `tenant_id` (from auth, not the request body) to the payload, with a keyword index and a mandatory `must` filter on every search, or use one collection per tenant. Include the tenant in the coalescing key. Pairs with S1 and S2.

**X2: `/health` always healthy** (checked: read it)
- **Where:** `main.py` `health`; the readiness and liveness probes in `k8s/gateway.yaml`.
- **Failure scenario:** Kubernetes keeps routing traffic to a pod whose Redis or Qdrant connection is down.
- **Fix:** keep liveness light. Make readiness check that Redis (`PING`) and Qdrant (`collection_exists`) are reachable.

**X3: in-memory result queues never removed** (checked: reported, not re-checked)
- **Where:** `scheduler/job_store.py`, the `MemoryJobStore` results.
- **Failure scenario:** one result queue per `reply_to` is never removed, so memory grows slowly in long-running memory-mode pods.
- **Fix:** remove the queue once it's empty after consumption.

**C7 and C11 remainders:** see N2-N4 below and next steps 2 and 6.

### 6.3 New issues found by the benchmark (2026-11-10)

| ID | Issue | Evidence | Status |
|---|---|---|---|
| N1 | Maternity ↔ paternity leave matched | 254 of 378 wrong FAQ hits (`results/wrong_examples.csv`) | ⏳ Next step 1 |
| N2 | Time-sensitive phrasings missed | 454 wrong time-sensitive hits: "Is the VPN down at the moment?", "When is the next fire drill?" | ⏳ Next step 2 |
| N3 | Time words over-trigger on FAQs | 7.25% of FAQ requests skipped: "now that…", "into next year", "current password" | ⏳ Next step 2 |
| N4 | The `personal_phrases` entry "my manager" skips general questions | 0.66% of FAQ requests | ⏳ Next step 6 |
| N5 | Paraphrase cap can be exceeded by one under concurrency | Accepted, the same kind of race as hit_count | Accepted |

---

## 7. Test coverage now, and gaps

**Covered now:**
- every rule-check example for C1-C7, C12 and C13, in both directions (`tests/test_cache_matching.py`, `test_c1`…);
- `/chat` behaviour: hit, miss, store, Qdrant down, store failure, not-cacheable answers, skips, 422, feedback, decision log;
- the eligibility filter, paraphrase storage and its cap, the sweeper grace period, the clear script;
- LLM usage and finish reasons, and the scheduler pass-through.

**Still missing:**
- Scheduler failure paths: a crash mid-job, `publish_result` raising, a job TTL expiring, `submit` failing partway, a follower cancelling (S3, S7, S8, S10).
- A test that a coalesced follower is served at its own fair turn (S5).
- Randomised fairness tests with many users and multiple companies.
- Real-Redis concurrency: the tests use fakeredis unless `REDIS_TEST_URL` is set.
- Six files in `tests/` are manual scripts with no `test_` functions: `test_scheduler_api.py`, `test_scheduler_demo.py`, `test_real_llm_fairness.py`, `test_dynamic_scheduler.py`, `test_scheduler_performance.py`, `test_scheduler_worker.py`. Consider moving them to `scripts/` or `eval/`.

---

## 8. History of this work

| Date | Step | Outcome |
|---|---|---|
| 2026-11-10 | Review of the whole gateway (report only) | Findings S1-S12, C1-C15, X1-X3; cache baseline 15/32 hits, 0/26 wrong |
| 2026-11-10 | Fix cache findings and add the accuracy features | Everything in section 1; 201 tests pass |
| 2026-11-10 | Cost and reliability benchmark | `eval/cost/`, results in `RESULTS.md` |
| 2026-11-11 | This handoff rewrite of `REVIEW.md` | – |

The first version of this file, the 2026-11-10 review, was never committed. This rewrite keeps **every finding**. Open and deferred ones (S1-S12, C10, X2, X3) keep their full detail: location, concrete failure scenario, suggested fix, and how they were checked. Fixed ones are shortened to what changed and where. The verbatim rule patch from the original Appendix A is no longer included, because it has been applied and extended in `cache_checks.py`; `git diff 2007299 -- smart_ai_gateway/services/cache_checks.py` shows it.
