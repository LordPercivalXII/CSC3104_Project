# Manual scheduler scripts

Demonstrations and benchmarks that print results for a person to read. They are not
automated tests (no assertions), so they live here rather than in `tests/`. Importing any
of them does nothing; `tests/test_eval_reproducibility.py` checks that.

Run from the repo root as modules, so `scheduler` is importable:

| Script | What it shows | Needs |
|---|---|---|
| `python -m scripts.manual.round_robin_demo` | Round-robin order across three users | nothing |
| `python -m scripts.manual.dynamic_scheduler_demo` | Requests arriving at different times | nothing |
| `python -m scripts.manual.scheduler_performance` | Fair share vs FIFO waiting times (simulated) | nothing |
| `python -m scripts.manual.scheduler_worker_demo` | The worker processing jobs with a mock LLM | nothing |
| `python -m scripts.manual.scheduler_api_demo` | Concurrent requests to `/scheduler/chat` | a running gateway |
| `python -m scripts.manual.real_llm_fairness` | Fairness against the real LLM via `/chat` | a running gateway; uses Gemini quota |
