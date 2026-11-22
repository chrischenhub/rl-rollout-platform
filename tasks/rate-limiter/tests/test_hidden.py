"""Hidden checks for pyswe/rate-limiter. Each test maps to a rubric criterion."""

import random
import sys
import threading

import pytest

from ratelimit import KeyedLimiter, TokenBucket


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fast_switching():
    """Switch threads as often as possible, so unsynchronized code interleaves."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


# burst_capacity
def test_starts_full_and_drains(clock):
    bucket = TokenBucket(rate=2, capacity=5, clock=clock)
    assert bucket.tokens == pytest.approx(5)
    assert [bucket.try_acquire() for _ in range(6)] == [True] * 5 + [False]


# refill_rate
def test_refills_continuously_at_rate(clock):
    bucket = TokenBucket(rate=4, capacity=10, clock=clock)
    assert bucket.try_acquire(10)
    clock.advance(0.5)
    assert bucket.tokens == pytest.approx(2)
    clock.advance(0.125)
    assert bucket.tokens == pytest.approx(2.5)
    assert bucket.try_acquire(2.5) and not bucket.try_acquire(0.25)


def test_refill_never_exceeds_capacity(clock):
    bucket = TokenBucket(rate=100, capacity=3, clock=clock)
    bucket.try_acquire(1)
    clock.advance(3600)
    assert bucket.tokens == pytest.approx(3)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]


# atomic_acquire
def test_failed_acquire_takes_nothing(clock):
    bucket = TokenBucket(rate=1, capacity=5, clock=clock)
    assert bucket.try_acquire(4)
    assert not bucket.try_acquire(2)
    assert bucket.tokens == pytest.approx(1)
    assert bucket.try_acquire(1)


def test_fractional_rate(clock):
    bucket = TokenBucket(rate=0.5, capacity=1, clock=clock)
    assert bucket.try_acquire()
    clock.advance(1.5)
    assert not bucket.try_acquire()
    clock.advance(0.5)
    assert bucket.try_acquire()


# wait_time
def test_wait_time(clock):
    bucket = TokenBucket(rate=4, capacity=8, clock=clock)
    assert bucket.wait_time(8) == 0.0
    assert bucket.try_acquire(8)
    assert bucket.wait_time(1) == pytest.approx(0.25)
    assert bucket.wait_time(6) == pytest.approx(1.5)
    clock.advance(1)
    assert bucket.wait_time(6) == pytest.approx(0.5)
    assert bucket.tokens == pytest.approx(4)  # asking didn't take anything


# validation
@pytest.mark.parametrize("rate,capacity", [(0, 1), (-1, 1), (1, 0), (1, -5)])
def test_rejects_bad_construction(rate, capacity):
    with pytest.raises(ValueError):
        TokenBucket(rate=rate, capacity=capacity, clock=FakeClock())


@pytest.mark.parametrize("tokens", [0, -1, 5.5])
def test_rejects_bad_token_counts(tokens, clock):
    bucket = TokenBucket(rate=1, capacity=5, clock=clock)
    with pytest.raises(ValueError):
        bucket.try_acquire(tokens)
    with pytest.raises(ValueError):
        bucket.wait_time(tokens)


def test_keyed_rejects_bad_max_keys():
    with pytest.raises(ValueError):
        KeyedLimiter(rate=1, capacity=1, max_keys=0, clock=FakeClock())


# clock_backwards
def test_clock_going_backwards_adds_nothing(clock):
    bucket = TokenBucket(rate=10, capacity=10, clock=clock)
    assert bucket.try_acquire(10)
    clock.advance(-50)
    assert bucket.tokens == pytest.approx(0)
    assert not bucket.try_acquire(1)
    clock.advance(0.5)  # refills count from the new, earlier reading
    assert bucket.tokens == pytest.approx(5)


# thread_safety
def test_threads_never_over_admit(clock, fast_switching):
    bucket = TokenBucket(rate=1, capacity=1000, clock=clock)  # frozen clock: no refills
    admitted = []

    def worker():
        admitted.append(sum(bucket.try_acquire() for _ in range(400)))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(admitted) == 1000


def test_last_token_goes_to_exactly_one_thread(clock, fast_switching):
    over = 0
    for _ in range(300):
        bucket = TokenBucket(rate=1, capacity=1, clock=clock)
        barrier = threading.Barrier(8)
        wins = []

        def worker():
            barrier.wait()
            wins.append(bucket.try_acquire())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        over += sum(wins) - 1
    assert over == 0


def test_keyed_limiter_under_threads(clock, fast_switching):
    limiter = KeyedLimiter(rate=1, capacity=1_000_000, max_keys=50, clock=clock)
    errors = []

    def worker(seed):
        rng = random.Random(seed)
        try:
            for _ in range(3000):
                limiter.try_acquire(rng.randrange(120))
        except Exception as e:  # pragma: no cover - the failure we're looking for
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(limiter) <= 50


# keyed_isolation
def test_keys_have_independent_buckets(clock):
    limiter = KeyedLimiter(rate=1, capacity=2, clock=clock)
    assert [limiter.try_acquire("a") for _ in range(3)] == [True, True, False]
    assert limiter.try_acquire("b") and limiter.try_acquire("b", 1)
    assert not limiter.try_acquire("b")
    clock.advance(1)
    assert limiter.try_acquire("a") and not limiter.try_acquire("a")


# keyed_eviction
def test_least_recently_used_key_is_evicted(clock):
    limiter = KeyedLimiter(rate=0.001, capacity=1, max_keys=2, clock=clock)
    assert limiter.try_acquire("a") and limiter.try_acquire("b")
    assert not limiter.try_acquire("a")  # uses "a", so "b" is now least recent
    assert limiter.try_acquire("c")
    assert len(limiter) == 2
    assert limiter.try_acquire("b")  # forgotten, so it starts full again
    assert not limiter.try_acquire("c")  # "c" was kept and is still empty
    assert len(limiter) == 2


def test_len_counts_tracked_keys(clock):
    limiter = KeyedLimiter(rate=1, capacity=1, max_keys=1000, clock=clock)
    for key in range(1500):
        limiter.try_acquire(key)
    assert len(limiter) == 1000
