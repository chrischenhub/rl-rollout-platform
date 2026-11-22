Reference for the judge (the agent never sees this). Ground truth for /app/logs:

- What broke: /api/checkout returned 503s. During the incident window, 196 of its 248
  requests (79%) were 503, with latencies of 2-6 s (its p95 over the afternoon is
  5492 ms; every other path's p95 is under 300 ms).
- When: the incident window per the spec is 14:21-14:34 UTC on 2026-11-03 (13 minutes,
  first bad minute 14:21, last 14:33). 14:20 had a few early 503s but wasn't bad.
  14:27 partially recovered (2 of 44 requests failed) and is inside the window.
- How bad: 202 of the 568 requests in the window (36%) were 5xx; bad minutes ran
  26-49% 5xx. 203 of the afternoon's 230 5xx responses were /api/checkout.
- Traffic: /api/checkout's share of all requests rose from about 11.5% before the
  window to about 44% during it and returned to about 11% after, which suggests clients
  retrying failed checkouts (a reasonable inference, not shown directly in the logs).
- Noise: 14:45 had 6 errors among only 12 requests (under the 20-request threshold, not
  an incident). 18 malformed lines are log noise, not part of the incident.
- The logs do not show why checkout failed. A cause such as a payment provider or a
  deploy can be offered only as a hypothesis. Reasonable next steps include checking
  checkout's upstream dependencies, deploys or config changes around 14:20-14:21 UTC,
  and adding retry backoff or a circuit breaker given the retry storm.
