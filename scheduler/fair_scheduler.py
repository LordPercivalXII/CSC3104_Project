
from collections import defaultdict, deque
from threading import Lock


class FairShareScheduler:
    def __init__(self):
        # Each employee has their own queue
        self.queues = defaultdict(deque)

        # Order of employees waiting for their turn
        self.active_users = deque()

        # Protect shared data from concurrent threads
        self.lock = Lock()

    def add_request(self, user_id, request):
        with self.lock:
            # New active employee joins the rotation
            if not self.queues[user_id]:
                self.active_users.append(user_id)

            # Add request to that employee's queue
            self.queues[user_id].append(request)

    def get_next_request(self):
        with self.lock:
            # No requests waiting
            if not self.active_users:
                return None

            # Select the next employee
            user_id = self.active_users.popleft()

            # Get their oldest request
            request = self.queues[user_id].popleft()

            # Put employee at back if more requests remain
            if self.queues[user_id]:
                self.active_users.append(user_id)
            else:
                del self.queues[user_id]

            return user_id, request

    def pending_count(self):
        with self.lock:
            return sum(
                len(queue)
                for queue in self.queues.values()
            )
