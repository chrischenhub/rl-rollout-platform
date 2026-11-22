import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate: float, capacity: float, clock: Callable[[], float] = time.monotonic):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._clock = clock
        self._tokens = self.capacity
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)  # a clock that went backwards adds nothing
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    def _check(self, tokens: float) -> None:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError(f"tokens must be in (0, {self.capacity}], got {tokens}")

    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def try_acquire(self, tokens: float = 1) -> bool:
        self._check(tokens)
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def wait_time(self, tokens: float = 1) -> float:
        self._check(tokens)
        with self._lock:
            self._refill()
            missing = tokens - self._tokens
            return 0.0 if missing <= 0 else missing / self.rate
