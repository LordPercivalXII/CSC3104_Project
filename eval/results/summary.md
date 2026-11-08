# Scheduler experiment results

Setups used (from setups.csv):

    time,setup,backend,limit,llm,sharing,gateway_pods,commit
    2026-11-07T17:44:32+08:00,fair-shared,redis,9,mock,true,3,f0c9cbb
    2026-11-07T17:58:04+08:00,fair-per-pod,memory,3,mock,true,4,f0c9cbb
    2026-11-07T19:17:49+08:00,limit-per-pod,memory,9,mock,true,3,f0c9cbb
    2026-11-07T19:28:39+08:00,shared-limit,redis,9,mock,true,4,f0c9cbb
    2026-11-07T20:00:31+08:00,sharing-on,redis,10,config,true,3,f0c9cbb
    2026-11-07T20:15:37+08:00,sharing-off,redis,10,config,false,3,f0c9cbb

## E1 fairness: heavy user sends 18 at once, 3 light users send 1 each 5 s later

| Setup | Runs | Light users' mean wait (s) | Heavy user's last answer (s) |
| --- | --- | --- | --- |
| FIFO (one queue) | 3 | 114.1 ± 0.0 | 60.4 ± 0.0 |
| Fair share per pod | 3 | 54.3 ± 0.0 | 140.2 ± 34.5 |
| Fair share shared (Redis) | 3 | 54.1 ± 0.0 | 120.1 ± 0.0 |

## E3 rate limit: 18 requests per minute for 2 minutes, limit set to 9 per minute

| Setup | Runs | Most LLM calls in any 60 s (per run) | Last answer (s) |
| --- | --- | --- | --- |
| Limit per pod (memory) | 3 | 18, 18, 18 | 117 |
| One shared limit (Redis) | 3 | 9, 9, 9 | 207 |

## Duplicate questions: N people ask the same new question at once (real Gemini, limit 10 per minute)

Mean over runs of how many people got each kind of answer. Every "new LLM call" is one Gemini request.

| N | Sharing | Runs | New LLM calls | Shared answers | Semantic cache | Mean wait (s) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | off | 3 | 1.0 (1/1/1) | 0.0 | 0.0 | 5.9 |
| 1 | on | 3 | 1.0 (1/1/1) | 0.0 | 0.0 | 9.1 |
| 5 | off | 3 | 5.0 (5/5/5) | 0.0 | 0.0 | 4.5 |
| 5 | on | 3 | 1.0 (1/1/1) | 3.3 | 0.7 | 4.6 |
| 10 | off | 3 | 9.7 (10/10/9) | 0.0 | 0.3 | 7.4 |
| 10 | on | 3 | 1.3 (1/2/1) | 8.3 | 0.3 | 2.9 |
| 20 | off | 3 | 7.7 (12/3/8) | 0.0 | 12.3 | 6.1 |
| 20 | on | 3 | 1.0 (1/1/1) | 10.3 | 8.7 | 4.1 |

Requests that failed (not HTTP 200): 0
