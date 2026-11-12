"""Demo entrypoint: real cache/scheduler, with an optional delayed mock LLM.

    python -m uvicorn demo.app:app --host 0.0.0.0 --port 8000

The normal main:app entrypoint never applies this delay. Gemini is never delayed.
"""
import os
import time

import main
from smart_ai_gateway.config import get_settings

delay = float(os.getenv("DEMO_MOCK_DELAY_SECONDS", "1"))
if not 0 <= delay <= 10:
    raise ValueError("DEMO_MOCK_DELAY_SECONDS must be between 0 and 10")

if get_settings().llm.provider == "mock":
    _generate = main.generate_answer

    def _delayed_mock(prompt: str):
        time.sleep(delay)
        return _generate(prompt)

    # The observed scheduler function in main calls this at execution time, so its
    # inference-duration metric includes the simulated model service time.
    main.generate_answer = _delayed_mock

app = main.app
