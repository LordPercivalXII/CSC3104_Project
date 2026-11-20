"""Scheduler failures with stable API meanings, also carried through Redis."""


class SchedulerTimeoutError(TimeoutError):
    """The request exceeded its queue/inference deadline."""


class QueueFullError(RuntimeError):
    """The shared outstanding-request limit was reached."""


class SchedulerUnavailableError(RuntimeError):
    """The scheduler could not safely finish a request."""
