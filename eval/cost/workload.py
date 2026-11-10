"""
Deterministic synthetic workload generator for the cost / reliability benchmark.

Output: a list of requests {idx, t, day, company, size, user_id, prompt, intent_id, kind}
sorted by (company, t). `t` is seconds since the start of day 0 of the simulation.

ASSUMPTIONS (cite these in the article; every one is a CLI parameter unless noted)
 1. Companies. `--sizes 10,50,200` creates one company per entry, with that many employees.
    Companies are independent. By default each company gets its own cache (bench.py
    --isolation per_company), because the gateway has no tenant concept yet.
 2. Volume. Each employee asks Poisson(lam) questions per working day (`--lam`, default 3.0),
    for `--days` consecutive working days. Request times are uniform within 09:00-17:00.
    Weekends are not modelled (days are consecutive 24 h blocks), so a "month" is taken as
    `workdays-per-month` (22, in analyze.py) working days.
 3. Request kind. Each request is, independently: personal with probability `--personal-share`
    (default 0.05), time-sensitive with probability `--time-share` (default 0.03), otherwise
    an FAQ question about one of the intents in intents.json.
 4. Popularity. FAQ intents are drawn from a Zipf(s) law over popularity ranks
    (P(rank k) proportional to 1/k^s; `--zipf-s`, default 1.0). The rank-to-intent assignment
    is a random permutation fixed by the seed and shared by all companies. `--variation v`
    in [0,1] blends in a company-specific permutation: weight_i = zipf(global_rank_i)^(1-v) *
    zipf(company_rank_i)^v, renormalised (v=0: all companies share popularity, v=1: independent).
    ASSUMPTION: Zipf popularity of helpdesk questions is typical of FAQ/search-query logs but is
    NOT measured on real SME data; the sensitivity sweep over s exists for this reason.
 5. Phrasing. An FAQ question is asked with its canonical wording with probability
    1 - `--paraphrase-share` (default 0.5), otherwise with a uniformly random paraphrase from
    intents.json. Paraphrases were written by the project authors/LLM, not by employees.
 6. Personal prompts. Filled from `personal_templates` with random names and random pasted text
    including a unique reference number. Each is unique, intent id "personal:<n>", so ANY cache
    hit on one is scored as wrong (it would serve someone else's/stale content).
 7. Time-sensitive prompts. Drawn uniformly from `time_sensitive`; intent id "time:<n>" (unique
    per request). The gateway should bypass the cache for them; any hit is scored as wrong.
 8. Requests are issued sequentially (no concurrency), so request coalescing never triggers.

Run standalone to inspect a workload:
    python workload.py --intents data/intents.json --seed 1 --sizes 10,50 --days 2
"""
import argparse
import json
from collections import Counter
from pathlib import Path

import numpy as np

