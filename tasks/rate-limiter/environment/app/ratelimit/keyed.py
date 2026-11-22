import time
from collections.abc import Callable, Hashable


class KeyedLimiter:
    def __init__(self, rate: float, capacity: float, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        raise NotImplementedError

    def try_acquire(self, key: Hashable, tokens: float = 1) -> bool:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError
