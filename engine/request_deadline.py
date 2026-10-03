"""One monotonic budget for queueing, candidate checks and database work."""
from contextvars import ContextVar
import time

_DEADLINE = ContextVar('request_deadline', default=None)


class RequestTimedOut(TimeoutError):
    pass


def begin(seconds=240):
    return _DEADLINE.set(time.monotonic() + seconds)


def end(token):
    _DEADLINE.reset(token)


def remaining():
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise RequestTimedOut('The analysis timed out. Please retry or narrow the question.')
    return seconds


def expired():
    deadline = _DEADLINE.get()
    return deadline is not None and time.monotonic() >= deadline
