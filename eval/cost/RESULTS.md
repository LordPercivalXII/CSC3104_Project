# Does a semantic prompt cache save an SME money? Benchmark results

*Run on 2026-11-10 against the working tree (uncommitted changes on `docker-k8s-deployment`). "Old" is commit `2007299`. The raw data, scripts and the commands to reproduce everything are in this folder; see [README.md](README.md).*

## Key findings

1. **The cache answers 24-66% of questions without calling the LLM.** Measured hit rates were 23.9% for a company of 10 employees, 46.4% for 50 and 65.6% for 200 (95% CIs ±4 percentage points). Bigger companies repeat more questions, so more of them are cached.
2. **The cost saved per request is real but small at today's prices.** With Gemini 3.5 Flash-Lite (the gateway's model) the cache saves **$0.07-0.18 per 1,000 requests**. With premium models (Gemini 3.1 Pro, Claude Sonnet 5.5) it saves $0.29-0.91 per 1,000 requests.
3. **For a dedicated deployment, the cache does not pay for itself at SME scale.** The cheapest measured hosting is one AWS t3.medium node at $38.54 per month. It breaks even at about **220,000 requests per month with Flash-Lite**, which is roughly 3,300 employees at 3 questions a day. With a premium model it breaks even at **44,000-51,000 requests per month**, roughly 660-770 employees. Every simulated company (10-200 staff) has a negative net saving under every price and hosting option measured.
4. **The extra running cost of the cache itself is negligible.** It uses 54-99 ms of CPU per request, about **$0.0004-0.0015 per 1,000 requests**. That is 100-250 times smaller than the LLM cost it avoids. If the gateway runs on infrastructure the company already pays for, the cache saves money from the first request.
5. **Its strongest benefits are speed and quota, not dollars.** A cached answer takes a median of **62 ms**, against 3.4 s for a Gemini call, about 55 times faster. On a fixed free-tier quota, a 65.6% hit rate means the same quota serves **about 2.9 times as many employees**.
6. **Reliability: the improved cache served 46% fewer wrong FAQ answers** than the original (378 vs 696 in 78,363 requests each), for about 1 percentage point fewer hits. Overall, **1.06-1.85% of cache hits were wrong**. Two thirds of the remaining FAQ errors are one confusable pair, maternity vs paternity leave.

---

## 1. Method

### System under test

The gateway's real `/chat` endpoint was run in-process, using FastAPI TestClient and an in-memory Qdrant with one fresh collection per company. It used the real embedding model (`bge-small-en-v1.5`), the real cross-encoder verifier and the real rule checks.

Three configurations were compared:

| Config | Description |
|---|---|
| `nocache` | Every request goes to the LLM (the baseline cost). |
| `old` | The gateway as at commit `2007299`: similarity threshold 0.95 and the original rule checks. |
| `new` | The improved gateway: 0.97 threshold, extended rule checks, caching eligibility filter, paraphrase storage, and truncated answers not cached (see `REVIEW.md`). |

### LLM answers and tokens: real Gemini measurements, replayed

- **Recording.** Each of the 151 benchmark intents was asked once to the real `gemini-3.5-flash-lite`, using the gateway's own settings: its system instruction, `thinking_level = minimal` and the 2,048-token output cap. Answer text, token counts and latency were recorded. That gave **159 real calls**: 151 intents plus 8 personal or time-sensitive samples, all with finish reason STOP.
- **Measured per answer:** prompt 102 tokens (mean; this includes the system instruction), output 104 tokens (mean; median 93, p95 192), **thinking 0 tokens**, latency median 3.6 s (p95 8.0 s).
- **Replay.** During the benchmark, every cache miss was answered from these recordings. It counted as one LLM call with the recorded tokens and latency. This makes runs reproducible and avoids free-tier limits, while keeping real answer sizes.

### Workload: synthetic SME helpdesk traffic

The workload comes from `workload.py`; every parameter is on its command line.

**Questions.** There are 151 labelled intents: IT 61, HR 40, Facilities 20, Finance 15, General 15. Each has a canonical question and 5 paraphrases (casual, terse, with typos, formal, with context). **106 intents fall into 46 "confusable" groups**: questions that look alike but need different answers, such as mute vs unmute in Teams, a $50 vs $500 expense limit, or paid vs unpaid leave. The wording is independent of the sets used to tune the thresholds; no sentence overlaps.

