"""Verify and package the completed paper_v1 runs without running inference.

    python eval/cost/snapshot.py
    python eval/cost/snapshot.py --check

Raw CSVs and their metadata are read-only. Run analyze.py first when updating
derived tables. This records current analysis/pricing hashes separately from
the original benchmark provenance; it does not certify prices or model weights.
"""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

from provenance import digest, sha256_file

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
THRESHOLDS = ("0.90", "0.95", "0.97", "0.99")
RUNS = ("main", *(f"threshold_{t}" for t in THRESHOLDS))
SOURCES = ("eval/cost/analyze.py", "eval/cost/bench.py", "eval/cost/workload.py",
           "eval/cost/replay.py", "eval/cost/provenance.py", "eval/cost/snapshot.py",
           "eval/cost/prices.json", "eval/cost/data/intents.json", "eval/cost/data/real_answers.jsonl")
REPORTS = ("summary.md", "summary.csv", "economics.csv", "hit_wrong_rates.png", "savings_vs_volume.png")
ROOT_OUTPUTS = ("README.md", "comparison.json", "comparison.png")
STREAM_FIELDS = ("seed", "size", "company", "user", "idx", "t", "kind", "intent", "prompt")


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def normalized_hash(path):
    """Source checkout line endings can vary across Windows and Linux."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def numeric(row):
    result = {}
    for key, value in row.items():
        try:
            number = float(value)
            result[key] = number if math.isfinite(number) else None
        except ValueError:
            result[key] = value
    return result


def audit(root):
    totals, per_seed, errors, streams, outcomes, provenance = {}, {}, [], {}, {}, []
    paired_files = 0
    for run in RUNS:
        configs = ("nocache", "old", "new") if run == "main" else ("new",)
        expected = {(config, seed) for config in configs for seed in range(1, 6)}
        seen = set()
        paths = sorted((root / run / "raw").glob("*.csv"))
        if len(paths) != len(expected):
            raise ValueError(f"{run}: expected {len(expected)} CSVs, found {len(paths)}")
        for path in paths:
            meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
            identity = meta["identity"]
            if meta["csv_sha256"] != sha256_file(path) or meta["identity_sha256"] != digest(identity):
                raise ValueError(f"Fingerprint mismatch: {path}")
            rows = read_csv(path)
            key = (identity["config"], identity["seed"])
            if key not in expected or key in seen or len(rows) != meta["request_count"]:
                raise ValueError(f"Unexpected/duplicate seed or row count: {path}")
            seen.add(key)
            paired_files += 1
            if run != "main":
                threshold = float(run.removeprefix("threshold_"))
                settings = meta["effective_settings"]["semantic_cache"]
                if (settings["similarity_threshold"], settings["verify_threshold"], settings["verifier_threshold"]) != (threshold, 0.75, 0.5):
                    raise ValueError(f"Unexpected threshold settings: {path}")
            stream_hash = digest([[row[field] for field in STREAM_FIELDS] for row in rows])
            pair = ("main" if run == "main" else "threshold", identity["seed"])
            if pair in streams and streams[pair] != stream_hash:
                raise ValueError(f"Unpaired request stream: {path}")
            streams[pair] = stream_hash
            outcome_hash = digest([[r[k] for k in ("status", "hit", "served_intent", "correct")] for r in rows])
            outcomes[(run, identity["seed"])] = outcome_hash
            counts = dict(requests=len(rows), hits=0, correct_hits=0, wrong_hits=0, http_errors=0, llm_calls=0)
            for row in rows:
                success = int(row["status"]) == 200
                hit = success and row["hit"] == "1"
                counts["hits"] += int(hit)
                counts["correct_hits"] += int(hit and row["correct"] == "1")
                counts["wrong_hits"] += int(hit and row["correct"] != "1")
                counts["http_errors"] += int(not success)
                counts["llm_calls"] += int(row["llm_calls"])
                if not success:
                    errors.append({"run": run, "file": path.relative_to(root).as_posix(),
                                   **{k: row[k] for k in ("seed", "idx", "intent", "status", "gw_wall_ms", "cpu_s")}})
            label = key[0] if run == "main" else run
            total = totals.setdefault(label, dict.fromkeys(counts, 0))
            for field, value in counts.items():
                total[field] += value
            per_seed.setdefault(label, []).append({"seed": key[1], **counts})
            provenance.append({"run": run, "config": key[0], "seed": key[1],
                               "source_commit": identity["code"]["git_commit"],
                               "source_content_sha256": identity["code"]["content_sha256"],
                               "inputs": identity["inputs"], "loaded_models": identity.get("loaded_models", {})})
        if seen != expected:
            raise ValueError(f"Missing jobs in {run}")
        for name in REPORTS:
            if not (root / run / name).is_file():
                raise ValueError(f"Missing derived report: {run}/{name}")
    summaries = []
    for threshold in THRESHOLDS:
        run = f"threshold_{threshold}"
        summary = read_csv(root / run / "summary.csv")
        if len(summary) != 1:
            raise ValueError(f"Expected one threshold summary row: {run}")
        summaries.append({"threshold": float(threshold), "pooled": totals[run],
                          "mean_over_seeds": numeric(summary[0]), "per_seed": per_seed[run]})
    identical = all(outcomes[("threshold_0.95", seed)] == outcomes[(run, seed)]
                    for run in ("threshold_0.97", "threshold_0.99") for seed in range(1, 6))
    return {"schema_version": 1, "raw_pairs_verified": paired_files, "main_pooled": {
        key: totals[key] for key in ("nocache", "old", "new")}, "thresholds": summaries,
        "threshold_095_097_099_identical_outcomes": identical, "http_failures": errors,
        "benchmark_provenance": provenance}


def make_plot(root, comparison):
    import os
    os.environ.setdefault("MPLCONFIGDIR", str(REPO / ".pytest_cache" / "matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
    for ax, (field, label) in zip(axes, (("hit_rate", "Cache hits / all requests (%)"),
                                       ("wrong_given_hit", "Wrong intent / cache hits (%)"))):
        means, all_rates = [], []
        for x, result in enumerate(comparison["thresholds"]):
            rates = []
            for seed in sorted(result["per_seed"], key=lambda r: r["seed"]):
                rates.append(100 * (seed["hits"] / seed["requests"] if field == "hit_rate"
                                   else seed["wrong_hits"] / seed["hits"]))
            all_rates.extend(rates)
            ax.scatter([x + offset for offset in (-.12, -.06, 0, .06, .12)], rates,
                       s=28, color="#3b82b6", alpha=.65, clip_on=False,
                       label="Individual seeds" if x == 0 else None)
            means.append(100 * result["mean_over_seeds"][field])
        ax.plot(range(4), means, color="#172f4d", marker="D", markersize=4, label="Mean of 5 seeds")
        ax.set_xticks(range(4), ("0.90*", "0.95", "0.97", "0.99"))
        ax.set_xlabel("Direct-match threshold")
        ax.set_ylabel(label)
        ax.set_ylim(0, max(all_rates) * 1.10)
        ax.grid(axis="y", color="#e2e5e9", linewidth=.7)
        ax.set_axisbelow(True)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].legend(frameon=False, fontsize=9, loc="lower right")
    fig.suptitle("Threshold comparison: 50 employees, 20 simulated days", fontsize=12)
    fig.text(.07, .025, "*0.90 includes one HTTP 504; raw observation retained. Points show seed variation, not confidence bounds.", fontsize=8)
    fig.tight_layout(rect=(0, .065, 1, .95))
    fig.savefig(root / "comparison.png", dpi=220)
    plt.close(fig)


def snapshot_files(root):
    files = [root / name for name in ROOT_OUTPUTS]
    for run in RUNS:
        files.extend(root / run / name for name in REPORTS)
        files.extend(sorted((root / run / "raw").glob("*.csv")))
        files.extend(sorted((root / run / "raw").glob("*.meta.json")))
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=HERE / "results" / "paper_v1")
    parser.add_argument("--check", action="store_true", help="verify the snapshot without rewriting any files")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = root / "snapshot_manifest.json"
    if args.check:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for name, info in manifest["files"].items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or sha256_file(path) != info["sha256"]:
                raise ValueError(f"Snapshot artifact changed: {name}")
        for name, fingerprint in manifest["source_sha256_lf"].items():
            if name not in SOURCES or normalized_hash(REPO / name) != fingerprint:
                raise ValueError(f"Analysis/input source changed: {name}")
        comparison = audit(root)
        print(f"Verified {len(manifest['files'])} artifacts and {comparison['raw_pairs_verified']} raw CSV/metadata pairs")
        return
    comparison = audit(root)
    (root / "comparison.json").write_text(json.dumps(comparison, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    make_plot(root, comparison)
    manifest = {"schema_version": 1, "scope": "main plus four threshold runs; pilot and temporary runs excluded",
                "raw_pairs_verified": comparison["raw_pairs_verified"],
                "analysis_note": "Derived reports regenerated with failed-delivery accounting; raw observations unchanged",
                "source_sha256_lf": {name: normalized_hash(REPO / name) for name in SOURCES},
                "files": {path.relative_to(root).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
                          for path in snapshot_files(root)}}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Packaged {len(manifest['files'])} artifacts; manifest excludes itself")


if __name__ == "__main__":
    main()
