"""
Aggregate raw benchmark CSVs into summary tables and plots.

Method
  * One "run" = one (config, seed, zipf_s, paraphrase_share, company size). Every metric is first
    computed per run (pooling the requests of all companies of that size in the seed), then the
    mean and a 95% confidence interval are taken ACROSS SEEDS (Student t, df = seeds - 1).
    With a single seed no CI is available (shown as nan).
  * Savings are priced from the recorded token usage of the requests the cache answered:
    saved = (tokens the request WOULD have cost) - (tokens actually sent to the LLM).
    saved_correct counts only hits that served the right answer (the headline number: a wrong
    answer is not a saving). saved_gross also counts wrong hits.
    A failed request receives no avoided-call, token or cost credit. Actual inference work on
    failed requests is still charged as waste. Token fractions retain the full request-stream
    baseline denominator, so failed delivery cannot inflate the reported benefit.
  * Thinking tokens are billed per the price entry (thinking_billed_as: output | input | none).
  * Requests/month for a company = size * lam * workdays_per_month. The hit rate measured over the
    simulated D days (cold start included) is assumed to hold for the month.
  * Gateway overhead: measured CPU-seconds per request (minus the test-harness baseline) times
    hosting $/vCPU-second ("marginal"), PLUS the fixed monthly cost of the always-on deployment
    (nodes needed for the pod requests, from k8s/*.yaml by default). Using both is conservative
    (the fixed cost already pays for the CPU); use --fixed-only to drop the marginal term.
  * Break-even requests/month R* = fixed / (saved_correct_per_request - cpu_cost_per_request).
  * Latency: hits = measured gateway wall time; misses = gateway wall time + RECORDED real LLM
    latency; nocache = recorded LLM latency only.

Outputs (under --results): summary.csv, economics.csv, summary.md, *.png
"""
import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
       2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
       2.045, 2.042]
HOURS_PER_MONTH = 730.0


def tcrit(df):
    try:
        from scipy import stats
        return float(stats.t.ppf(0.975, df))
    except Exception:
        return T95[df - 1] if df <= 30 else 1.96 + 2.4 / df


def mean_ci(xs):
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not xs:
        return float("nan"), float("nan")
    m = statistics.fmean(xs)
    if len(xs) < 2:
        return m, float("nan")
    return m, tcrit(len(xs) - 1) * statistics.stdev(xs) / math.sqrt(len(xs))


def fmt(m, ci, digits=3, pct=False):
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return "n/a"
    k = 100 if pct else 1
    s = f"{m * k:.{digits}f}"
    if not (isinstance(ci, float) and math.isnan(ci)):
        s += f" ± {ci * k:.{digits}f}"
    return s + ("%" if pct else "")


