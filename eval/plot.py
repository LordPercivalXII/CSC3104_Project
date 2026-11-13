"""
Turns the CSVs in eval/results/ into graphs for the report and a summary of
the numbers (eval/results/summary.md).

    pip install matplotlib
    python eval/plot.py

Graphs are sized for one column of an IEEE paper (3.5 in wide, 8 pt text).
"""
import csv
import os
import statistics
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

RESULTS = "eval/results"

# Validated palette (two categorical slots), plus text and grid colours
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8,
    "xtick.labelsize": 7.5, "ytick.labelsize": 7.5, "legend.fontsize": 7.5,
    "axes.edgecolor": INK_2, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.axisbelow": True, "legend.frameon": False,
    "savefig.dpi": 300, "savefig.bbox": "tight",
})


def load(name):
    path = os.path.join(RESULTS, f"{name}.csv")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in ("sent_s", "done_s", "latency_s"):
            r[key] = float(r[key])
        r["status"] = int(r["status"])
    return rows


def mean_sd(values):
    values = list(values)
    if not values:
        return float("nan"), 0.0
    return statistics.mean(values), statistics.stdev(values) if len(values) > 1 else 0.0


def save(fig, name):
    path = os.path.join(RESULTS, f"{name}.png")
    fig.savefig(path)
    plt.close(fig)
    print(f"  wrote {path}")


# --- E1: fairness ------------------------------------------------------------

E1_ORDER = [("fifo", "FIFO\n(one queue)"),
            ("fair-per-pod", "Fair share\nper pod"),
            ("fair-shared", "Fair share\nshared (Redis)")]


def fairness(rows, summary):
    if not rows:
        return
    # Per run: mean wait of the light users, and when the heavy user's last request finished
    light, heavy = defaultdict(list), defaultdict(list)
    for (config, trial), group in group_by(rows, "config", "trial").items():
        light[config].append(statistics.mean(r["latency_s"] for r in group if r["user"] != "heavy"))
        heavy[config].append(max(r["done_s"] for r in group if r["user"] == "heavy"))

    configs = [(c, label) for c, label in E1_ORDER if c in light]
    x = range(len(configs))
    w = 0.36
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    for offset, data, colour, name in ((-w / 2, light, BLUE, "Light users: mean wait"),
                                       (w / 2, heavy, ORANGE, "Heavy user: last answer")):
        means = [mean_sd(data[c])[0] for c, _ in configs]
        sds = [mean_sd(data[c])[1] for c, _ in configs]
        bars = ax.bar([i + offset for i in x], means, w - 0.04, color=colour, label=name,
                      yerr=sds, error_kw={"elinewidth": 0.8, "capsize": 2, "ecolor": INK_2})
        if colour == BLUE:
            ax.bar_label(bars, labels=[f"{m:.0f} s" for m in means], padding=3, fontsize=7, color=INK)
    ax.set_xticks(list(x), [label for _, label in configs])
    ax.set_ylabel("Time (s)")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.2), ncol=2)
    save(fig, "e1_fairness")

    summary.append("## E1 fairness: heavy user sends 18 at once, 3 light users send 1 each 5 s later\n")
    summary.append("| Setup | Runs | Light users' mean wait (s) | Heavy user's last answer (s) |")
    summary.append("| --- | --- | --- | --- |")
    for c, label in configs:
        lm, ls = mean_sd(light[c])
        hm, hs = mean_sd(heavy[c])
        summary.append(f"| {label.replace(chr(10), ' ')} | {len(light[c])} | {lm:.1f} ± {ls:.1f} | {hm:.1f} ± {hs:.1f} |")
    summary.append("")


# --- E3: rate limit ------------------------------------------------------------

E3_ORDER = [("limit-per-pod", "Limit per pod (memory)", ORANGE, "s"),
            ("shared-limit", "One shared limit (Redis)", BLUE, "o")]
LIMIT = 9


def calls_per_window(times, end):
    """Successful response completions in (t - 60, t], evaluated at every event.

    Completion timestamps cannot prove provider start-time quota compliance.
    Event boundaries prevent a fractional-second peak being missed.
    """
    times = sorted(times)
    xs = sorted(set([0, end, *times, *(t + 60 for t in times if t + 60 <= end)]))
    left, right, counts = 0, 0, []
    for x in xs:
        while right < len(times) and times[right] <= x:
            right += 1
        while left < right and times[left] + 60 <= x:
            left += 1
        counts.append(right - left)
    return xs, counts


