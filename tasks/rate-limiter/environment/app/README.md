# ratelimit

Token-bucket rate limiting for one caller (`TokenBucket`) or many (`KeyedLimiter`).

## TokenBucket(rate, capacity, clock=time.monotonic)

- `rate`: tokens added per second; `capacity`: the most tokens the bucket holds. Both
  must be positive, or the constructor raises `ValueError`.
- `clock`: a zero-argument function returning seconds as a float. Tests pass a fake one.
- The bucket starts full.
- Tokens refill continuously (fractions included) at `rate` per second, and never
  above `capacity`.
- If the clock ever goes backwards, no time has passed: the bucket neither gains nor
  loses tokens, and later refills count from the new, earlier reading.
- `try_acquire(tokens=1) -> bool`: if at least `tokens` are available, removes them and
  returns `True`; otherwise returns `False` and removes nothing.
- `wait_time(tokens=1) -> float`: seconds until `try_acquire(tokens)` would succeed if
  nobody else took tokens meanwhile; `0.0` if it would succeed now. Doesn't remove
  tokens.
- `tokens` (property): the tokens available now, after refilling.
- `try_acquire` and `wait_time` raise `ValueError` if `tokens` is not positive or is
  more than `capacity`.
- Thread-safe: any number of threads may call it at once, and it never admits more
  than the tokens available.

## KeyedLimiter(rate, capacity, max_keys=10_000, clock=time.monotonic)

- One `TokenBucket(rate, capacity, clock)` per key, created full the first time a key
  is used.
- `try_acquire(key, tokens=1) -> bool`: as `TokenBucket.try_acquire`, on that key's
  bucket. Calling it counts as using the key.
- Tracks at most `max_keys` keys: when a new key would exceed it, the least recently
  used key is forgotten (its next use starts a full bucket again). `max_keys` must be
  positive, or the constructor raises `ValueError`.
- `len(limiter)`: the number of keys tracked.
- Thread-safe, like `TokenBucket`.
