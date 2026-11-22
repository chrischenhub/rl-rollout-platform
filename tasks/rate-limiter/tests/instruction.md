Implement the `ratelimit` package in `/app` so that it does everything
`/app/README.md` specifies. The classes and method signatures are already stubbed out
in `ratelimit/bucket.py` and `ratelimit/keyed.py`; keep them. Use only the standard
library.

`cd /app && python -m pytest` runs the existing tests, which cover very little of the
spec. Hidden tests will check every rule in the README, including behaviour under
concurrent use from many threads.
