import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable

from ratelimit.bucket import TokenBucket


class KeyedLimiter:
    def __init__(self, rate: float, capacity: float, max_keys: int = 10_000,
                 clock: Callable[[], float] = time.monotonic):
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        TokenBucket(rate, capacity, clock)  # validates rate and capacity up front
        self.rate, self.capacity, self.max_keys, self._clock = rate, capacity, max_keys, clock
        self._buckets: OrderedDict[Hashable, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def try_acquire(self, key: Hashable, tokens: float = 1) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = TokenBucket(self.rate, self.capacity, self._clock)
                while len(self._buckets) > self.max_keys:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
        return bucket.try_acquire(tokens)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)
