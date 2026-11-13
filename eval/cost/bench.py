"""
Benchmark runner: replays a synthetic workload through the gateway and writes one raw CSV per
(config, seed, zipf_s, paraphrase_share).

Configs
  nocache  every request is one LLM call (computed directly, no app).
  old      frozen git archive of --old-ref (default 2007299), in a temporary directory.
  new      the working tree, also in a separate subprocess.
The app (main.app) runs in-process under fastapi TestClient; Qdrant is local in-memory with a fresh
collection per run (per company with --isolation per_company, the default); the scheduler's
inference function is replaced by the replay LLM (recorded real answers/tokens, no API calls).
Time is virtual: semantic_cache's clock is patched so TTL / aging see the workload's timestamps.

Notes for the article
  * Requests are sequential, so request coalescing (identical simultaneous questions) never fires.
  * cpu_s = time.process_time() delta around the request (all threads of the process, including
    embedding, Qdrant search, verifier, scheduler thread). cpu_baseline_s is the same measure for a
    /health call (TestClient + HTTP plumbing); analyze.py subtracts it. torch is limited to
    --torch-threads threads (default 1) so CPU-seconds approximate "one core's work".
  * llm_latency_s is the RECORDED real Gemini latency of the answer (replay itself takes ~0).

Example
  python bench.py --configs nocache,old,new --seeds 1-5 --sizes 10,50,200 --days 20 --zipf-s 0.8,1.0,1.2
"""
import argparse
from contextlib import ExitStack, contextmanager
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_OLD_REF = "2007299"
BASE_TS = 1_700_000_000.0

COLUMNS = ["config", "seed", "zipf_s", "para_share", "personal_share", "time_share", "variation", "isolation",
           "lam", "days", "size", "company", "user", "idx", "t", "kind", "intent", "prompt", "status", "source", "hit",
           "served_intent", "correct", "confusable_error", "match_type", "extra", "gw_wall_ms", "cpu_s",
           "cpu_baseline_s", "llm_calls", "llm_prompt_tokens", "llm_output_tokens", "llm_thinking_tokens",
           "llm_latency_s", "base_prompt_tokens", "base_output_tokens", "base_thinking_tokens",
           "base_latency_s", "synthetic"]


