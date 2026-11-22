from ratelimit import TokenBucket


def test_starts_full():
    bucket = TokenBucket(rate=1, capacity=3, clock=lambda: 0.0)
    assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]
