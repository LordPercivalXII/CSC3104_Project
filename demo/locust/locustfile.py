"""Interactive gateway traffic for the demo; importing this sends no requests.

Run with the demo Compose stack, or from the repository root:
    locust -f demo/locust/locustfile.py --host http://localhost:8000

DEMO_SCENARIO=cache (default), misses, or scheduler selects the workload.
Start with 5 users at 1 user/second; the demo guide stays at 50 users or fewer.
The target gateway controls the LLM provider: this file does not override it.
"""
from itertools import count
import os
from pathlib import Path
import random
import sys
import uuid

from locust import HttpUser, between, task

# Also works when the containing directory is mounted directly into Locust's image.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workloads import (  # noqa: E402
    COLD_PROMPT_SPACE, choose_request, response_error, response_outcome, scenario_name,
)


SCENARIO = scenario_name(os.getenv("DEMO_SCENARIO", "cache"))
REQUEST_TIMEOUT = float(os.getenv("DEMO_REQUEST_TIMEOUT_SECONDS", "180"))
if not 0 < REQUEST_TIMEOUT <= 3600:
    raise ValueError("DEMO_REQUEST_TIMEOUT_SECONDS must be greater than 0 and at most 3600")

_rng = random.Random()
_sequence = count(_rng.randrange(COLD_PROMPT_SPACE))


class GatewayUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Keep one identity for this simulated user's entire lifetime so the
        # scheduler can give each user turns. Never put identities into labels.
        self.user_id = f"demo-{uuid.uuid4().hex}"

    @task
    def ask(self):
        case = choose_request(SCENARIO, _rng, next(_sequence))
        with self.client.post(
            case.endpoint,
            json=case.payload(self.user_id),
            name=f"{case.endpoint} [{case.kind}]",
            context={"scenario": SCENARIO, "kind": case.kind},
            timeout=REQUEST_TIMEOUT,
            catch_response=True,
        ) as response:
            try:
                body = response.json()
            except ValueError:
                body = None
            error = response_error(response.status_code, body, case.endpoint)
            if error:
                response.failure(error)
                response.request_meta["context"]["outcome"] = "error"
            else:
                response.success()
                response.request_meta["context"]["outcome"] = response_outcome(body, case.endpoint)