def parse_seeds(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def raw_name(config, seed, zs, ps, sizes, days):
    return f"{config}__seed{seed}__s{zs}__p{ps}__n{'-'.join(map(str, sizes))}__d{days}.csv"


def grid(a):
    sizes = [int(x) for x in a.sizes.split(",")]
    for seed, zs, ps in itertools.product(parse_seeds(a.seeds), a.zipf_s.split(","), a.paraphrase_share.split(",")):
        yield seed, zs, ps, sizes


# ---------------------------------------------------------------- child: run the app
@contextmanager
def _in_memory_qdrant_imports():
    """Prevent even the import-time Qdrant compatibility HTTP check.

    The gateway constructs a client before the first per-company collection is
    created. Redirect that construction to local storage, not a live endpoint.
    """
    from qdrant_client import QdrantClient
    with patch("qdrant_client.QdrantClient", side_effect=lambda *args, **kwargs: QdrantClient(":memory:")):
        yield


def _setup_app(a):
    """Import the gateway from --code-root and return (main_module, sc_module, llm_module)."""
    root = str(Path(a.code_root).resolve())
    os.chdir(root)
    # Do not inherit another deployment's overrides or a config outside the snapshot.
    for key in list(os.environ):
        if key.startswith("GW_") or key in ("GATEWAY_CONFIG", "QDRANT_URL", "SCHEDULER_COALESCE", "SCHEDULER_WORKERS",
                                            "GEMINI_API_KEY", "GOOGLE_API_KEY"):
            os.environ.pop(key)
    for kv in a.env or []:
        k, v = kv.split("=", 1)
        os.environ[k] = v
    os.environ.update({"GW_LLM_PROVIDER": "mock", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                       "PYTHONIOENCODING": "utf-8", "SCHEDULER_BACKEND": "memory",
                       "LLM_RATE_LIMIT_PER_MINUTE": "0", "SCHEDULER_WORKERS": "1",
                       "GW_GATEWAY_DECISION_LOG_ENABLED": "true" if a.decision_log_dir else "false"})
    sys.path[:] = [root, str(HERE)] + [p for p in sys.path if Path(p or ".").resolve() not in (Path(root), HERE)]
    try:
        import torch
        torch.set_num_threads(a.torch_threads)
    except Exception:
        pass
    with _in_memory_qdrant_imports():
        import main
        from smart_ai_gateway.services import semantic_cache as sc
        from smart_ai_gateway.services import llm
    return main, sc, llm


class _Clock:
    """Stands in for the `time` module inside semantic_cache; time() returns the virtual clock."""

    def __init__(self):
        self.now = None

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return self.now if self.now is not None else time.time()


def _new_collection(sc):
    from qdrant_client import QdrantClient, models
    try:
        sc.client.close()
    except Exception:
        pass
    sc.client = QdrantClient(":memory:")
    dim = getattr(sc._cfg, "embedding_dim", 384)
    sc.client.create_collection(sc.COLLECTION_NAME,
                                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
    try:
        from smart_ai_gateway.services import cache_aging
        if hasattr(cache_aging, "ensure_indexes"):
            cache_aging.ensure_indexes(sc.client, sc.COLLECTION_NAME)
    except Exception as exc:
        print("index creation skipped:", exc, file=sys.stderr)


def _row_base(a, config, seed, zs, ps, r):
    return {"config": config, "seed": seed, "zipf_s": zs, "para_share": ps, "personal_share": a.personal_share,
            "time_share": a.time_share, "variation": a.variation, "isolation": a.isolation, "lam": a.lam,
            "days": a.days, "size": r["size"], "company": r["company"], "user": r["user_id"], "idx": r["idx"],
            "t": round(r["t"], 1), "kind": r["kind"], "intent": r["intent_id"], "prompt": r["prompt"]}


def _fill_base(row, info):
    row.update(base_prompt_tokens=info["prompt"], base_output_tokens=info["output"],
               base_thinking_tokens=info["thinking"], base_latency_s=info["latency"], synthetic=int(info["synthetic"]))


def run_nocache(a, wl, replay, seed, zs, ps):
    rows = []
    for r in wl:
        info = replay.info(r["intent_id"])
        row = _row_base(a, "nocache", seed, zs, ps, r)
        row.update(status=200, source="llm", hit=0, served_intent=r["intent_id"], correct=1, confusable_error=0,
                   gw_wall_ms=0, cpu_s=0, cpu_baseline_s=0, llm_calls=1, llm_prompt_tokens=info["prompt"],
                   llm_output_tokens=info["output"], llm_thinking_tokens=info["thinking"], llm_latency_s=info["latency"])
        _fill_base(row, info)
        rows.append(row)
    return rows


def run_app(a, config, wl, replay, ctx, seed, zs, ps, intents_doc, log_path=None):
    from fastapi.testclient import TestClient
    from replay import served_intent
    main, sc, clock = ctx
    confusable = {i["id"]: set(i.get("confusable_with") or []) for i in intents_doc["intents"]}
    main.scheduler_worker.inference_fn = replay
    if log_path is not None and hasattr(main, "_log_decision"):
        # per-run decision log (the real append_jsonl is still used)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.unlink(missing_ok=True)
        main.resolve_path = lambda _p, _lp=log_path: _lp
    rows = []
    with TestClient(main.app, raise_server_exceptions=False) as client:
        # CPU cost of the test harness itself (subtracted in analyze.py)
        client.get("/health")
        c0 = time.process_time()
        for _ in range(30):
            client.get("/health")
        baseline = (time.process_time() - c0) / 30
        current_company = None
        for r in wl:
            if a.isolation == "per_company" and r["company"] != current_company:
                _new_collection(sc)
                current_company = r["company"]
            elif current_company is None:
                _new_collection(sc)
                current_company = -1
            clock.now = BASE_TS + r["t"]
            replay.hint = r["intent_id"]
            n0 = len(replay.calls)
            w0, c0 = time.perf_counter(), time.process_time()
            try:
                resp = client.post("/chat", json={"user_id": r["user_id"], "prompt": r["prompt"]})
                status, body = resp.status_code, (resp.json() if resp.status_code == 200 else {})
            except Exception as exc:  # noqa: BLE001
                status, body = 599, {"error": str(exc)}
            wall, cpu = (time.perf_counter() - w0) * 1000, time.process_time() - c0
            new_calls = replay.calls[n0:]
            hit = int(status == 200 and not new_calls)
            row = _row_base(a, config, seed, zs, ps, r)
            info = replay.info(r["intent_id"])
            served = served_intent(body.get("response", "")) if status == 200 else None
            extra = {k: v for k, v in body.items() if k not in ("response", "source", "match_type", "request_id")}
            row.update(status=status, source=("semantic_cache" if hit else ("llm" if new_calls else "error")),
                       hit=hit, served_intent=served or "", correct=int(status == 200 and served == r["intent_id"]),
                       confusable_error=int(hit and served in confusable.get(r["intent_id"], ())),
                       match_type=body.get("match_type") or "", extra=json.dumps(extra, default=str),
                       gw_wall_ms=round(wall, 3), cpu_s=round(cpu, 5), cpu_baseline_s=round(baseline, 5),
                       llm_calls=len(new_calls), llm_prompt_tokens=sum(c["prompt"] for c in new_calls),
                       llm_output_tokens=sum(c["output"] for c in new_calls),
                       llm_thinking_tokens=sum(c["thinking"] for c in new_calls),
                       llm_latency_s=sum(c["latency"] for c in new_calls))
            _fill_base(row, info)
            rows.append(row)
    return rows


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def _input_fingerprints(a):
    from provenance import sha256_file
    return {"intents_sha256": sha256_file(a.intents),
            "recordings_sha256": sha256_file(a.recordings) if Path(a.recordings).exists() else None}


def _harness_fingerprints():
    from provenance import sha256_file
    return {name: sha256_file(HERE / name) for name in ("bench.py", "workload.py", "replay.py", "provenance.py")}


def run_jobs(a, config):
    from provenance import code_provenance, digest, loaded_model_provenance, runtime_versions, sha256_file
    sys.path.insert(0, str(HERE))
    import workload
    from replay import ReplayLLM
    out_dir = Path(a.out)
    if not Path(a.recordings).exists():
        print(f"[WARNING] {a.recordings} not found - ALL answers will be synthetic", flush=True)
    ctx = None
    # Capture code and inputs before any requests, then reject mid-run edits.
    code_root = a.code_root if config != "nocache" else a.new_root
    code = code_provenance(code_root, getattr(a, "code_revision", None))
    inputs = _input_fingerprints(a)
    harness = _harness_fingerprints()
    intents_doc = json.loads(Path(a.intents).read_text(encoding="utf-8"))
    versions = runtime_versions()
    models, effective_settings = {}, None
    if config != "nocache":
        main, sc, llm = _setup_app(a)
        clock = _Clock()
        sc.time = clock
        ca = sys.modules.get("smart_ai_gateway.services.cache_aging")
        if ca is not None and hasattr(ca, "time"):
            ca.time = clock
        ctx = (main, sc, clock)
        legacy = not hasattr(llm, "LLMAnswer")
        models = loaded_model_provenance(sc)
        from smart_ai_gateway.config import get_settings
        effective_settings = asdict(get_settings())

    def verify_unchanged():
        checks = (("Gateway code", code_provenance(code_root, getattr(a, "code_revision", None)), code),
                  ("Benchmark input files", _input_fingerprints(a), inputs),
                  ("Benchmark harness", _harness_fingerprints(), harness))
        for label, actual, expected in checks:
            if actual != expected:
                raise RuntimeError(f"{label} changed during this benchmark; no result was written for this job")

    for seed, zs, ps, sizes in grid(a):
        verify_unchanged()
        path = out_dir / raw_name(config, seed, zs, ps, sizes, a.days)
        meta_path = path.with_suffix(".meta.json")
        identity = {"config": config, "seed": seed, "zipf_s": zs, "paraphrase_share": ps,
                    "sizes": sizes, "days": a.days, "lam": a.lam, "personal_share": a.personal_share,
                    "time_share": a.time_share, "variation": a.variation, "isolation": a.isolation,
                    "torch_threads": a.torch_threads, "env_overrides": sorted(a.env or []),
                    "code": code, "inputs": inputs, "harness_sha256": harness, "versions": versions,
                    "loaded_models": models}
        if path.exists() and not a.force:
            if not meta_path.exists():
                raise ValueError(f"{path} has no provenance; use a new --out directory (or explicit --force)")
            previous = json.loads(meta_path.read_text(encoding="utf-8"))
            if previous.get("identity_sha256") != digest(identity) or previous.get("csv_sha256") != sha256_file(path):
                raise ValueError(f"{path} belongs to different settings/code or was modified; use a new --out directory")
            print("skip existing", path.name, flush=True)
            continue
        wl = workload.generate(intents_doc, seed=seed, sizes=sizes, days=a.days, lam=a.lam, zipf_s=float(zs),
                               paraphrase_share=float(ps), personal_share=a.personal_share,
                               time_share=a.time_share, variation=a.variation)
        t0 = time.time()
        if config == "nocache":
            replay = ReplayLLM(a.recordings, seed=seed)
            replay.register(wl)
            rows = run_nocache(a, wl, replay, seed, zs, ps)
        else:
            replay = ReplayLLM(a.recordings, seed=seed, legacy_str=legacy)
            replay.register(wl)
            log_path = None
            if a.decision_log_dir:
                log_path = Path(a.decision_log_dir) / ("decisions_" + raw_name(config, seed, zs, ps, sizes, a.days).replace(".csv", ".jsonl"))
            rows = run_app(a, config, wl, replay, ctx, seed, zs, ps, intents_doc, log_path)
        verify_unchanged()
        write_csv(path, rows)
        metadata = {"schema_version": 2, "created_utc": datetime.now(timezone.utc).isoformat(),
                    "identity": identity, "identity_sha256": digest(identity),
                    "csv_sha256": sha256_file(path), "effective_settings": effective_settings,
                    "request_count": len(rows), "measurement": "sequential offline replay; virtual cache time"}
        meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        hits = sum(int(r["hit"]) for r in rows)
        errs = sum(1 for r in rows if r["status"] != 200)
        print(f"[{config}] {path.name}: {len(rows)} requests, {hits} hits, {errs} errors, {time.time() - t0:.0f}s", flush=True)


def _absolutize(argv):
    out, flag = [], False
    for tok in argv:
        if flag:
            tok, flag = str(Path(tok).resolve()), False
        elif tok in PATH_FLAGS:
            flag = True
        elif tok.split("=")[0] in PATH_FLAGS and "=" in tok:
            k, v = tok.split("=", 1)
            tok = f"{k}={Path(v).resolve()}"
        out.append(tok)
    return out


PATH_FLAGS = ("--decision-log-dir", "--intents", "--recordings", "--out", "--old-root", "--new-root")


def main():
    import workload
    ap = argparse.ArgumentParser(description="Run the gateway cost benchmark")
    workload.add_args(ap)
    ap.add_argument("--configs", default="nocache,old,new")
    ap.add_argument("--seeds", default="1-5", help="e.g. 1-5 or 1,2,7")
    ap.add_argument("--recordings", default=str(HERE / "data" / "real_answers.jsonl"))
    ap.add_argument("--out", default=str(HERE / "results" / "raw"))
    ap.add_argument("--isolation", choices=["per_company", "shared"], default="per_company")
    ap.add_argument("--old-ref", default=DEFAULT_OLD_REF, help="Git commit archived for the old baseline (default 2007299)")
    ap.add_argument("--old-root", default=None, help="Explicit external baseline directory; overrides --old-ref, files are fingerprinted")
    ap.add_argument("--new-root", default=str(REPO))
    ap.add_argument("--env", action="append", help="extra KEY=VAL for the gateway, e.g. GW_SEMANTIC_CACHE_SIMILARITY_THRESHOLD=0.9")
    ap.add_argument("--torch-threads", type=int, default=1)
    ap.add_argument("--decision-log-dir", default=None, help="new code: write one decision log per run here")
    ap.add_argument("--force", action="store_true", help="overwrite existing raw CSVs")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--code-root", help=argparse.SUPPRESS)
    ap.add_argument("--code-revision", help=argparse.SUPPRESS)
    a = ap.parse_args()
    for override in a.env or []:
        if "=" not in override or not override.split("=", 1)[0].startswith(("GW_SEMANTIC_CACHE_", "GW_GATEWAY_")):
            ap.error("--env accepts only GW_SEMANTIC_CACHE_* or GW_GATEWAY_* KEY=VALUE policy overrides")
    if set(a.configs.split(",")) - {"nocache", "old", "new"}:
        ap.error("--configs must contain only nocache,old,new")
    # the child chdir's into the gateway checkout, so every path must be absolute
    for k in ("intents", "recordings", "out", "old_root", "new_root"):
        if getattr(a, k) is not None:
            setattr(a, k, str(Path(getattr(a, k)).resolve()))
    if a.decision_log_dir:
        a.decision_log_dir = str(Path(a.decision_log_dir).resolve())
    sys.argv = [sys.argv[0]] + _absolutize(sys.argv[1:])
    if a.child:
        (config,) = a.configs.split(",")
        run_jobs(a, config)
        return
    with ExitStack() as stack:
        from provenance import archived_baseline
        baseline_revision = None
        if "old" in a.configs.split(",") and a.old_root is None:
            old_root, baseline_revision = stack.enter_context(archived_baseline(REPO, a.old_ref))
            a.old_root = str(old_root)
        _run_configs(a, baseline_revision)


def _run_configs(a, baseline_revision):
    for config in a.configs.split(","):
        if config == "nocache":
            run_jobs(a, config)
            continue
        root = {"old": a.old_root, "new": a.new_root}[config]
        cmd = [sys.executable, str(HERE / "bench.py"), "--child", "--configs", config, "--code-root", root]
        if config == "old" and baseline_revision:
            cmd += ["--code-revision", baseline_revision]
        # re-pass every option except --configs (child gets exactly one)
        passthrough = []
        skip = False
        for tok in sys.argv[1:]:
            if skip:
                skip = False
                continue
            if tok in ("--configs", "--code-root"):
                skip = True
                continue
            if tok.startswith("--configs=") or tok.startswith("--code-root="):
                continue
            passthrough.append(tok)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", GW_LLM_PROVIDER="mock")
        print(f"== {config}: {root}", flush=True)
        # gateway chatter (print statements) is discarded; progress lines from run_jobs start with '['
        p = subprocess.Popen(cmd + passthrough, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             text=True, encoding="utf-8", errors="replace")
        tail = []
        for line in p.stdout:
            if line.startswith(("[", "skip", "==")):
                print(line, end="", flush=True)
            tail = (tail + [line])[-40:]
        if p.wait() != 0:
            print("".join(tail))
            sys.exit(f"{config} run failed (exit {p.returncode}); last output above")


if __name__ == "__main__":
    main()
