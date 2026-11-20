# Tests simulated LLM processing

from scheduler.scheduler_worker import SchedulerWorker

def main():
    worker = SchedulerWorker()
    worker.start()
    try:
        print("Scheduler worker started!")
        requests = [
            ("A", "What is Docker?"),
            ("A", "What is Kubernetes?"),
            ("A", "Explain cloud computing"),
            ("B", "What is Redis?"),
            ("B", "Explain caching"),
            ("C", "What is FastAPI?")
        ]
        futures = [worker.submit(user, prompt) for user, prompt in requests]
        for future in futures:
            print("Response:", future.result(timeout=15))
    finally:
        worker.stop()
    print("All requests completed!")


if __name__ == "__main__":
    main()