**Traffic.**
- Each employee asks a Poisson(3) number of questions per working day, between 09:00 and 17:00, for 20 working days.
- Questions are chosen by **Zipf(s = 1.0)** popularity, with a random rank order per seed.
- **50%** of questions use a paraphrase instead of the canonical wording.
- **5%** of requests are personal (filled templates such as "Summarise this email: …", each one unique) and **3%** are time-sensitive.

**Scale.** One company each of **10, 50 and 200 employees**, with a separate cache for each. That is about 15,700 requests per seed; 5 seeds give 78,363 requests per config.

**Sensitivity.** The 50-employee company was re-run for 10 days with Zipf s ∈ {0.6, 0.8, 1.0, 1.2} × paraphrase share ∈ {0.2, 0.5, 0.8}, using 3 seeds.

### Scoring

- Each recorded answer carries a hidden tag for its intent. A cache hit is **correct** only if the served answer came from the same intent as the question.
- A hit on a personal or time-sensitive prompt is always **wrong**.
- **Savings count correct hits only.** A wrong hit avoided an LLM call but gave a bad answer.

### Costs

**LLM prices** are paid list prices per 1M tokens, retrieved 2026-11-10 and stored in `prices.json`:

| Model | Input | Output |
|---|---|---|
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 |
| Gemini 3.8 Flash | $0.75 | $3.75 (promotional until 2026-12-31) |
| Gemini 3.1 Pro Preview | $2.00 | $12.00 |
| Gemini 4 Argon | $2.00 | $10.00 (promotional until 2026-11-30) |
| OpenAI GPT-5.4 mini | $0.75 | $4.50 |
| Claude Sonnet 5.5 | $2.00 | $10.00 |

Thinking tokens are billed as output, but there were none at `minimal`.

**Hosting** is the deployment that `k8s/*.yaml` requests (3 gateway pods, Qdrant and Redis), packed onto on-demand nodes for 730 h per month:

| Instance | $/month |
|---|---|
| AWS t3.medium, ap-southeast-1 | $38.54 |
| GCP e2-standard-2, asia-southeast1 | $60.59 |

The headline uses the cheapest. Gateway CPU time was measured per request and priced at the same node's rate per vCPU-second.

### Statistics

- Results are computed per seed, then reported as the mean ± 95% CI across seeds (t-distribution, df = 4 for the main grid).
- Old and new were compared with **paired** tests, because both run on the same seeded workloads.

---

## 2. Results

### 2.1 Hit rate and correctness

Main grid: 5 seeds, mean ± 95% CI.

| Employees | Config | Hit rate | Correct hits / requests | Wrong hits / requests | Wrong / hits | LLM tokens saved |
|---|---|---|---|---|---|---|
| 10 | old | 24.8 ± 4.5% | 24.5% | 0.26 ± 0.41% | 1.05 ± 1.51% | 25.0% |
| 10 | **new** | 23.9 ± 4.1% | 23.7% | 0.26 ± 0.41% | 1.06 ± 1.52% | 24.2 ± 5.4% |
| 50 | old | 47.3 ± 4.1% | 46.6% | 0.71 ± 0.51% | 1.48 ± 0.98% | 47.5% |
| 50 | **new** | 46.4 ± 4.0% | 45.8% | 0.60 ± 0.46% | 1.27 ± 0.89% | 46.6 ± 5.5% |
| 200 | old | 66.7 ± 3.6% | 65.0% | 1.71 ± 0.74% | 2.54 ± 1.02% | 67.5% |
| 200 | **new** | 65.6 ± 3.8% | 64.4% | 1.22 ± 0.50% | 1.85 ± 0.73% | 66.3 ± 4.3% |

`nocache` has a 0% hit rate by definition. Every hit avoids exactly one LLM call, so calls avoided equal the hit rate. The accounting checks out: LLM calls plus hits equals requests for every config.

**Old vs new.** Over 78,363 requests each, the new cache served **378 wrong FAQ answers against the old cache's 696 (−46%)**.

| Employees | Wrong FAQ answers, new − old per seed | Paired t (df = 4) | Hit-rate change |
|---|---|---|---|
| 10 | 0, 0, 0, 0, 0 | – | −0.84 ± 1.30 pp |
| 50 | −5, −5, 0, −5, −1 | −2.87 (p ≈ 0.045) | −0.89 ± 0.93 pp |
| 200 | −47, −26, −19, −203, −7 | −1.67 (not significant; one seed dominates) | −1.15 ± 1.08 pp |

The improvements trade about 1 percentage point of hit rate for fewer wrong answers.

