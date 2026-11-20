"""Live traffic from the evaluation corpus; standard library only.

The cache scenario reads the canonical eval/cost/data/intents.json, mounted into
Locust containers. It shares the evaluation's default category/phrasing shares,
but does not reproduce its NumPy RNG, company model or virtual-time simulation.
Input shares never promise cache-hit rates or establish answer correctness.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import random
import uuid


SCENARIOS = ("cache", "misses", "scheduler")


@dataclass(frozen=True)
class FAQIntent:
    intent_id: str
    canonical: str
    paraphrases: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationDataset:
    path: Path
    sha256: str
    intents: tuple[FAQIntent, ...]
    personal_templates: tuple[str, ...]
    time_sensitive: tuple[str, ...]


def load_dataset(path=None) -> EvaluationDataset:
    """Use the exact evaluation JSON, never a second hardcoded prompt list."""
    selected = path if path is not None else os.getenv("DEMO_INTENTS_PATH")
    if selected is None:
        selected = Path(__file__).resolve().parents[2] / "eval" / "cost" / "data" / "intents.json"
    return _read_dataset(Path(selected).resolve())


@lru_cache(maxsize=8)
def _read_dataset(path: Path) -> EvaluationDataset:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Cannot read the evaluation dataset at {path}; set DEMO_INTENTS_PATH or mount intents.json") from exc
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or not isinstance(doc.get("intents"), list) or not doc["intents"]:
            raise ValueError("intents must be a nonempty list")
        def text(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("all IDs, prompts and templates must be nonempty strings")
            return value
        intents = []
        for item in doc["intents"]:
            if not isinstance(item, dict) or not isinstance(item.get("paraphrases", []), list):
                raise ValueError("each intent must have a paraphrases list")
            intents.append(FAQIntent(text(item["id"]), text(item["canonical"]),
                                     tuple(text(p) for p in item.get("paraphrases", []))))
        if len({i.intent_id for i in intents}) != len(intents):
            raise ValueError("intent IDs must be unique")
        personal, temporal = doc.get("personal_templates"), doc.get("time_sensitive")
        if not isinstance(personal, list) or not personal or not isinstance(temporal, list) or not temporal:
            raise ValueError("personal_templates and time_sensitive must be nonempty lists")
        # Validate formatting at startup rather than failing halfway through a load test.
        slots = dict.fromkeys(("name", "pasted", "date", "amount", "id"), "example")
        for template in personal:
            text(template).format_map(slots)
        return EvaluationDataset(path, hashlib.sha256(raw).hexdigest(), tuple(intents),
                                 tuple(text(p) for p in personal), tuple(text(p) for p in temporal))
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"Invalid evaluation dataset at {path}: {exc}") from exc

# Two separate four-digit codes avoid the cache's personal-ID/phone filters.
# A process starts at a random offset, then does not repeat a cold prompt for
# 81 million requests. Other runs/processes can still overlap existing entries.
COLD_PROMPT_SPACE = 9000 * 9000


@dataclass(frozen=True)
class RequestCase:
    endpoint: str
    prompt: str
    kind: str
    intent_id: str | None = None

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


class EvaluationWorkload:
    """Seeded corpus sampling for live users, with no inference or network I/O."""

    def __init__(self, dataset=None, *, seed=1, paraphrase_share=0.5, personal_share=0.05,
                 time_share=0.03, zipf_s=1.0, run_id=None):
        shares = (paraphrase_share, personal_share, time_share)
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in shares) or personal_share + time_share > 1:
            raise ValueError("DEMO_*_SHARE values must be between 0 and 1; personal + time cannot exceed 1")
        if not math.isfinite(zipf_s) or zipf_s < 0:
            raise ValueError("DEMO_ZIPF_S must be finite and nonnegative")
        self.dataset = dataset if dataset is not None else load_dataset()
        self.seed, self.zipf_s = seed, zipf_s
        self.rng = random.Random(seed)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.paraphrase_share, self.personal_share, self.time_share = shares
        # A stable shuffled rank assignment avoids always making the first JSON
        # intent most popular. This is not the offline generator's NumPy stream.
        self.ranked = list(self.dataset.intents)
        random.Random(f"{seed}:popularity").shuffle(self.ranked)
        self.weights = [(rank + 1) ** -zipf_s for rank in range(len(self.ranked))]

    @classmethod
    def from_environment(cls):
        return cls(seed=int(os.getenv("DEMO_DATA_SEED", "1")),
                   paraphrase_share=float(os.getenv("DEMO_PARAPHRASE_SHARE", "0.5")),
                   personal_share=float(os.getenv("DEMO_PERSONAL_SHARE", "0.05")),
                   time_share=float(os.getenv("DEMO_TIME_SHARE", "0.03")),
                   zipf_s=float(os.getenv("DEMO_ZIPF_S", "1.0")))

    def choose(self, sequence: int) -> RequestCase:
        rng, dataset = self.rng, self.dataset
        bucket = rng.random()
        reference = f"{self.run_id}-{sequence}"
        if bucket < self.personal_share:
            template = rng.choice(dataset.personal_templates)
            prompt = template.format_map({
                "name": rng.choice(("Alice", "Bob", "Chen", "Dana", "Elif", "Farid")),
                "pasted": f"Please review my message about a pending request (reference {reference}).",
                "date": f"{rng.randint(1, 28)} {rng.choice(('January', 'March', 'June', 'September', 'December'))}",
                "amount": f"${rng.randint(5, 1999)}", "id": f"#{reference}",
            })
            return RequestCase("/chat", prompt, "personal", f"personal:{reference}")
        if bucket < self.personal_share + self.time_share:
            return RequestCase("/chat", rng.choice(dataset.time_sensitive), "time", f"time:{reference}")
        intent = rng.choices(self.ranked, weights=self.weights, k=1)[0]
        if intent.paraphrases and rng.random() < self.paraphrase_share:
            return RequestCase("/chat", rng.choice(intent.paraphrases), "paraphrase", intent.intent_id)
        return RequestCase("/chat", intent.canonical, "faq", intent.intent_id)


def choose_request(scenario: str, rng: random.Random, sequence: int, workload=None) -> RequestCase:
    scenario = scenario_name(scenario)
    if scenario == "scheduler":
        return RequestCase("/scheduler/chat", fresh_prompt(sequence), "scheduler")
    if scenario == "misses":
        return RequestCase("/chat", fresh_prompt(sequence), "cold")

    if workload is None:
        raise ValueError("The cache scenario requires an EvaluationWorkload")
    return workload.choose(sequence)


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
        # Keep compatibility with older gateway images that reported 'mock'.
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
