"""Interactive gateway traffic for the demo; importing this sends no requests.

Run with the demo Compose stack, or from the repository root:
    locust -f demo/locust/locustfile.py --host http://localhost:8000

DEMO_SCENARIO=cache (default) samples eval/cost/data/intents.json, with the same
default category shares as the evaluation. misses/scheduler remain stress probes.
The target gateway controls the LLM provider: this file does not override it.
"""
from itertools import count
import logging
import os
from pathlib import Path
import random
import sys
import uuid

from locust import HttpUser, between, events, task

# Also works when the containing directory is mounted directly into Locust's image.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workloads import (  # noqa: E402
    COLD_PROMPT_SPACE, EvaluationWorkload, choose_request, response_error, response_outcome, scenario_name,
)


SCENARIO = scenario_name(os.getenv("DEMO_SCENARIO", "cache"))
REQUEST_TIMEOUT = float(os.getenv("DEMO_REQUEST_TIMEOUT_SECONDS", "180"))
if not 0 < REQUEST_TIMEOUT <= 3600:
    raise ValueError("DEMO_REQUEST_TIMEOUT_SECONDS must be greater than 0 and at most 3600")

_rng = random.Random()
_workload = EvaluationWorkload.from_environment() if SCENARIO == "cache" else None
_sequence = count(0 if _workload else _rng.randrange(COLD_PROMPT_SPACE))


@events.init.add_listener
def log_dataset(environment, **kwargs):
    """Log after Locust configures logging, not during module discovery."""
    if _workload is None:
        return
    logging.getLogger(__name__).info(
        "Evaluation corpus: %s, sha256=%s, %s intents, %s paraphrases, %s personal templates, %s time prompts",
        _workload.dataset.path, _workload.dataset.sha256, len(_workload.dataset.intents),
        sum(len(i.paraphrases) for i in _workload.dataset.intents),
        len(_workload.dataset.personal_templates), len(_workload.dataset.time_sensitive),
    )
    logging.getLogger(__name__).info(
        "Sampling: seed=%s, zipf_s=%s, paraphrase_share=%s, personal_share=%s, time_share=%s, run_id=%s",
        _workload.seed, _workload.zipf_s, _workload.paraphrase_share,
        _workload.personal_share, _workload.time_share, _workload.run_id,
    )


class GatewayUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Keep one identity for this simulated user's entire lifetime so the
        # scheduler can give each user turns. Never put identities into labels.
        self.user_id = f"demo-{uuid.uuid4().hex}"

    @task
    def ask(self):
        case = choose_request(SCENARIO, _rng, next(_sequence), _workload)
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