**Wrong answers by type.**

| Type | Old | New | Notes |
|---|---|---|---|
| FAQ, wrong intent served | 696 | **378** | New: 342 of 378 are on a confusable intent. Old: 662 of 696 came through the verifier. |
| Time-sensitive prompt served from cache | 450 | 454 | The time check misses some phrasings (section 3) |
| Personal prompt served from cache | 0 | 0 | No two personal prompts were similar enough to match |

**Most common wrong FAQ answers (new).**

| Served instead | Count |
|---|---|
| maternity ↔ paternity leave application | 254 (67%) |
| remove ↔ add to a distribution list | 31 |
| Teams unmute ↔ mute | 23 |
| reporting sick ↔ sick certificate | 10 |

A typical case: the typo-ridden "how do i aply for maternty leave" was matched by the verifier (p = 0.91) to an entry about paternity leave.

### 2.2 What happened to each request (new cache)

| Stage | Share of requests |
|---|---|
| Direct hit (similarity ≥ 0.97) | 56.7% |
| Verified hit (0.75-0.97, accepted by the verifier) | 3.7% |
| Skipped (time-sensitive, personal or ineligible) | 12.65% |
| Below threshold (< 0.75) | 12.4% |
| Rejected by verifier | 10.0% |
| Rejected by rule checks | 4.6% |

**Paraphrase learning matters.** About **28% of hits** came from paraphrase storage. 6.1% were verified hits, and at least 21.9% were direct hits on a phrasing first stored through a verified match.

**The eligibility filter has a cost.** It skipped **7.25% of genuine FAQ requests**, mostly because of time words in non-time questions:
- "now" (2.2%), as in "…deactivate the automatic reply now that I have returned";
- "next year" (1.0%), as in "carry over 5 leave days into next year";
- "this month", "tomorrow" and "current";
- "my manager" (0.66%).

These are cache opportunities lost, not wrong answers.

**Warm-up.** The FAQ hit rate is nearly flat across the four quarters of each run. For 200 employees it goes 68.5%, 70.5%, 71.2%, 71.4%. The cache fills up within the first week.

### 2.3 Cost savings

New cache, hosting on AWS t3.medium ap-southeast-1 ($38.54/month). There are 22 working days per month.

| Employees | Requests / month | LLM | Saved / 1k requests | Saved / month | Net / month | Break-even requests / month |
|---|---|---|---|---|---|---|
| 10 | 660 | Flash-Lite | $0.066 | $0.04 | −$38.50 | 623k |
| 50 | 3,300 | Flash-Lite | $0.125 | $0.41 | −$38.13 | 318k |
| 200 | 13,200 | Flash-Lite | $0.178 | $2.35 | −$36.20 | **220k** |
| 200 | 13,200 | Gemini 3.8 Flash | $0.287 | $3.79 | −$34.76 | 136k |
| 200 | 13,200 | GPT-5.4 mini | $0.335 | $4.42 | −$34.14 | 117k |
| 200 | 13,200 | Claude Sonnet 5.5 | $0.766 | $10.11 | −$28.45 | 51k |
| 200 | 13,200 | Gemini 3.1 Pro | $0.893 | $11.78 | −$26.77 | **44k** |

- Each break-even figure uses the hit rate measured at that company size. Larger companies would hit more often (section 2.4), so the volumes shown are, if anything, an overestimate.
- On GCP e2-standard-2 asia-southeast1 ($60.59/month), break-even with Flash-Lite at 200 employees is 346k requests per month.
- `economics.csv` has every model × hosting combination, with 95% CIs.

**Fixed vs marginal cost.** The gateway's own CPU cost is $0.0004-0.0015 per 1,000 requests across the hosting options, against $0.07-0.91 saved. The cache is therefore **profitable as a marginal service**, for example on a cluster the company already runs, but **not as a dedicated deployment** for one SME. A shared multi-tenant service, which is this project's SaaS idea, spreads the fixed cost:
- with Flash-Lite, it needs about 17 companies of 200 staff to break even;
- with a premium model, about 4.

That needs tenant isolation, which is not implemented yet.

**Free-tier quota.** With 65.6% of calls avoided, a quota of N LLM calls per day serves N ÷ 0.344 ≈ **2.9 N** requests per day. For example, a 500-requests-per-day quota covers about 480 employees with the cache, against 166 without it. For an SME on the free tier, this is the cache's most practical value.

### 2.4 Latency

