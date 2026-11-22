import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate: float, capacity: float, clock: Callable[[], float] = time.monotonic):
        raise NotImplementedError

    @property
    def tokens(self) -> float:
        raise NotImplementedError

    def try_acquire(self, tokens: float = 1) -> bool:
        raise NotImplementedError

    def wait_time(self, tokens: float = 1) -> float:
        raise NotImplementedError