def rate_limit(rows, summary):
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    summary.append(f"## E3 rate limit: 18 requests per minute for 2 minutes, limit set to {LIMIT} per minute\n")
    summary.append("Response completion windows are measured here; provider call start times were not recorded.\n")
    summary.append("| Setup | Runs | Most successful completions in any 60 s (per run) | Last answer (s) |")
    summary.append("| --- | --- | --- | --- |")
    groups = group_by(rows, "config", "trial")
    for config, label, colour, marker in E3_ORDER:
        trials = [g for (c, _), g in groups.items() if c == config]
        if not trials:
            continue
        peaks, ends = [], []
        for i, group in enumerate(trials):
            times = [r["done_s"] for r in group if r["status"] == 200]
            if not times:
                continue
            xs, ys = calls_per_window(times, max(times))
            peaks.append(max(ys))
            ends.append(max(times))
            ax.plot(xs, ys, color=colour, linewidth=1.6 if i == 0 else 0.8,
                    alpha=1 if i == 0 else 0.45, label=label if i == 0 else None,
                    marker=marker, markevery=20, markersize=3.5)
        if ends:
            summary.append(f"| {label} | {len(trials)} | {', '.join(map(str, peaks))} | {statistics.mean(ends):.0f} |")
    ax.axhline(LIMIT, color=INK_2, linewidth=1, linestyle="--")
    ax.annotate(f"limit {LIMIT} per minute", xy=(1, LIMIT), xycoords=("axes fraction", "data"),
                xytext=(0, 3), textcoords="offset points", ha="right", fontsize=7, color=INK_2)
    ax.set_xlabel("Time since the first request (s)")
    ax.set_ylabel("Successful completions in the last 60 s")
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.2), ncol=2)
    save(fig, "e3_rate_limit")
    summary.append("")


# --- Duplicate questions --------------------------------------------------------

AQUA = "#1baf7a"   # third validated slot; numbers are also in summary.md (table view)
HOW = [("llm", "New LLM call", ORANGE), ("shared", "Shared answer", BLUE),
       ("cache", "Semantic cache", AQUA)]


def answered_by(r):
    if r["source"] == "semantic_cache":
        return "cache"
    return "shared" if r["coalesced"] == "True" else "llm"


def duplicates(rows, summary):
    if not rows:
        return
    # For each setup and N: how many people got their answer each way, per run
    counts = defaultdict(lambda: defaultdict(list))
    waits = defaultdict(list)
    for (config, _), group in group_by(rows, "config", "trial").items():
        n = len(group)
        ok = [r for r in group if r["status"] == 200]
        for how, _, _ in HOW:
            counts[(config, n)][how].append(sum(1 for r in ok if answered_by(r) == how))
        waits[(config, n)].append(statistics.mean(r["latency_s"] for r in group))

    ns = sorted({n for _, n in counts})
    setups = [("sharing-off", "off"), ("sharing-on", "on")]
    fig, ax = plt.subplots(figsize=(3.5, 2.4))
    w, positions, labels = 0.38, [], []
    for i, n in enumerate(ns):
        for j, (config, short) in enumerate(setups):
            if (config, n) not in counts:
                continue
            x = i * 1.1 + (j - 0.5) * (w + 0.04)
            bottom = 0
            for how, name, colour in HOW:
                value = statistics.mean(counts[(config, n)][how])
                ax.bar(x, value, w, bottom=bottom, color=colour, edgecolor="white",
                       linewidth=0.8, label=name if (i, j) == (0, 0) else None)
                bottom += value
            ax.text(x, bottom + 0.3, short, ha="center", va="bottom", fontsize=6.5, color=INK_2)
        positions.append(i * 1.1)
        labels.append(str(n))
    ax.set_xticks(positions, labels)
    ax.set_xlabel("People asking the same question at once (sharing off / on)")
    ax.set_ylabel("People answered")
    ax.set_ylim(0, max(ns) * 1.12)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper left", reverse=True)
    save(fig, "duplicates")

    summary.append("## Duplicate questions: N people ask the same new question at once "
                   "(real Gemini, limit 10 per minute)\n")
    summary.append("Mean over runs of how many people got each kind of answer. "
                   "New LLM calls are inferred from successful non-cache, non-coalesced responses; "
                   "failed executions and SDK retries are not counted by this CSV.\n")
    summary.append("| N | Sharing | Runs | New LLM calls | Shared answers | Semantic cache | Mean wait (s) |")
    summary.append("| --- | --- | --- | --- | --- | --- | --- |")
    for n in ns:
        for config, short in setups:
            if (config, n) not in counts:
                continue
            c = counts[(config, n)]
            per_run = "/".join(str(v) for v in c["llm"])
            summary.append(f"| {n} | {short} | {len(c['llm'])} | {statistics.mean(c['llm']):.1f} ({per_run}) | "
                           f"{statistics.mean(c['shared']):.1f} | {statistics.mean(c['cache']):.1f} | "
                           f"{mean_sd(waits[(config, n)])[0]:.1f} |")
    summary.append("")


def group_by(rows, *keys):
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in keys)].append(r)
    return groups


def main():
    import argparse
    global RESULTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=RESULTS, help="directory containing the run CSVs")
    RESULTS = parser.parse_args().results
    os.makedirs(RESULTS, exist_ok=True)
    summary = ["# Scheduler experiment results\n"]
    setups = os.path.join(RESULTS, "setups.csv")
    if os.path.exists(setups):
        with open(setups) as f:
            summary.append("Setups used (from setups.csv):\n")
            summary += [f"    {line.rstrip()}" for line in f]
            summary.append("")

    failed = 0
    for name, fn in (("fairness", fairness), ("steady", rate_limit), ("duplicates", duplicates)):
        rows = load(name)
        failed += sum(1 for r in rows if r["status"] != 200)
        fn(rows, summary)

    summary.append(f"Requests that failed (not HTTP 200): {failed}")
    path = os.path.join(RESULTS, "summary.md")
    with open(path, "w") as f:
        f.write("\n".join(summary) + "\n")
    print(f"  wrote {path}")


if __name__ == "__main__":
    main()
