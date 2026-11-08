"""
Load generator for the scheduler experiments. Standard library only.

Sends requests to the gateway, one thread per request, and appends one CSV
row per request. Times are seconds from the start of the scenario.

    python eval/loadtest.py fairness   --config redis-fair --run 1
    python eval/loadtest.py fairness   --config fifo --run 1 --fifo
    python eval/loadtest.py steady     --config redis --run 1
    python eval/loadtest.py duplicates --config coalesce-on --run 1 --n 10

Scenarios:
  fairness    E1. A heavy user sends many requests at once; a few seconds
              later light users send one each. --fifo sends every request
              under one shared identity, which turns the per-user queues into
              a single first-come-first-served queue (the baseline), while the
              CSV keeps the real user.
  steady      E3. Requests from many users at a fixed rate per minute.
  duplicates  Duplicate-call experiment. N people send the same new question
              to /chat at the same moment.
"""
import argparse
import csv
import json
import os
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIELDS = ["experiment", "config", "run", "trial", "label", "user", "sent_s", "done_s",
          "latency_s", "status", "source", "coalesced"]


def post(url, user_id, prompt, timeout):
    """POST one prompt. Returns (HTTP status, response JSON or {})."""
    body = json.dumps({"user_id": user_id, "prompt": prompt}).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json"},
                      method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, {}
    except (URLError, TimeoutError, OSError):
        return 0, {}


class Recorder:
    """Collects one row per request from many threads."""

    def __init__(self, args, experiment):
        self.args = args
        self.experiment = experiment
        self.rows = []
        self.lock = threading.Lock()
        self.threads = []
        self.trial = uuid.uuid4().hex[:8]     # groups the rows of one scenario run
        self.t0 = time.monotonic()

    def send(self, endpoint, label, user, prompt, identity=None):
        """Send in a background thread. identity = the user_id the gateway sees."""
        def run():
            sent = time.monotonic() - self.t0
            status, data = post(self.args.url + endpoint, identity or user, prompt,
                                self.args.timeout)
            done = time.monotonic() - self.t0
            with self.lock:
                self.rows.append({
                    "experiment": self.experiment, "config": self.args.config,
                    "run": self.args.run, "trial": self.trial, "label": label, "user": user,
                    "sent_s": round(sent, 3), "done_s": round(done, 3),
                    "latency_s": round(done - sent, 3), "status": status,
                    "source": data.get("source", ""),
                    "coalesced": data.get("coalesced", ""),
                })

        thread = threading.Thread(target=run)
        thread.start()
        self.threads.append(thread)

    def finish(self):
        for thread in self.threads:
            thread.join()

        os.makedirs(os.path.dirname(self.args.out) or ".", exist_ok=True)
        new_file = not os.path.exists(self.args.out)
        with open(self.args.out, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(sorted(self.rows, key=lambda r: r["sent_s"]))

        failed = sum(1 for r in self.rows if r["status"] != 200)
        print(f"  {self.experiment} {self.args.config} run {self.args.run}: "
              f"{len(self.rows)} requests, {failed} failed")
        return failed


def fairness(args):
    rec = Recorder(args, "fairness")
    tag = uuid.uuid4().hex[:6]
    shared = f"everyone-{tag}" if args.fifo else None

    for i in range(1, args.heavy + 1):
        rec.send("/scheduler/chat", f"H{i}", "heavy", f"heavy {tag} {i}", shared)
        time.sleep(0.05)   # keep the arrival order the same every run

    time.sleep(args.light_delay)
    for i in range(1, args.light + 1):
        rec.send("/scheduler/chat", f"L{i}", f"light{i}", f"light {tag} {i}", shared)
        time.sleep(0.05)

    return rec.finish()


def steady(args):
    rec = Recorder(args, "steady")
    tag = uuid.uuid4().hex[:6]
    gap = 60 / args.rate
    total = int(args.rate * args.minutes)

    for i in range(total):
        # Spread over 10 users so no single user's queue decides the order
        rec.send("/scheduler/chat", f"S{i + 1}", f"user{i % 10}", f"steady {tag} {i}")
        next_at = (i + 1) * gap
        time.sleep(max(0, next_at - (time.monotonic() - rec.t0)))

    return rec.finish()


def duplicates(args):
    rec = Recorder(args, "duplicates")
    # A new question every trial, so it is never already in the semantic cache.
    # The numbers keep trials apart (the cache only matches the same numbers).
    question = (f"What is the approval process for purchase order "
                f"{int(time.time())}{args.n}{args.run}?")

    for i in range(1, args.n + 1):
        rec.send("/chat", f"D{i}", f"dup-user{i}", question)

    return rec.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("scenario", choices=["fairness", "steady", "duplicates"])
    parser.add_argument("--url", default=os.getenv("URL", "http://localhost:8080"))
    parser.add_argument("--config", required=True, help="name of the setup, stored in the CSV")
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--out", help="CSV to append to (default eval/results/<scenario>.csv)")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--heavy", type=int, default=18, help="fairness: heavy user's requests")
    parser.add_argument("--light", type=int, default=3, help="fairness: number of light users")
    parser.add_argument("--light-delay", type=float, default=5, help="fairness: seconds before light users send")
    parser.add_argument("--fifo", action="store_true", help="fairness: send everything as one user")
    parser.add_argument("--rate", type=float, default=18, help="steady: requests per minute")
    parser.add_argument("--minutes", type=float, default=2, help="steady: how long to send")
    parser.add_argument("--n", type=int, default=5, help="duplicates: people asking at once")
    args = parser.parse_args()
    args.out = args.out or f"eval/results/{args.scenario}.csv"

    failed = {"fairness": fairness, "steady": steady, "duplicates": duplicates}[args.scenario](args)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
