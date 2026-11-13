"""Offline regression checks; no application/model imports or live service calls."""
import csv
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManualImportSafetyTests(unittest.TestCase):
    def test_manual_modules_do_not_start_threads_or_network(self):
        script = r'''
import importlib.util, pathlib, socket, threading, urllib.request
from unittest.mock import patch
names = ["test_scheduler_demo", "test_scheduler_worker", "test_dynamic_scheduler",
         "test_scheduler_performance", "test_real_llm_fairness", "test_scheduler_api"]
def forbidden(*args, **kwargs):
    raise AssertionError("manual module attempted work during import")
with patch.object(socket.socket, "connect", forbidden), patch.object(threading.Thread, "start", forbidden), patch.object(urllib.request, "urlopen", forbidden):
    for name in names:
        spec = importlib.util.spec_from_file_location(name, pathlib.Path("tests") / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class ProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provenance = load_module("eval_provenance", ROOT / "eval/cost/provenance.py")

    def test_frozen_baseline_matches_git_without_switching_branch(self):
        head_before = self.provenance.git_output(ROOT, "rev-parse", "HEAD")
        expected = subprocess.check_output(["git", "-C", str(ROOT), "show", "2007299:main.py"])
        with self.provenance.archived_baseline(ROOT) as (folder, revision):
            self.assertEqual((folder / "main.py").read_bytes(), expected)
            self.assertEqual(len(revision), 40)
            self.assertFalse(folder.is_relative_to(ROOT))
            self.assertEqual(self.provenance.code_provenance(folder, revision)["git_commit"], revision)
        self.assertFalse(folder.exists())
        self.assertEqual(self.provenance.git_output(ROOT, "rev-parse", "HEAD"), head_before)

    def test_file_changes_change_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "main.py"
            path.write_text("version = 1\n", encoding="utf-8")
            before = self.provenance.code_provenance(folder, "test")
            path.write_text("version = 2\n", encoding="utf-8")
            after = self.provenance.code_provenance(folder, "test")
            self.assertNotEqual(before["content_sha256"], after["content_sha256"])

    def test_environments_and_generated_directories_are_pruned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "smart_ai_gateway/services/rules.py"
            source.parent.mkdir(parents=True)
            source.write_text("enabled = True\n", encoding="utf-8")
            ignored = []
            for name in (".venv/Lib/site-packages/pkg", "__pycache__", "results", "custom-python/pkg"):
                path = root / "smart_ai_gateway" / name / "ignored.py"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("version = 1\n", encoding="utf-8")
                ignored.append(path)
            (root / "smart_ai_gateway/custom-python/pyvenv.cfg").write_text("home = unused\n", encoding="utf-8")
            before = self.provenance.code_provenance(root, "test")
            self.assertEqual(set(before["files_sha256"]), {"smart_ai_gateway/services/rules.py"})
            for path in ignored:
                path.write_text("version = 2\n", encoding="utf-8")
            self.assertEqual(before, self.provenance.code_provenance(root, "test"))

    def test_loaded_models_report_resolved_revisions_without_guessing(self):
        embedding_revision, verifier_revision = "a" * 40, "b" * 40
        transformer = SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=embedding_revision)))
        cache = SimpleNamespace(
            _cfg=SimpleNamespace(embedding_model="example/embedding", verifier_model="example/verifier"),
            embedding_model=SimpleNamespace(_modules={"0": transformer}),
            verifier=SimpleNamespace(model=SimpleNamespace(config=SimpleNamespace(_commit_hash=verifier_revision))),
        )
        actual = self.provenance.loaded_model_provenance(cache)
        self.assertEqual(actual["embedding"]["resolved_revisions"], [embedding_revision])
        self.assertEqual(actual["verifier"]["resolved_revisions"], [verifier_revision])
        cache.embedding_model = SimpleNamespace(tokenizer=SimpleNamespace(init_kwargs={"revision": "main"}))
        cache.verifier = None
        actual = self.provenance.loaded_model_provenance(cache)
        self.assertEqual(actual["embedding"]["revision_status"], "unavailable")
        self.assertEqual(actual["embedding"]["resolved_revisions"], [])
        self.assertEqual(actual["verifier"]["revision_status"], "not_loaded")
        cache.embedding_model = SimpleNamespace(name_or_path=f"C:/cache/models--example/snapshots/{embedding_revision}/")
        self.assertEqual(self.provenance.loaded_model_provenance(cache)["embedding"]["resolved_revisions"], [embedding_revision])


class OfflineReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bench = load_module("eval_bench_tests", ROOT / "eval/cost/bench.py")
        cls.replay = load_module("eval_replay_tests", ROOT / "eval/cost/replay.py")
        cls.provenance = load_module("eval_provenance_tests", ROOT / "eval/cost/provenance.py")
        cls.workload = load_module("eval_workload_tests", ROOT / "eval/cost/workload.py")

    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "main.py").write_text("# Fixture source, never imported\n", encoding="utf-8")
        self.harness = self.root / "harness"
        self.harness.mkdir()
        for name in ("bench.py", "workload.py", "replay.py", "provenance.py"):
            (self.harness / name).write_bytes((ROOT / "eval/cost" / name).read_bytes())
        self.intents, self.recordings = self.root / "intents.json", self.root / "recordings.jsonl"
        self.intents.write_bytes((ROOT / "eval/cost/fixtures/intents.json").read_bytes())
        self.recordings.write_bytes((ROOT / "eval/cost/fixtures/real_answers.jsonl").read_bytes())
        self.args = SimpleNamespace(
            code_root=None, code_revision="test", new_root=str(self.root), intents=str(self.intents),
            recordings=str(self.recordings), out=str(self.root / "output"), seeds="1", sizes="8",
            days=1, lam=1.0, zipf_s="1.0", paraphrase_share="0.5", personal_share=0.05,
            time_share=0.03, variation=0.5, isolation="per_company", torch_threads=1,
            env=[], force=False, decision_log_dir=None,
        )
        self.stack.enter_context(patch.dict(sys.modules, {
            "provenance": self.provenance, "workload": self.workload, "replay": self.replay,
        }))
        self.stack.enter_context(patch.object(self.bench, "HERE", self.harness))
        self.stack.enter_context(patch.object(sys, "path", list(sys.path)))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def test_import_time_qdrant_client_cannot_connect_to_remote_service(self):
        import qdrant_client
        from qdrant_client.qdrant_remote import QdrantRemote
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
                patch.object(QdrantRemote, "__init__", side_effect=AssertionError("remote client forbidden")), \
                self.bench._in_memory_qdrant_imports():
            client = qdrant_client.QdrantClient(url="https://not-a-real-service.invalid", timeout=1)
            self.assertEqual(client.get_collections().collections, [])
            client.close()

    def test_recorded_incomplete_answer_is_not_cacheable(self):
        record = {"intent_id": "partial", "answer": "A partial answer", "prompt_tokens": 10,
                  "output_tokens": 5, "thinking_tokens": 0, "latency_s": 1,
                  "finish_reason": "MAX_TOKENS"}
        self.recordings.write_text(json.dumps(record) + "\n", encoding="utf-8")
        model = self.replay.ReplayLLM(self.recordings)
        model.hint = "partial"
        answer = model("question")
        self.assertFalse(answer.cacheable)
        self.assertEqual(answer.finish_reason, "MAX_TOKENS")
        self.assertEqual(answer.usage["output_tokens"], 5)
        self.assertEqual(len(model.calls), 1)
        self.assertTrue(self.replay.Answer("complete", {}, "STOP").cacheable)
        self.assertFalse(self.replay.Answer("safety-stopped", {}, "FinishReason.SAFETY").cacheable)

    def test_fixture_nocache_run_writes_valid_metadata_and_resumes(self):
        with patch.object(self.bench, "_setup_app", side_effect=AssertionError("app import forbidden")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
            self.bench.run_jobs(self.args, "nocache")
            csv_path, = Path(self.args.out).glob("*.csv")
            before = csv_path.read_bytes()
            metadata = json.loads(csv_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreater(len(rows), 0)
            self.assertEqual(metadata["schema_version"], 2)
            self.assertEqual(metadata["request_count"], len(rows))
            self.assertEqual(metadata["csv_sha256"], self.provenance.sha256_file(csv_path))
            self.assertEqual(metadata["identity"]["loaded_models"], {})
            self.bench.run_jobs(self.args, "nocache")
            self.assertEqual(csv_path.read_bytes(), before)
            csv_path.write_bytes(before + b"\n")
            with self.assertRaisesRegex(ValueError, "was modified"):
                self.bench.run_jobs(self.args, "nocache")

    def test_mid_run_input_and_harness_edits_are_rejected_before_output(self):
        original = self.bench.run_nocache
        cases = [(self.intents, "Benchmark input files"), (self.recordings, "Benchmark input files"),
                 (self.harness / "workload.py", "Benchmark harness")]
        for path, reason in cases:
            with self.subTest(path=path.name):
                contents = path.read_bytes()

                def change_during_run(*args, **kwargs):
                    rows = original(*args, **kwargs)
                    path.write_bytes(contents + b"\n")
                    return rows

                with patch.object(self.bench, "run_nocache", side_effect=change_during_run):
                    with self.assertRaisesRegex(RuntimeError, reason + " changed"):
                        self.bench.run_jobs(self.args, "nocache")
                self.assertFalse(list(Path(self.args.out).glob("*.csv")))
                self.assertFalse(list(Path(self.args.out).glob("*.meta.json")))
                path.write_bytes(contents)


if __name__ == "__main__":
    unittest.main()
