# Benchmark summary

Raw runs: 5 (config x seed x params x size); prices retrieved 2026-11-10. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 5 | 46.6 ± 3.6% | 46.6 ± 3.6% | 0.07 ± 0.09% | 0.16 ± 0.22% | 46.8 ± 4.7% | 0.00 ± 0.00% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 47 ± 3 | 199 ± 32 | 3383 ± 108 | 8149 ± 268 | 1242 ± 165 | 7534 ± 186 |

Gateway CPU per request (harness baseline removed): new size 50 s=1.0 p=0.5: 68.6 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: AWS t3.medium ap-southeast-1 (1 node(s), fixed $38.54/month, $0.0528/h)

(Full grid of all hosting options: economics.csv)

#### Gemini 3.5 Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.1269 ± 0.0280 | 0.1267 ± 0.0281 | 0.42 ± 0.09 | -38.13 ± 0.09 | 312,583 |

#### Gemini 3.8 Flash (in $0.75/M, out $3.75/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2047 ± 0.0425 | 0.2044 ± 0.0427 | 0.67 ± 0.14 | -37.87 ± 0.14 | 193,023 |

#### Gemini 3.1 Pro Preview (top Pro model listed on the pricing page) (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.6358 ± 0.1355 | 0.6349 ± 0.1358 | 2.10 ± 0.45 | -36.45 ± 0.45 | 62,083 |

#### Gemini 4 Argon (Google flagship reasoning model, for comparison) (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5457 ± 0.1135 | 0.5450 ± 0.1138 | 1.80 ± 0.38 | -36.75 ± 0.38 | 72,267 |

#### OpenAI GPT-5.4 mini (mid-tier chat) (in $0.75/M, out $4.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2384 ± 0.0508 | 0.2381 ± 0.0509 | 0.79 ± 0.17 | -37.76 ± 0.17 | 165,783 |

#### Anthropic Claude Sonnet 5.5 (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5457 ± 0.1135 | 0.5450 ± 0.1138 | 1.80 ± 0.38 | -36.75 ± 0.38 | 72,267 |
