# Benchmark summary

Raw runs: 5 (config x seed x params x size); prices retrieved 2026-11-10. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 5 | 47.9 ± 1.1% | 46.0 ± 1.0% | 1.94 ± 0.59% | 4.04 ± 1.17% | 48.0 ± 1.8% | 0.00 ± 0.00% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 67 ± 1 | 99 ± 4 | 3443 ± 405 | 8191 ± 336 | 1211 ± 123 | 7640 ± 257 |

Gateway CPU per request (harness baseline removed): new size 50 s=1.0 p=0.5: 75.0 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: AWS t3.medium ap-southeast-1 (1 node(s), fixed $38.54/month, $0.0528/h)

(Full grid of all hosting options: economics.csv)

#### Gemini 3.5 Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.1294 ± 0.0217 | 0.1258 ± 0.0213 | 0.41 ± 0.07 | -38.13 ± 0.07 | 312,074 |

#### Gemini 3.8 Flash (in $0.75/M, out $3.75/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2089 ± 0.0322 | 0.2028 ± 0.0316 | 0.67 ± 0.10 | -37.88 ± 0.10 | 192,799 |

#### Gemini 3.1 Pro Preview (top Pro model listed on the pricing page) (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.6487 ± 0.1035 | 0.6301 ± 0.1016 | 2.08 ± 0.34 | -36.47 ± 0.34 | 61,986 |

#### Gemini 4 Argon (Google flagship reasoning model, for comparison) (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5570 ± 0.0859 | 0.5408 ± 0.0842 | 1.78 ± 0.28 | -36.76 ± 0.28 | 72,173 |

#### OpenAI GPT-5.4 mini (mid-tier chat) (in $0.75/M, out $4.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.2433 ± 0.0388 | 0.2363 ± 0.0381 | 0.78 ± 0.13 | -37.77 ± 0.13 | 165,545 |

#### Anthropic Claude Sonnet 5.5 (in $2.0/M, out $10.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 50 | 3,300 | 0.5570 ± 0.0859 | 0.5408 ± 0.0842 | 1.78 ± 0.28 | -36.76 ± 0.28 | 72,173 |
