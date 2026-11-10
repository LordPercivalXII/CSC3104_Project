"""Extra article outputs: wrong_examples.csv, stage_breakdown.{csv,md}, paraphrase_effect.md.
Usage: python extras.py --raw results/raw_main --decisions results/decisions/main --results results"""
import argparse, csv, glob, json, os, re
from collections import Counter, defaultdict
ap = argparse.ArgumentParser()
ap.add_argument("--raw", default="results/raw_main"); ap.add_argument("--decisions", default="results/decisions/main")
ap.add_argument("--results", default="results"); a = ap.parse_args()
def rd(p): return list(csv.DictReader(open(p, encoding="utf-8")))
def ex(r):
    try: return json.loads(r["extra"] or "{}")
    except Exception: return {}
# ---- wrong examples
out, cnt, hits, reqs = [], Counter(), Counter(), Counter()
for f in sorted(glob.glob(f"{a.raw}/*.csv")):
    if os.path.basename(f).startswith("nocache"): continue
    for r in rd(f):
        reqs[r["config"]] += 1
        if r["hit"] != "1": continue
        hits[r["config"]] += 1
        if r["correct"] == "1": continue
        e = ex(r); cnt[r["config"]] += 1
        out.append({"config": r["config"], "seed": r["seed"], "size": r["size"], "company": r["company"], "kind": r["kind"],
                    "prompt": r["prompt"], "true_intent": r["intent"], "served_intent": r["served_intent"],
                    "match_type": r["match_type"], "similarity": e.get("similarity_score"), "effective_score": e.get("effective_score"),
                    "verifier_score": e.get("verifier_score"), "confusable_error": r["confusable_error"]})
with open(f"{a.results}/wrong_examples.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys())); w.writeheader(); w.writerows(out)
md = ["# Wrong cache hits (main grid, all seeds and sizes)", ""]
for c in cnt: md.append(f"* {c}: {cnt[c]} wrong hits of {hits[c]} hits, {reqs[c]} requests")
by = Counter((o["config"], o["kind"], o["match_type"], o["confusable_error"]) for o in out)
md += ["", "config, kind, match_type, confusable -> count", ""] + [f"* {k}: {v}" for k, v in sorted(by.items())]
open(f"{a.results}/wrong_summary.md", "w", encoding="utf-8").write("\n".join(md))
print("\n".join(md))
# ---- stage breakdown (new only)
stages, skips, faq_skip, faq_total, nrun = Counter(), Counter(), Counter(), 0, 0
qh = defaultdict(lambda: [[0, 0] for _ in range(4)])  # per kind faq: quarter -> [hits, n]
dp = Counter()
for f in sorted(glob.glob(f"{a.raw}/new__*.csv")):
    rows = rd(f)
    base = os.path.basename(f).replace(".csv", ".jsonl")
    lp = f"{a.decisions}/decisions_{base}"
    log = [json.loads(l) for l in open(lp, encoding="utf-8") if l.strip()]
    assert len(log) == len(rows), (lp, len(log), len(rows))
    nrun += 1
    for r, d in zip(rows, log):
        st = d["stage"] if d["stage"] != "skipped" else "skipped"
        stages[st] += 1
        if st == "skipped": skips[(r["kind"], re.sub(r"\(.*\)", "(..)", d["skip_reason"] or ""))] += 1
        if r["kind"] == "faq":
            faq_total += 1
            if st == "skipped": faq_skip[d["skip_reason"]] += 1
    # warm-up: per company, faq requests split by time quarter
    comp = defaultdict(list)
    for r in rows:
        if r["kind"] == "faq": comp[(r["size"], r["company"])].append(r)
    for k, rs in comp.items():
        n = len(rs)
        for i, r in enumerate(rs):
            q = min(3, i * 4 // n); qh[r["size"]][q][0] += int(r["hit"]); qh[r["size"]][q][1] += 1
    # paraphrase-point heuristic
    stored, para = defaultdict(set), defaultdict(set)
    cur = None
    for r, d in zip(rows, log):
        key = (r["size"], r["company"]); p = r["prompt"]
        if r["hit"] == "1":
            if d["stage"] == "verified_hit": dp["verified_hit"] += 1; para[key].add(p)
            elif p in para[key]: dp["direct_on_stored_paraphrase_text"] += 1
            else: dp["direct_other"] += 1
        elif d["stored"]: stored[key].add(p)
tot = sum(stages.values())
rows_out = [{"stage": k, "requests": v, "share": round(v / tot, 4)} for k, v in stages.most_common()]
rows_out += [{"stage": f"skipped[{k[0]}]: {k[1]}", "requests": v, "share": round(v / tot, 4)} for k, v in skips.most_common()]
with open(f"{a.results}/stage_breakdown.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=["stage", "requests", "share"]); w.writeheader(); w.writerows(rows_out)
m = [f"# Stage breakdown (new, {nrun} runs, {tot} requests)", "", "| stage | requests | share |", "|---|---|---|"]
m += [f"| {r['stage']} | {r['requests']} | {r['share']:.2%} |" for r in rows_out]
fs = sum(faq_skip.values())
m += ["", f"## FAQ (non-personal, non-time) requests skipped by the eligibility filter: {fs} of {faq_total} ({fs / faq_total:.2%})", "",
      "| skip reason | FAQ requests | share of FAQ |", "|---|---|---|"]
m += [f"| {k} | {v} | {v / faq_total:.2%} |" for k, v in faq_skip.most_common()]
m += ["", "## Warm-up: FAQ hit rate by quarter of each company's FAQ stream (new)", "", "| size | Q1 | Q2 | Q3 | Q4 |", "|---|---|---|---|---|"]
for s in sorted(qh, key=int): m.append(f"| {s} | " + " | ".join(f"{h / n:.1%}" for h, n in qh[s]) + " |")
th = sum(dp.values())
m += ["", "## Hit sources (new)", "", f"* verified hits (grey zone, stored as paraphrase afterwards): {dp['verified_hit']} ({dp['verified_hit'] / th:.1%} of hits)",
      f"* direct hits whose exact wording was stored earlier as a verified paraphrase (lower bound of paraphrase-point hits): {dp['direct_on_stored_paraphrase_text']} ({dp['direct_on_stored_paraphrase_text'] / th:.1%})",
      f"* other direct hits (original point or repeated wording): {dp['direct_other']} ({dp['direct_other'] / th:.1%})"]
open(f"{a.results}/stage_breakdown.md", "w", encoding="utf-8").write("\n".join(m))
print("\n".join(m))
