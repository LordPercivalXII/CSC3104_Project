"""
Replay LLM: returns the RECORDED real Gemini answer (and token usage / latency) for a prompt's
intent, so the benchmark spends no API money but uses real token sizes.

* Prompt -> intent comes from the workload (register()), or from `hint` set by bench.py.
* Intents without a recording get a synthetic answer; token counts and latency are drawn from
  the empirical distribution of the recordings (kind-specific if --extra samples exist, else all
  FAQ recordings). Results flag these as synthetic=True.
* Every answer ends with "\\n[ref:<intent_id>]" so a served cached answer can be mapped back to
  the intent that produced it (hit correctness scoring).
* Every call is counted in `self.calls`.
"""
import json
import random
import re
import zlib
from pathlib import Path

REF_RE = re.compile(r"\[ref:(.+?)\]")


def served_intent(text):
    """Intent id embedded in a served answer, or None."""
    m = REF_RE.findall(text or "")
    return m[-1] if m else None


class Answer:
    """Duck-types smart_ai_gateway.services.llm.LLMAnswer (text/cacheable/finish_reason/usage)."""

    def __init__(self, text, usage, finish_reason="STOP"):
        reason = getattr(finish_reason, "name", None) or finish_reason
        reason = str(reason).rsplit(".", 1)[-1] if reason is not None else None
        self.text, self.usage, self.finish_reason = text, usage, reason
        # Match the live gateway: nonempty but incomplete/safety-stopped answers
        # may be returned, but must not become reusable cached answers.
        self.cacheable = reason in (None, "STOP")


class ReplayLLM:
    def __init__(self, recordings_path=None, seed=0, legacy_str=False):
        self.seed, self.legacy_str = seed, legacy_str
        self.records = {}
        if recordings_path and Path(recordings_path).exists():
            for line in Path(recordings_path).read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.records[r["intent_id"]] = r
        faq = [r for r in self.records.values() if r.get("kind", "faq") == "faq"]
        self.pool = {"faq": faq or list(self.records.values())}
        for k in ("personal", "time"):
            self.pool[k] = [r for r in self.records.values() if r.get("kind") == k] or self.pool["faq"]
        self.prompt_intent, self.kind_of = {}, {}
        self.hint = None  # intent id of the request being sent (set by bench.py)
        self.calls = []
        self._cache = {}

    def register(self, workload):
        for r in workload:
            self.prompt_intent.setdefault(r["prompt"], r["intent_id"])
            self.kind_of[r["intent_id"]] = r["kind"]

    def info(self, intent_id):
        """Deterministic recorded-or-synthetic usage for an intent (what an LLM call would cost)."""
        if intent_id in self._cache:
            return self._cache[intent_id]
        rec = self.records.get(intent_id)
        if rec:
            d = dict(text=rec["answer"], prompt=rec["prompt_tokens"], output=rec["output_tokens"],
                     thinking=rec.get("thinking_tokens") or 0, latency=rec["latency_s"],
                     finish=rec.get("finish_reason") or "STOP", synthetic=False)
        else:
            pool = self.pool.get(self.kind_of.get(intent_id, "faq")) or []
            if pool:
                rng = random.Random(zlib.crc32(f"{self.seed}:{intent_id}".encode()))
                p = rng.choice(pool)  # draw a whole record so token counts stay correlated
                d = dict(prompt=p["prompt_tokens"], output=p["output_tokens"],
                         thinking=p.get("thinking_tokens") or 0, latency=p["latency_s"], finish="STOP")
            else:  # no recordings at all: crude constants so the pipeline still runs
                d = dict(prompt=200, output=150, thinking=0, latency=1.5, finish="STOP")
            d.update(text=f"(synthetic answer for {intent_id})", synthetic=True)
        d["text"] = d["text"].rstrip() + f"\n[ref:{intent_id}]"
        d["total"] = d["prompt"] + d["output"] + d["thinking"]
        self._cache[intent_id] = d
        return d

    def __call__(self, prompt):
        intent = self.hint if self.hint is not None else self.prompt_intent.get(prompt, f"unknown:{zlib.crc32(prompt.encode())}")
        d = self.info(intent)
        self.calls.append({"intent": intent, **{k: d[k] for k in ("prompt", "output", "thinking", "latency", "synthetic")}})
        if self.legacy_str:
            return d["text"]
        usage = {"prompt_tokens": d["prompt"], "output_tokens": d["output"], "thinking_tokens": d["thinking"],
                 "total_tokens": d["total"], "model": "replay", "estimated": False}
        return Answer(d["text"], usage, d["finish"])
