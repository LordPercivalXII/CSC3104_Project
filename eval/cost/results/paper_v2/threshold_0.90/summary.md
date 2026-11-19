# Benchmark summary

Raw runs: 5 (config x seed x params x size); prices retrieved 2026-11-10. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 5 | 49.4 ± 0.8% | 49.3 ± 0.6% | 0.14 ± 0.16% | 0.28 ± 0.31% | 49.7 ± 2.0% | 0.00 ± 0.00% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 71 ± 2 | 240 ± 28 | 3358 ± 126 | 8192 ± 265 | 941 ± 283 | 7563 ± 211 |

Gateway CPU per request (harness baseline removed): new size 50 s=1.0 p=0.5: 102.1 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: AWS t3.medium ap-southeast-1 (1 node(s), fixed $38.54/month, $0.0528/h)

(Full grid of all hosting options: economics.csv)

#### Gemini 3.5 Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.1346 ± 0.0235 | 0.1342 ± 0.0235 | 0.44 ± 0.08 | -38.10 ± 0.08 | 293,101 |

#### Gemini 3.8 Flash (in $0.75/M, out $3.75/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2171 ± 0.0350 | 0.2165 ± 0.0350 | 0.71 ± 0.12 | -37.83 ± 0.12 | 180,938 |

#### Gemini 3.1 Pro Preview (top Pro model listed on the pricing page) (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.6744 ± 0.1123 | 0.6725 ± 0.1123 | 2.22 ± 0.37 | -36.33 ± 0.37 | 58,149 |

#### Gemini 4 Argon (Google flagship reasoning model, for comparison) (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5789 ± 0.0934 | 0.5773 ± 0.0934 | 1.90 ± 0.31 | -36.64 ± 0.31 | 67,701 |

#### OpenAI GPT-5.4 mini (mid-tier chat) (in $0.75/M, out $4.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2529 ± 0.0421 | 0.2522 ± 0.0421 | 0.83 ± 0.14 | -37.71 ± 0.14 | 155,362 |

#### Anthropic Claude Sonnet 5.5 (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5789 ± 0.0934 | 0.5773 ± 0.0934 | 1.90 ± 0.31 | -36.64 ± 0.31 | 67,701 |
