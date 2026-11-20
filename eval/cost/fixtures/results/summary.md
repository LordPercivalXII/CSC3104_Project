# Benchmark summary

Raw runs: 6 (config x seed x params x size); prices retrieved 2026-01-01. Values are mean ± 95% CI across seeds (t-distribution).

Workload: lam = 3.0 questions/employee/day; month = 22 working days. Deployment: 3 gateway pods (0.25 vCPU, 0.75 GB each) + Qdrant + Redis; node reserve 0.5 vCPU / 1.0 GB; fixed cost + marginal CPU cost.

## Hit rate and reliability

| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |
|---|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 8 | 2 | 57.1 ± 24.9% | 57.1 ± 24.9% | 0.00 ± 0.00% | 0.00 ± 0.00% | 53.8 ± 35.9% | 0.00 ± 0.00% |
| nocache | 1.0 | 0.5 | 8 | 2 | 0.0 ± 0.0% | 0.0 ± 0.0% | 0.00 ± 0.00% | n/a | 0.0 ± 0.0% | 0.00 ± 0.00% |
| old | 1.0 | 0.5 | 8 | 2 | 57.1 ± 24.9% | 57.1 ± 24.9% | 0.00 ± 0.00% | 0.00 ± 0.00% | 53.8 ± 35.9% | 0.00 ± 0.00% |

## Latency (ms)

| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 8 | 28 ± 16 | 181 ± 269 | 1417 ± 132 | 1928 ± 953 | 172 ± 276 | 1735 ± 1490 |
| nocache | 1.0 | 0.5 | 8 | n/a | n/a | 1300 ± 1271 | 1720 ± 1525 | 1300 ± 1271 | 1720 ± 1525 |
| old | 1.0 | 0.5 | 8 | 57 ± 404 | 146 ± 16 | 1414 ± 127 | 1952 ± 1015 | 135 ± 121 | 1743 ± 1595 |

Gateway CPU per request (harness baseline removed): new size 8 s=1.0 p=0.5: 52.4 ms; old size 8 s=1.0 p=0.5: 47.8 ms

## Economics

Savings count correct hits only (`saved gross` also counts wrong hits). Net = savings - gateway CPU - fixed deployment cost, per company-month.

### Hosting: GCP e2-standard-2 us-central1 (1 node(s), fixed $48.91/month, $0.067/h)

(Full grid of all hosting options: economics.csv)

#### Fake Flash-Lite (in $0.3/M, out $2.5/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 8 | 528 | 0.2022 ± 0.1942 | 0.2022 ± 0.1942 | 0.11 ± 0.10 | -48.80 ± 0.10 | 243,914 |
| old | 1.0 | 0.5 | 8 | 528 | 0.2022 ± 0.1942 | 0.2022 ± 0.1942 | 0.11 ± 0.10 | -48.80 ± 0.10 | 243,858 |

#### Fake Pro (in $2.0/M, out $12.0/M)

| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |
|---|---|---|---|---|---|---|---|---|---|
| new | 1.0 | 0.5 | 8 | 528 | 1.0285 ± 0.9585 | 1.0285 ± 0.9585 | 0.54 ± 0.51 | -48.37 ± 0.51 | 47,836 |
| old | 1.0 | 0.5 | 8 | 528 | 1.0285 ± 0.9585 | 1.0285 ± 0.9585 | 0.54 ± 0.51 | -48.37 ± 0.51 | 47,834 |