| | Median | p95 |
|---|---|---|
| Cache hit (50 / 200 employees) | 62 / 89 ms | 227 / 230 ms |
| LLM call (real Gemini, measured) | 3.4-3.8 s | 8.0 s |
| Overall, 200 employees with cache | **113 ms** | 7.1 s |
| Overall, no cache | 2.9 s | 7.9 s |

### 2.5 Sensitivity

New cache, 50 employees, 10 days, 3 seeds.

Hit rate, by Zipf popularity s and the share of questions asked as a paraphrase:

| Zipf s | Paraphrase share 0.2 | 0.5 | 0.8 |
|---|---|---|---|
| 0.6 (questions spread out) | 43.3% | 32.3% | **25.9%** (min) |
| 0.8 | 48.5% | 38.5% | 34.0% |
| 1.0 | 53.1% | 45.8% | 44.5% |
| 1.2 (a few very common questions) | **59.6%** (max) | 55.8% | 53.1% |

- Hit rate rises with s and falls with paraphrase share. Savings range from **$0.069 to $0.171 per 1,000 requests** with Flash-Lite, and from $0.35 to $0.86 with Gemini 3.1 Pro.
- The share of wrong hits stays between 0.8% and 3.5%. The worst case is many paraphrases with flat popularity. The full table and plot are in `results/sens/`.
- At s = 1.0, old vs new wrong-per-hit was 2.3% vs 1.2% at paraphrase share 0.2, and 1.4% vs 1.5% at 0.8. With only 3 seeds, these differences are not significant.

---

## 3. Threats to validity

1. **Synthetic workload.** The popularity (Zipf), the volume (3 questions per day) and the phrasing mix are assumptions, not SME measurements. The sensitivity grid shows how much this matters: the hit rate ranges from 26% to 60% at a fixed company size. Real query logs would be the strongest next step, and the gateway now writes a decision log that could collect them.
2. **Labels.** The questions and paraphrases were written by the authors with LLM help. Some confusable pairs differ only by company-policy numbers (5 vs 10 days of carry-over), which a generic LLM answer can't distinguish, so counting them as wrong is strict.
3. **Short answers make the savings look small.** The answers were short, about 100 output tokens with no thinking, because the system instruction asks for concise helpdesk replies. Longer answers, retrieval-augmented prompts with large contexts, or reasoning models would save proportionally more per hit.
4. **One cache per company, run sequentially.** Requests were sent one at a time, so coalescing and scheduler queueing aren't measured. No caches were shared across companies.
5. **Hosting prices.** These come from secondary price aggregators (official pages couldn't be fetched as text). The GKE management fee isn't confirmed and was excluded. CPU time was measured on a laptop with one PyTorch thread.
6. **Thresholds.** These were tuned on separate labelled pairs (`cache_matching_pairs.py`). This benchmark's wording is independent of them, but its topics overlap.
7. **Time-sensitive detection is rule-based.** It both misses phrasings ("at the moment", "next fire drill", "this Saturday", "on Sunday") and over-triggers ("now that…", "next year"). The 454 time-sensitive wrong hits come from the first problem; the 7% of FAQ requests skipped come mostly from the second.

## 4. Suggested follow-ups (not done in this run)

- **Add maternity/paternity** (and similar sibling terms) to the rule checks. That would remove about two thirds of the remaining wrong FAQ answers.
- **Rework the time-sensitive check.** Catch "at the moment", "next <event>" and named weekdays with "this/on"; stop skipping "now that" and policy phrases such as "into next year".
- **Benchmark tenant-shared caches** once tenant isolation exists, since the cost case depends on sharing infrastructure.

## 5. Files

| Path | Contents |
|---|---|
| `results/main/summary.md`, `summary.csv`, `economics.csv` | Main grid tables: all sizes, all prices, all hosting options |
| `results/main/hit_wrong_rates.png`, `savings_vs_volume.png` | Plots. The savings-vs-volume curve is a linear extrapolation beyond 200 employees, not a measurement |
| `results/sens/summary.md`, `sensitivity.png` | Sensitivity grid |
| `results/wrong_examples.csv`, `wrong_summary.md` | Every wrong hit (old and new), with scores |
| `results/stage_breakdown.md` | Decision stages and filter skips (new) |
| `results/raw_main/`, `raw_sens/`, `decisions/` | Per-request raw data and the gateway decision logs |
| `data/intents.json`, `data/real_answers.jsonl`, `prices.json` | Benchmark inputs |