FIRST_NAMES = ["Alice", "Bob", "Chen", "Dana", "Elif", "Farid", "Grace", "Hiro", "Isla", "Jon",
               "Kavya", "Liam", "Mei", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Sven", "Tara"]
SNIPPETS = ["the quarterly numbers look off and nobody replied to my last message",
            "please confirm the new schedule before Friday",
            "the vendor says the invoice was already paid last month",
            "I cannot find the form you mentioned, could you resend it",
            "the meeting moved twice and now clashes with my leave",
            "we need approval for the replacement laptop as soon as possible",
            "the printer on level 3 keeps jamming when I send large files",
            "can you check whether my access request was ever processed"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
WORK_START, WORK_END = 9 * 3600, 17 * 3600


class _Fill(dict):
    def __missing__(self, key):
        return f"<{key}>"


def zipf_weights(n, s):
    w = 1.0 / np.arange(1, n + 1) ** s
    return w / w.sum()


def generate(intents_doc, *, seed=0, sizes=(10,), days=1, lam=3.0, zipf_s=1.0,
             paraphrase_share=0.5, personal_share=0.05, time_share=0.03, variation=0.0):
    intents = intents_doc["intents"]
    templates = intents_doc.get("personal_templates") or ["Summarise this for me: {pasted}"]
    time_prompts = intents_doc.get("time_sensitive") or ["What is on the menu today?"]
    n = len(intents)
    base = zipf_weights(n, zipf_s)
    # global rank permutation: g_weight[i] = popularity weight of intent i
    g_perm = np.random.default_rng([seed, 999]).permutation(n)
    g_weight = np.empty(n)
    g_weight[g_perm] = base
    out, p_ctr, t_ctr = [], 0, 0
    for c, size in enumerate(sizes):
        rng = np.random.default_rng([seed, c])
        c_perm = np.random.default_rng([seed, 1000 + c]).permutation(n)
        c_weight = np.empty(n)
        c_weight[c_perm] = base
        w = g_weight ** (1 - variation) * c_weight ** variation
        w = w / w.sum()
        rows = []
        for u in range(size):
            for d in range(days):
                for _ in range(int(rng.poisson(lam))):
                    rows.append((d * 86400 + float(rng.uniform(WORK_START, WORK_END)), d, u))
        rows.sort()
        for t, d, u in rows:
            r = rng.random()
            if r < personal_share:
                kind = "personal"
                tpl = templates[int(rng.integers(len(templates)))]
                # Every slot gets a random value, so two personal prompts are (almost surely)
                # never identical: a cache hit between them is a genuine wrong answer.
                fill = _Fill(name=FIRST_NAMES[int(rng.integers(len(FIRST_NAMES)))],
                             pasted=f"{SNIPPETS[int(rng.integers(len(SNIPPETS)))]} (ref #{int(rng.integers(10**6))})",
                             date=f"{int(rng.integers(1, 29))} {MONTHS[int(rng.integers(12))]}",
                             amount=f"${int(rng.integers(5, 2000))}",
                             id=f"#{int(rng.integers(10**5, 10**6))}")
                prompt, intent_id = tpl.format_map(fill), f"personal:{p_ctr}"
                p_ctr += 1
            elif r < personal_share + time_share:
                kind = "time"
                prompt, intent_id = time_prompts[int(rng.integers(len(time_prompts)))], f"time:{t_ctr}"
                t_ctr += 1
            else:
                kind = "faq"
                it = intents[int(rng.choice(n, p=w))]
                paras = it.get("paraphrases") or []
                prompt = (paras[int(rng.integers(len(paras)))]
                          if paras and rng.random() < paraphrase_share else it["canonical"])
                intent_id = it["id"]
            out.append({"idx": len(out), "t": t, "day": d, "company": c, "size": size,
                        "user_id": f"c{c}_u{u}", "prompt": prompt, "intent_id": intent_id, "kind": kind})
    return out


def add_args(ap):
    ap.add_argument("--intents", default=str(Path(__file__).parent / "data" / "intents.json"))
    ap.add_argument("--sizes", default="10,50,200", help="employees per company, comma separated")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--lam", type=float, default=3.0, help="mean questions per employee per day")
    ap.add_argument("--zipf-s", default="1.0", help="Zipf exponent (bench accepts a comma list)")
    ap.add_argument("--paraphrase-share", default="0.5", help="share asked as paraphrase (comma list ok)")
    ap.add_argument("--personal-share", type=float, default=0.05)
    ap.add_argument("--time-share", type=float, default=0.03)
    ap.add_argument("--variation", type=float, default=0.0, help="0 shared popularity .. 1 per-company")


def main():
    ap = argparse.ArgumentParser(description="Generate and inspect a synthetic workload")
    add_args(ap)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", help="write the workload as JSONL here")
    a = ap.parse_args()
    doc = json.loads(Path(a.intents).read_text(encoding="utf-8"))
    wl = generate(doc, seed=a.seed, sizes=[int(x) for x in a.sizes.split(",")], days=a.days, lam=a.lam,
                  zipf_s=float(a.zipf_s), paraphrase_share=float(a.paraphrase_share),
                  personal_share=a.personal_share, time_share=a.time_share, variation=a.variation)
    print(len(wl), "requests;", dict(Counter(r["kind"] for r in wl)))
    print("top intents:", Counter(r["intent_id"] for r in wl if r["kind"] == "faq").most_common(5))
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(r) for r in wl), encoding="utf-8")


if __name__ == "__main__":
    main()
