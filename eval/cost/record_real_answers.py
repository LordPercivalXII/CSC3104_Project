"""
Record REAL Gemini answers + token usage for each intent's canonical question, using exactly the
gateway's settings (model, system_instruction, thinking level, max_output_tokens from
smart_ai_gateway/gateway.config via smart_ai_gateway.services.llm).

    python record_real_answers.py --limit 5            # first try: 5 intents
    python record_real_answers.py --extra 6            # plus 6 personal/time-sensitive samples
    python record_real_answers.py --rpm 8              # default throttle

Resumable: appends to data/real_answers.jsonl and skips intent ids already present.
The API key is read from the repo's .env (GEMINI_API_KEY) and never printed. Calls cost real money
or free-tier quota. Needs network; uses the same google-genai client as the gateway.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def load_env(path):
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def call_with_retry(llm, prompt, max_attempts=6):
    from google.genai import errors
    delay = 10.0
    for attempt in range(max_attempts):
        t0 = time.perf_counter()
        try:
            resp = llm.client.models.generate_content(model=llm._cfg.model, contents=prompt,
                                                      config=llm._generation_config())
            return resp, time.perf_counter() - t0
        except errors.APIError as exc:
            if exc.code in (429, 503, 500, 502) and attempt < max_attempts - 1:
                print(f"  HTTP {exc.code}, retrying in {delay:.0f}s", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise


def main():
    ap = argparse.ArgumentParser(description="Record real Gemini answers for the benchmark")
    ap.add_argument("--intents", default=str(HERE / "data" / "intents.json"))
    ap.add_argument("--out", default=str(HERE / "data" / "real_answers.jsonl"))
    ap.add_argument("--rpm", type=float, default=8, help="max requests per minute")
    ap.add_argument("--limit", type=int, default=0, help="record at most N new intents (0 = all)")
    ap.add_argument("--extra", type=int, default=0, help="also record N personal/time-sensitive samples")
    ap.add_argument("--dry-run", action="store_true", help="list what would be recorded, no API call")
    a = ap.parse_args()

    doc = json.loads(Path(a.intents).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        done = {json.loads(l)["intent_id"] for l in out.read_text(encoding="utf-8").splitlines() if l.strip()}

    todo = [(i["id"], "faq", i["canonical"]) for i in doc["intents"] if i["id"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    extras = []
    names, pasted = ["Alice Tan", "Omar Haddad", "Priya Nair"], "the vendor says the invoice was already paid (ref #4821)"
    for n in range(a.extra):
        if n % 2 == 0 and doc.get("personal_templates"):
            tpl = doc["personal_templates"][(n // 2) % len(doc["personal_templates"])]
            extras.append((f"extra:personal:{n}", "personal", tpl.format_map(
                {"name": names[n % 3], "pasted": pasted, "date": "14 March",
                 "amount": "$320", "id": "#58213"})))
        elif doc.get("time_sensitive"):
            extras.append((f"extra:time:{n}", "time", doc["time_sensitive"][(n // 2) % len(doc["time_sensitive"])]))
    todo += [e for e in extras if e[0] not in done]
    print(f"{len(done)} already recorded, {len(todo)} to record")
    if a.dry_run or not todo:
        for t in todo:
            print(" ", t[0], "|", t[2][:80])
        return

    load_env(REPO / ".env")
    os.environ.pop("GW_LLM_PROVIDER", None)  # must be the real provider
    sys.path.insert(0, str(REPO))
    from smart_ai_gateway.services import llm
    if llm.client is None:
        sys.exit("gateway.config [llm] provider is not gemini; refusing to record mock answers")

    gap = 60.0 / a.rpm
    with open(out, "a", encoding="utf-8") as f:
        for n, (iid, kind, prompt) in enumerate(todo, 1):
            t_start = time.time()
            resp, latency = call_with_retry(llm, prompt)
            meta = resp.usage_metadata
            cand = resp.candidates[0] if resp.candidates else None
            fin = getattr(getattr(cand, "finish_reason", None), "name", None) or str(getattr(cand, "finish_reason", None))
            get = lambda k: int(getattr(meta, k, None) or 0)  # noqa: E731
            rec = {"intent_id": iid, "kind": kind, "prompt": prompt, "answer": (resp.text or "").strip(),
                   "prompt_tokens": get("prompt_token_count"), "output_tokens": get("candidates_token_count"),
                   "thinking_tokens": get("thoughts_token_count"), "total_tokens": get("total_token_count"),
                   "finish_reason": fin, "latency_s": round(latency, 3), "model": llm._cfg.model,
                   "timestamp": datetime.now(timezone.utc).isoformat()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{n}/{len(todo)}] {iid}: {rec['prompt_tokens']} in / {rec['output_tokens']} out / "
                  f"{rec['thinking_tokens']} think, {latency:.1f}s, {fin}", flush=True)
            if n < len(todo):
                time.sleep(max(0.0, gap - (time.time() - t_start)))


if __name__ == "__main__":
    main()
