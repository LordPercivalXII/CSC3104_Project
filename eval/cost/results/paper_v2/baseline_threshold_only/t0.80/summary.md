# Benchmark summary

Raw runs: 5 (config x seed x params x size); prices retrieved 2026-11-10. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 5 | 61.7 ± 1.9% | 56.4 ± 1.1% | 5.32 ± 1.60% | 8.59 ± 2.41% | 62.1 ± 2.7% | 0.00 ± 0.00% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 62 ± 2 | 87 ± 2 | 3370 ± 170 | 8088 ± 262 | 76 ± 3 | 7297 ± 144 |

Gateway CPU per request (harness baseline removed): new size 50 s=1.0 p=0.5: 65.1 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: AWS t3.medium ap-southeast-1 (1 node(s), fixed $38.54/month, $0.0528/h)

(Full grid of all hosting options: economics.csv)

#### Gemini 3.5 Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.1676 ± 0.0261 | 0.1556 ± 0.0233 | 0.51 ± 0.08 | -38.03 ± 0.08 | 251,252 |

#### Gemini 3.8 Flash (in $0.75/M, out $3.75/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2705 ± 0.0391 | 0.2508 ± 0.0346 | 0.83 ± 0.11 | -37.72 ± 0.11 | 155,458 |

#### Gemini 3.1 Pro Preview (top Pro model listed on the pricing page) (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.8402 ± 0.1251 | 0.7793 ± 0.1111 | 2.57 ± 0.37 | -35.97 ± 0.37 | 49,990 |

#### Gemini 4 Argon (Google flagship reasoning model, for comparison) (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.7213 ± 0.1042 | 0.6687 ± 0.0921 | 2.21 ± 0.30 | -36.34 ± 0.30 | 58,226 |

#### OpenAI GPT-5.4 mini (mid-tier chat) (in $0.75/M, out $4.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.3151 ± 0.0469 | 0.2922 ± 0.0417 | 0.96 ± 0.14 | -37.58 ± 0.14 | 133,447 |

#### Anthropic Claude Sonnet 5.5 (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.7213 ± 0.1042 | 0.6687 ± 0.0921 | 2.21 ± 0.30 | -36.34 ± 0.30 | 58,226 |
