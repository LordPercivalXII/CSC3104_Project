# Tests requests arriving at different times

import threading
import time

from scheduler.fair_scheduler import FairShareScheduler

scheduler = FairShareScheduler()


def submit_requests():
    # Employee A submits three requests
    for i in range(1, 4):
        scheduler.add_request("A", f"A{i}")

    print("Employee A submitted 3 requests")

    # Employee B joins while processing is happening
    time.sleep(1.5)

    scheduler.add_request("B", "B1")
    scheduler.add_request("B", "B2")

    print("Employee B submitted 2 requests")

    # Employee C joins later
    time.sleep(1)

    scheduler.add_request("C", "C1")

    print("Employee C submitted 1 request")


def process_requests():
    processed = 0

    while processed < 6:
        result = scheduler.get_next_request()

        if result is None:
            # No requests currently waiting
            time.sleep(0.1)
            continue

        user_id, request = result

        print(f"Processing {request} from Employee {user_id}")

        # Simulate LLM inference taking one second
        time.sleep(1)

        processed += 1


producer = threading.Thread(target=submit_requests)
consumer = threading.Thread(target=process_requests)

producer.start()
consumer.start()

producer.join()
consumer.join()

print("All requests processed!")
