"""Small, dependency-free workloads shared by Locust and the offline tests.

The percentages describe inputs, not promised cache hit rates. An empty cache,
similarity thresholds, expiry and incomplete LLM answers all affect real hits.
"""
from dataclasses import dataclass
import random


SCENARIOS = ("cache", "misses", "scheduler")
FAQ_PAIRS = (
    ("how do I reset my password", "I forgot my password, how can I change it"),
    ("how do I connect to the VPN from home", "what are the steps to use the VPN when working remotely"),
    ("how many days of annual leave do I get", "what is my yearly vacation entitlement"),
    ("where can I find my payslip", "how do I view my salary statement"),
    ("how do I book a meeting room", "what's the process for reserving a conference room"),
    ("my laptop is running very slowly", "my computer has become really slow"),
    ("who do I contact for IT support", "how do I reach the IT helpdesk"),
    ("how do I submit an expense claim", "what is the process to claim back expenses"),
)

# Two separate four-digit codes avoid the cache's personal-ID/phone filters.
# A process starts at a random offset, then does not repeat a cold prompt for
# 81 million requests. Other runs/processes can still overlap existing entries.
COLD_PROMPT_SPACE = 9000 * 9000


@dataclass(frozen=True)
class RequestCase:
    endpoint: str
    prompt: str
    kind: str

    def payload(self, user_id: str) -> dict[str, str]:
        return {"user_id": user_id, "prompt": self.prompt}


def scenario_name(value: str) -> str:
    value = value.strip().lower()
    if value not in SCENARIOS:
        raise ValueError(f"DEMO_SCENARIO must be one of {', '.join(SCENARIOS)}; got {value!r}")
    return value


def fresh_prompt(sequence: int) -> str:
    """A number mismatch should reject a previous printer-answer candidate."""
    device, error = divmod(sequence % COLD_PROMPT_SPACE, 9000)
    return f"How can I resolve printer error {error + 1000} on device {device + 1000}?"


def choose_request(scenario: str, rng: random.Random, sequence: int) -> RequestCase:
    scenario = scenario_name(scenario)
    if scenario == "scheduler":
        return RequestCase("/scheduler/chat", fresh_prompt(sequence), "scheduler")
    if scenario == "misses":
        return RequestCase("/chat", fresh_prompt(sequence), "cold")

    # Approximately 60% exact repeats, 20% alternative wordings, 20% fresh questions.
    bucket = rng.randrange(10)
    if bucket >= 8:
        return RequestCase("/chat", fresh_prompt(sequence), "cold")
    pair = rng.choice(FAQ_PAIRS)
    return RequestCase("/chat", pair[bucket >= 6], "paraphrase" if bucket >= 6 else "faq")


def response_error(status_code: int, body: object, endpoint: str) -> str | None:
    """A successful HTTP status alone does not establish that a chat succeeded."""
    if status_code != 200:
        return f"HTTP {status_code}" if status_code else "Connection failed or timed out"
    if not isinstance(body, dict):
        return "Response is not a JSON object"
    answer = body.get("response")
    if not isinstance(answer, str) or not answer.strip():
        return "Response has no non-empty answer"
    sources = {"semantic_cache", "mock_llm", "gemini"}
    if endpoint == "/scheduler/chat":
        # The scheduler endpoint currently reports 'mock' even when its
        # inference function uses Gemini; never infer provider from it.
        sources = {"mock", "mock_llm", "gemini"}
    if body.get("source") not in sources:
        return "Response has an unexpected source"
    return None


def response_outcome(body: dict, endpoint: str) -> str:
    """A bounded request-event context value; no prompt or user identifiers."""
    if endpoint == "/scheduler/chat":
        return "scheduler"
    if body.get("source") == "semantic_cache":
        return "semantic_cache"
    return "coalesced" if body.get("coalesced") else "llm"
