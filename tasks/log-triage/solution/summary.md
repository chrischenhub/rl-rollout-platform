# Incident: /api/checkout 503s, 2026-11-03 14:21-14:34 UTC

**What broke.** `/api/checkout` returned 503 Service Unavailable. Inside the window,
196 of its 248 requests (79%) failed, taking 2-6 s each; its p95 latency for the
afternoon was 5.5 s, while every other endpoint stayed under 300 ms.

**When.** Bad minutes ran from 14:21 to 14:33 UTC, with a one-minute partial recovery
at 14:27. A few early 503s appeared at 14:20; the last bad minute was 14:33.

**How bad.** 36% of all requests in the window (202 of 568) were 5xx. Checkout
accounts for 203 of the afternoon's 230 5xx responses. Checkout's share of traffic
jumped from about 12% to 44% during the window and fell back afterwards, so clients
were probably retrying failed checkouts (hypothesis: the logs don't show retries
directly), which multiplied load on the failing service.

**Next step.** Check what changed for checkout's upstream dependencies, deploys or
config around 14:20 UTC, and make clients back off on 503 (retry with jitter, or a
circuit breaker) so a partial outage doesn't quadruple checkout traffic.
