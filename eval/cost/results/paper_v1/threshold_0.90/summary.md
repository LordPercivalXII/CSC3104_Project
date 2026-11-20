# Benchmark summary

Raw runs: 5 (config x seed x params x size); prices retrieved 2026-11-10. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 5 | 46.8 ± 3.4% | 46.7 ± 3.5% | 0.14 ± 0.16% | 0.31 ± 0.37% | 47.0 ± 4.5% | 0.01 ± 0.02% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 58 ± 5 | 211 ± 29 | 3367 ± 115 | 8148 ± 272 | 1242 ± 162 | 7535 ± 198 |

Gateway CPU per request (harness baseline removed): new size 50 s=1.0 p=0.5: 76.1 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: AWS t3.medium ap-southeast-1 (1 node(s), fixed $38.54/month, $0.0528/h)

(Full grid of all hosting options: economics.csv)

#### Gemini 3.5 Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.1275 ± 0.0275 | 0.1271 ± 0.0277 | 0.42 ± 0.09 | -38.13 ± 0.09 | 311,538 |

#### Gemini 3.8 Flash (in $0.75/M, out $3.75/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2056 ± 0.0417 | 0.2050 ± 0.0420 | 0.68 ± 0.14 | -37.87 ± 0.14 | 192,362 |

#### Gemini 3.1 Pro Preview (top Pro model listed on the pricing page) (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.6388 ± 0.1329 | 0.6368 ± 0.1337 | 2.10 ± 0.44 | -36.44 ± 0.44 | 61,858 |

#### Gemini 4 Argon (Google flagship reasoning model, for comparison) (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5483 ± 0.1112 | 0.5466 ± 0.1120 | 1.80 ± 0.37 | -36.74 ± 0.37 | 72,009 |

#### OpenAI GPT-5.4 mini (mid-tier chat) (in $0.75/M, out $4.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2395 ± 0.0498 | 0.2388 ± 0.0501 | 0.79 ± 0.17 | -37.76 ± 0.17 | 165,204 |

#### Anthropic Claude Sonnet 5.5 (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5483 ± 0.1112 | 0.5466 ± 0.1120 | 1.80 ± 0.37 | -36.74 ± 0.37 | 72,009 |
