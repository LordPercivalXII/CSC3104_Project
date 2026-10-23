# Demostrates basic Round Robin

from scheduler.fair_scheduler import FairShareScheduler

scheduler = FairShareScheduler()

# Employee A submits 5 requests
for i in range(1, 6):
    scheduler.add_request("A", f"A{i}")

# Employee B submits 2 requests
for i in range(1, 3):
    scheduler.add_request("B", f"B{i}")

# Employee C submits 3 requests
for i in range(1, 4):
    scheduler.add_request("C", f"C{i}")

print("Total pending:", scheduler.pending_count())
print("\nProcessing order:")

while scheduler.pending_count() > 0:
    result = scheduler.get_next_request()
    print(result)
