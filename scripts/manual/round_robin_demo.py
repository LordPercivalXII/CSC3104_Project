# Demostrates basic Round Robin

from scheduler.fair_scheduler import FairShareScheduler

def main():
    scheduler = FairShareScheduler()
    for user, count in (("A", 5), ("B", 2), ("C", 3)):
        for i in range(1, count + 1):
            scheduler.add_request(user, f"{user}{i}")
    print("Total pending:", scheduler.pending_count())
    print("\nProcessing order:")
    while scheduler.pending_count() > 0:
        print(scheduler.get_next_request())


if __name__ == "__main__":
    main()
