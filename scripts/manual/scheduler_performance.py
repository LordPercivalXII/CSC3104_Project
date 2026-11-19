
from collections import deque
from statistics import mean

from scheduler.fair_scheduler import FairShareScheduler


# Simulated time for each LLM request (seconds)
PROCESSING_TIME = 1.0


def create_requests():
    requests = []

    for i in range(1, 11):
        requests.append(("A", f"A{i}"))

    for i in range(1, 3):
        requests.append(("B", f"B{i}"))

    for i in range(1, 3):
        requests.append(("C", f"C{i}"))

    return requests


def fifo_order(requests):
    queue = deque(requests)
    result = []

    while queue:
        result.append(queue.popleft())

    return result


def round_robin_order(requests):
    scheduler = FairShareScheduler()

    for user_id, request_id in requests:
        scheduler.add_request(user_id, request_id)

    result = []

    while scheduler.pending_count() > 0:
        result.append(scheduler.get_next_request())

    return result


def evaluate(order):
    # All requests arrive at simulated time 0.
    # Only one LLM request can run at a time.
    current_time = 0.0
    waiting_times = {}

    for user_id, request_id in order:
        waiting_times[request_id] = current_time
        current_time += PROCESSING_TIME

    per_user = {}

    for user_id, request_id in order:
        per_user.setdefault(user_id, [])
        per_user[user_id].append(waiting_times[request_id])

    return {
        user: mean(times)
        for user, times in per_user.items()
    }


def display_results(name, order):
    print(f"\n{name}")
    print("-" * 45)

    print("Processing order:")
    print(" -> ".join(request for _, request in order))

    averages = evaluate(order)

    print("\nAverage waiting time per employee:")

    for user, average in averages.items():
        print(f"Employee {user}: {average:.2f} seconds")


def main():
    requests = create_requests()
    display_results("FIFO Scheduling", fifo_order(requests))
    display_results("Round Robin Scheduling", round_robin_order(requests))


if __name__ == "__main__":
    main()
