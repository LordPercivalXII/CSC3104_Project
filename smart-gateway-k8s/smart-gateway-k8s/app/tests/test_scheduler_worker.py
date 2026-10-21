# Tests simulated LLM processing

from scheduler.scheduler_worker import SchedulerWorker

worker = SchedulerWorker()

worker.start()

print("Scheduler worker started!")

# Submit requests from three employees
requests = [
    ("A", "What is Docker?"),
    ("A", "What is Kubernetes?"),
    ("A", "Explain cloud computing"),
    ("B", "What is Redis?"),
    ("B", "Explain caching"),
    ("C", "What is FastAPI?")
]

futures = []

for user_id, prompt in requests:
    future = worker.submit(user_id, prompt)
    futures.append(future)

# Wait for every response
for future in futures:
    result = future.result(timeout=15)
    print("Response:", result)

worker.stop()

print("All requests completed!")