def pct_q(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


# ------------------------------------------------------------------ loading
def load_raw(raw_dir):
    rows = []
    for p in sorted(Path(raw_dir).glob("*.csv")):
        with open(p, newline="", encoding="utf-8") as f:
            rows += list(csv.DictReader(f))
    num = ["seed", "size", "lam", "days", "status", "hit", "correct", "confusable_error", "gw_wall_ms", "cpu_s",
           "cpu_baseline_s", "llm_calls", "llm_prompt_tokens", "llm_output_tokens", "llm_thinking_tokens",
           "llm_latency_s", "base_prompt_tokens", "base_output_tokens", "base_thinking_tokens", "base_latency_s",
           "synthetic"]
    for r in rows:
        for k in num:
            r[k] = float(r[k]) if r[k] != "" else 0.0
        r["seed"], r["size"] = int(r["seed"]), int(r["size"])
    return rows


def token_cost(prompt, output, thinking, price):
    how = price.get("thinking_billed_as", "output")
    inp = prompt + (thinking if how == "input" else 0)
    out = output + (thinking if how == "output" else 0)
    return (inp * price["input_per_mtok"] + out * price["output_per_mtok"]) / 1e6


def run_metrics(rs, prices):
    """Metrics of one run (list of request rows)."""
    n = len(rs)
    ok = [r for r in rs if r["status"] == 200]
    hits = [r for r in ok if r["hit"]]
    wrong = [r for r in hits if not r["correct"]]
    right = [r for r in hits if r["correct"]]
    m = {"n": n, "error_rate": 1 - len(ok) / n, "hit_rate": len(hits) / n, "correct_hit_rate": len(right) / n,
         "wrong_rate": len(wrong) / n, "wrong_given_hit": len(wrong) / len(hits) if hits else float("nan"),
         "confusable_wrong_rate": sum(r["confusable_error"] for r in hits) / n,
         "llm_calls": sum(r["llm_calls"] for r in rs), "llm_calls_avoided": len(ok) - sum(r["llm_calls"] for r in rs),
         "synthetic_share": sum(r["synthetic"] for r in rs) / n,
         "cpu_per_req_s": statistics.fmean(max(r["cpu_s"] - r["cpu_baseline_s"], 0.0) for r in rs)}
    base_tok = sum(r["base_prompt_tokens"] + r["base_output_tokens"] + r["base_thinking_tokens"] for r in rs)
    delivered_base_tok = sum(r["base_prompt_tokens"] + r["base_output_tokens"] + r["base_thinking_tokens"] for r in ok)
    act_tok = sum(r["llm_prompt_tokens"] + r["llm_output_tokens"] + r["llm_thinking_tokens"] for r in rs)
    saved_tok = delivered_base_tok - act_tok
    m["tokens_saved"], m["tokens_saved_frac"] = saved_tok, saved_tok / base_tok if base_tok else 0
    hl = [r["gw_wall_ms"] for r in hits]
    ml = [r["gw_wall_ms"] + r["llm_latency_s"] * 1000 for r in ok if not r["hit"]]
    al = hl + ml
    m.update(lat_hit_median_ms=pct_q(hl, .5), lat_hit_p95_ms=pct_q(hl, .95), lat_miss_median_ms=pct_q(ml, .5),
             lat_miss_p95_ms=pct_q(ml, .95), lat_all_median_ms=pct_q(al, .5), lat_all_p95_ms=pct_q(al, .95))
    # $ per request for each LLM price (gross and correct-only)
    for i, p in enumerate(prices["llm"]):
        g = c = 0.0
        for r in rs:
            actual_cost = token_cost(r["llm_prompt_tokens"], r["llm_output_tokens"], r["llm_thinking_tokens"], p)
            baseline_cost = token_cost(r["base_prompt_tokens"], r["base_output_tokens"], r["base_thinking_tokens"], p) if r["status"] == 200 else 0
            s = baseline_cost - actual_cost
            g += s
            if r["status"] == 200 and (not r["hit"] or r["correct"]):
                c += s
            else:
                # Wrong reuse and failed delivery earn no benefit, but their
                # actual provider spend must not disappear from the accounting.
                c -= actual_cost
        m[f"saved_gross_per_req|{i}"], m[f"saved_correct_per_req|{i}"] = g / n, c / n
    return m


def deployment_nodes(h, a):
    cpu = a.gateway_replicas * a.gateway_cpu + a.qdrant_cpu + a.redis_cpu
    mem = a.gateway_replicas * a.gateway_mem_gb + a.qdrant_mem_gb + a.redis_mem_gb
    cpu_av, mem_av = h["vcpu"] - a.node_reserved_cpu, h["ram_gb"] - a.node_reserved_mem_gb
    return max(1, math.ceil(max(cpu / cpu_av, mem / mem_av)))


def hosting_label(h):
    return f"{h['provider']} {h['instance']} {h.get('region', '')}".strip()


def fixed_monthly(h, a, prices):
    nodes = deployment_nodes(h, a)
    cost = nodes * h["usd_per_hour"] * HOURS_PER_MONTH
    if a.include_cluster_fee:
        fees = [f["usd_per_hour"] for f in prices.get("cluster_fees", []) if f["provider"] == h["provider"]]
        cost += (fees[0] if fees else 0) * HOURS_PER_MONTH
    return nodes, cost


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description="Aggregate benchmark results")
    ap.add_argument("--raw", default=str(HERE / "results" / "raw"))
    ap.add_argument("--results", default=str(HERE / "results"))
    ap.add_argument("--prices", default=str(HERE / "prices.json"))
    ap.add_argument("--workdays-per-month", type=float, default=22)
    ap.add_argument("--fixed-only", action="store_true", help="drop the marginal CPU-seconds cost term")
    ap.add_argument("--include-cluster-fee", action="store_true", help="add the managed-Kubernetes control plane fee")
    ap.add_argument("--plot-hosting", default="", help="substring of the hosting label to use in plots (default: cheapest)")
    # deployment shape; defaults = what k8s/*.yaml requests
    ap.add_argument("--gateway-replicas", type=int, default=3)
    ap.add_argument("--gateway-cpu", type=float, default=0.25)
    ap.add_argument("--gateway-mem-gb", type=float, default=0.75)
    ap.add_argument("--qdrant-cpu", type=float, default=0.1)
    ap.add_argument("--qdrant-mem-gb", type=float, default=0.25)
    ap.add_argument("--redis-cpu", type=float, default=0.05)
    ap.add_argument("--redis-mem-gb", type=float, default=0.0625)
    ap.add_argument("--node-reserved-cpu", type=float, default=0.5, help="CPU kept by kubelet/system pods")
    ap.add_argument("--node-reserved-mem-gb", type=float, default=1.0)
    a = ap.parse_args()

    prices = json.loads(Path(a.prices).read_text(encoding="utf-8"))
    raw = load_raw(a.raw)
    if not raw:
        raise SystemExit(f"no CSVs in {a.raw}")
    out = Path(a.results)
    out.mkdir(parents=True, exist_ok=True)

    # ---- per-run metrics
    runs = defaultdict(list)
    for r in raw:
        runs[(r["config"], r["zipf_s"], r["para_share"], r["size"], r["seed"])].append(r)
    per_run = {k: run_metrics(v, prices) for k, v in runs.items()}
    groups = defaultdict(list)  # (config, zs, ps, size) -> [(seed, metrics)]
    for (c, zs, ps, size, seed), m in per_run.items():
        groups[(c, zs, ps, size)].append(m)
    lam = float(raw[0]["lam"])

    # ---- summary.csv
    rate_keys = ["hit_rate", "correct_hit_rate", "wrong_rate", "wrong_given_hit", "confusable_wrong_rate", "error_rate",
                 "tokens_saved_frac", "llm_calls_avoided", "cpu_per_req_s", "synthetic_share", "lat_hit_median_ms",
                 "lat_hit_p95_ms", "lat_miss_median_ms", "lat_miss_p95_ms", "lat_all_median_ms", "lat_all_p95_ms"]
    agg = {}
    srows = []
    for key in sorted(groups):
        ms = groups[key]
        row = {"config": key[0], "zipf_s": key[1], "para_share": key[2], "size": key[3], "seeds": len(ms),
               "requests_per_seed": statistics.fmean(m["n"] for m in ms)}
        for k in rate_keys:
            mean, ci = mean_ci([m[k] for m in ms])
            row[k], row[k + "_ci95"] = mean, ci
        agg[key] = row
        srows.append(row)
    with open(out / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(srows[0]))
        w.writeheader()
        w.writerows(srows)

    # ---- economics
    hostings = prices["hosting"]
    erows = []
    for key in sorted(groups):
        config, zs, ps, size = key
        if config == "nocache":
            continue
        reqs_month = size * lam * a.workdays_per_month
        for i, p in enumerate(prices["llm"]):
            for h in hostings:
                nodes, fixed = fixed_monthly(h, a, prices)
                usd_cpu_s = h["usd_per_hour"] / h["vcpu"] / 3600
                per_seed = []
                for m in groups[key]:
                    cpu_cost = 0.0 if a.fixed_only else m["cpu_per_req_s"] * usd_cpu_s
                    sc, sg = m[f"saved_correct_per_req|{i}"], m[f"saved_gross_per_req|{i}"]
                    margin = sc - cpu_cost
                    per_seed.append({"saved_gross_per_1k": sg * 1000, "saved_correct_per_1k": sc * 1000,
                                     "cpu_cost_per_1k": cpu_cost * 1000, "saved_correct_month": sc * reqs_month,
                                     "net_month": margin * reqs_month - fixed,
                                     "breakeven_req_month": fixed / margin if margin > 0 else float("inf")})
                row = {"config": config, "zipf_s": zs, "para_share": ps, "size": size, "llm": p["name"],
                       "llm_input_per_mtok": p["input_per_mtok"], "llm_output_per_mtok": p["output_per_mtok"],
                       "hosting": hosting_label(h), "nodes": nodes, "fixed_month_usd": fixed,
                       "requests_per_month": reqs_month, "seeds": len(per_seed)}
                for k in per_seed[0]:
                    vals = [d[k] for d in per_seed]
                    if k == "breakeven_req_month" and any(math.isinf(v) for v in vals):
                        row[k], row[k + "_ci95"] = float("inf"), float("nan")
                    else:
                        row[k], row[k + "_ci95"] = mean_ci(vals)
                erows.append(row)
    if erows:
        with open(out / "economics.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(erows[0]))
            w.writeheader()
            w.writerows(erows)

    # ---- summary.md
    md = ["# Benchmark summary", "",
          f"Raw runs: {len(per_run)} (config x seed x params x size); prices retrieved {prices.get('retrieved')}. "
          f"Values are mean ± 95% CI across seeds (t-distribution).", "",
          f"Workload: lam = {lam} questions/employee/day; month = {a.workdays_per_month:g} working days. "
          f"Deployment: {a.gateway_replicas} gateway pods ({a.gateway_cpu} vCPU, {a.gateway_mem_gb} GB each) + Qdrant + Redis; "
          f"node reserve {a.node_reserved_cpu} vCPU / {a.node_reserved_mem_gb} GB; "
          f"{'fixed cost only' if a.fixed_only else 'fixed cost + marginal CPU cost'}.", ""]
    md += ["## Hit rate and reliability", "",
           "| config | zipf s | para share | company size | seeds | hit rate | correct hits / all | wrong / all | wrong / hits | tokens saved | error rate |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in srows:
        md.append(f"| {r['config']} | {r['zipf_s']} | {r['para_share']} | {r['size']} | {r['seeds']} | "
                  f"{fmt(r['hit_rate'], r['hit_rate_ci95'], 1, True)} | {fmt(r['correct_hit_rate'], r['correct_hit_rate_ci95'], 1, True)} | "
                  f"{fmt(r['wrong_rate'], r['wrong_rate_ci95'], 2, True)} | {fmt(r['wrong_given_hit'], r['wrong_given_hit_ci95'], 2, True)} | "
                  f"{fmt(r['tokens_saved_frac'], r['tokens_saved_frac_ci95'], 1, True)} | {fmt(r['error_rate'], r['error_rate_ci95'], 2, True)} |")
    md += ["", "## Latency (ms)", "",
           "| config | zipf s | para share | size | hit median | hit p95 | miss median | miss p95 | overall median | overall p95 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in srows:
        L = lambda k: fmt(r[k], r[k + "_ci95"], 0)  # noqa: E731
        md.append(f"| {r['config']} | {r['zipf_s']} | {r['para_share']} | {r['size']} | {L('lat_hit_median_ms')} | {L('lat_hit_p95_ms')} | "
                  f"{L('lat_miss_median_ms')} | {L('lat_miss_p95_ms')} | {L('lat_all_median_ms')} | {L('lat_all_p95_ms')} |")
    md += ["", "Gateway CPU per request (harness baseline removed): " + "; ".join(
        f"{r['config']} size {r['size']} s={r['zipf_s']} p={r['para_share']}: {r['cpu_per_req_s'] * 1000:.1f} ms"
        for r in srows if r["config"] != "nocache"), ""]
    if erows:
        plot_h = None
        cands = [hosting_label(h) for h in hostings if a.plot_hosting in hosting_label(h)]
        if cands:
            plot_h = min(cands, key=lambda l: next(h["usd_per_hour"] for h in hostings if hosting_label(h) == l))
        md += ["## Economics", "",
               "Savings count correct hits only (`saved gross` also counts wrong hits). Failed requests receive no savings credit; any actual inference spend on them is charged as waste. Net = savings - gateway CPU - fixed deployment cost, per company-month.", ""]
        for h_label in ([plot_h] if plot_h else []):
            hh = next(h for h in hostings if hosting_label(h) == h_label)
            nodes, fixed = fixed_monthly(hh, a, prices)
            md += [f"### Hosting: {h_label} ({nodes} node(s), fixed ${fixed:,.2f}/month, ${hh['usd_per_hour']}/h)", ""]
            md += ["(Full grid of all hosting options: economics.csv)", ""]
            for p in prices["llm"]:
                md += [f"#### {p['name']} (in ${p['input_per_mtok']}/M, out ${p['output_per_mtok']}/M)", "",
                       "| config | zipf s | para | size | req/month | $ saved gross /1k | $ saved correct /1k | saved correct /month | net /month | break-even req/month |",
                       "|---|---|---|---|---|---|---|---|---|---|"]
                for r in erows:
                    if r["llm"] == p["name"] and r["hosting"] == h_label:
                        be = r["breakeven_req_month"]
                        be_s = "never" if math.isinf(be) else f"{be:,.0f}"
                        md.append(f"| {r['config']} | {r['zipf_s']} | {r['para_share']} | {r['size']} | {r['requests_per_month']:,.0f} | "
                                  f"{fmt(r['saved_gross_per_1k'], r['saved_gross_per_1k_ci95'], 4)} | {fmt(r['saved_correct_per_1k'], r['saved_correct_per_1k_ci95'], 4)} | "
                                  f"{fmt(r['saved_correct_month'], r['saved_correct_month_ci95'], 2)} | {fmt(r['net_month'], r['net_month_ci95'], 2)} | {be_s} |")
                md.append("")
    (out / "summary.md").write_text("\n".join(md), encoding="utf-8")

    make_plots(a, out, srows, erows, prices, hostings, plot_h if erows else None)
    print(f"wrote {out / 'summary.md'}, summary.csv, economics.csv and plots")


# ------------------------------------------------------------------ plots
def make_plots(a, out, srows, erows, prices, hostings, plot_h):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"nocache": "#777777", "old": "#d95f02", "new": "#1b9e77"}
    zs0 = sorted({r["zipf_s"] for r in srows}, key=float)
    ps0 = sorted({r["para_share"] for r in srows}, key=float)
    sizes = sorted({r["size"] for r in srows})
    # ---- 1. hit & wrong rate per config (default params = first zs/ps, all sizes)
    base = [r for r in srows if r["zipf_s"] == zs0[len(zs0) // 2] and r["para_share"] == ps0[len(ps0) // 2]]
    configs = [c for c in ("nocache", "old", "new") if any(r["config"] == c for r in base)]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    width = 0.8 / max(len(configs), 1)
    for ax, (key, title) in zip(axes, [("hit_rate", "Hit rate"), ("wrong_rate", "Wrong answers / all requests")]):
        for j, c in enumerate(configs):
            ys, es = [], []
            for s in sizes:
                rr = [r for r in base if r["config"] == c and r["size"] == s]
                ys.append(rr[0][key] * 100 if rr else 0)
                es.append(rr[0][key + "_ci95"] * 100 if rr and rr[0][key + "_ci95"] == rr[0][key + "_ci95"] else 0)
            ax.bar([i + j * width for i in range(len(sizes))], ys, width, yerr=es, color=colors[c], label=c, capsize=3)
        ax.set_xticks([i + width * (len(configs) - 1) / 2 for i in range(len(sizes))])
        ax.set_xticklabels([f"{s} staff" for s in sizes])
        ax.set_ylabel("%")
        ax.set_title(f"{title} (s={zs0[len(zs0) // 2]}, paraphrase={ps0[len(ps0) // 2]})", fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].legend(frameon=False)
    fig.tight_layout()
    fig.savefig(out / "hit_wrong_rates.png", dpi=150)
    plt.close(fig)

    # ---- 2. $ saved vs requests/month with break-even lines
    if erows and plot_h:
        import numpy as np
        llms = [p["name"] for p in prices["llm"]]
        fig, axes = plt.subplots(1, len(llms), figsize=(4.6 * len(llms), 4), squeeze=False)
        hh = next(h for h in hostings if hosting_label(h) == plot_h)
        for ax, name in zip(axes[0], llms):
            xs = np.logspace(2, 6, 100)
            for c in ("old", "new"):
                rr = [r for r in erows if r["config"] == c and r["llm"] == name and r["hosting"] == plot_h
                      and r["zipf_s"] == zs0[len(zs0) // 2] and r["para_share"] == ps0[len(ps0) // 2]]
                if not rr:
                    continue
                big = max(rr, key=lambda r: r["size"])
                per_req = big["saved_correct_per_1k"] / 1000 - big["cpu_cost_per_1k"] / 1000
                ax.plot(xs, per_req * xs - big["fixed_month_usd"], color=colors[c], label=f"{c} (size {big['size']} rates)")
                ax.scatter([r["requests_per_month"] for r in rr], [r["net_month"] for r in rr], color=colors[c], zorder=3)
                be = big["breakeven_req_month"]
                if not math.isinf(be):
                    ax.axvline(be, color=colors[c], ls=":", lw=1)
                    ax.annotate(f"break-even {be:,.0f}", (be, 0), fontsize=7, rotation=90, va="bottom", color=colors[c])
            ax.axhline(0, color="black", lw=0.6)
            ax.set_xscale("log")
            ax.set_xlabel("requests / month")
            ax.set_ylabel("net savings, USD / month")
            ax.set_title(name[:40], fontsize=9)
            ax.spines[["top", "right"]].set_visible(False)
        axes[0][0].legend(frameon=False, fontsize=8)
        fig.suptitle(f"Net $ saved vs volume (hosting: {plot_h}); dots = simulated company sizes", fontsize=10)
        fig.tight_layout()
        fig.savefig(out / "savings_vs_volume.png", dpi=150)
        plt.close(fig)

    # ---- 3. sensitivity
    if len(zs0) > 1 or len(ps0) > 1:
        s_big = max(sizes)
        fig, axes = plt.subplots(1, 3, figsize=(14, 4))
        for c in ("old", "new"):
            for ps in ps0:
                rr = sorted([r for r in srows if r["config"] == c and r["size"] == s_big and r["para_share"] == ps],
                            key=lambda r: float(r["zipf_s"]))
                if len(rr) > 1:
                    axes[0].errorbar([float(r["zipf_s"]) for r in rr], [r["correct_hit_rate"] * 100 for r in rr],
                                     yerr=[0 if r["correct_hit_rate_ci95"] != r["correct_hit_rate_ci95"] else r["correct_hit_rate_ci95"] * 100 for r in rr],
                                     marker="o", color=colors[c], alpha=0.4 + 0.6 * float(ps), label=f"{c} para={ps}")
            for zs in zs0:
                rr = sorted([r for r in srows if r["config"] == c and r["size"] == s_big and r["zipf_s"] == zs],
                            key=lambda r: float(r["para_share"]))
                if len(rr) > 1:
                    axes[1].errorbar([float(r["para_share"]) for r in rr], [r["correct_hit_rate"] * 100 for r in rr],
                                     yerr=[0 if r["correct_hit_rate_ci95"] != r["correct_hit_rate_ci95"] else r["correct_hit_rate_ci95"] * 100 for r in rr],
                                     marker="o", color=colors[c], label=f"{c} s={zs}")
        axes[0].set_xlabel("Zipf s")
        axes[1].set_xlabel("paraphrase share")
        for ax in axes[:2]:
            ax.set_ylabel("correct hits / all requests (%)")
            ax.legend(frameon=False, fontsize=7)
            ax.spines[["top", "right"]].set_visible(False)
        # heatmap for the new config over (s, paraphrase)
        cell = {(r["zipf_s"], r["para_share"]): r["correct_hit_rate"] * 100 for r in srows if r["config"] == "new" and r["size"] == s_big}
        if len(zs0) > 1 and len(ps0) > 1 and cell:
            import numpy as np
            grid = np.array([[cell.get((zs, ps), np.nan) for ps in ps0] for zs in zs0])
            im = axes[2].imshow(grid, origin="lower", cmap="viridis", aspect="auto")
            axes[2].set_xticks(range(len(ps0)), ps0)
            axes[2].set_yticks(range(len(zs0)), zs0)
            axes[2].set_xlabel("paraphrase share")
            axes[2].set_ylabel("Zipf s")
            axes[2].set_title(f"new: correct hit rate %, size {s_big}", fontsize=9)
            for i in range(len(zs0)):
                for j in range(len(ps0)):
                    axes[2].text(j, i, f"{grid[i, j]:.0f}", ha="center", va="center", color="white", fontsize=8)
            fig.colorbar(im, ax=axes[2])
        else:
            axes[2].axis("off")
        fig.tight_layout()
        fig.savefig(out / "sensitivity.png", dpi=150)
        plt.close(fig)


if __name__ == "__main__":
    main()
