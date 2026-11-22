One afternoon of our API's access logs is in `/app/logs`. Something went wrong in it.

## 1. Write `/app/triage.py`

It runs as

```
python3 /app/triage.py --logs /app/logs --out /app/out
```

and reads every `*.log` and `*.log.gz` file directly in `--logs` (ignore other files),
then writes `--out/report.json`:

```json
{
  "total_requests": 0,
  "malformed_lines": 0,
  "status_classes": {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0},
  "p95_latency_ms": {"/some/path": 0},
  "top_5xx_paths": [{"path": "/some/path", "count": 0}],
  "incident": {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}
}
```

A valid line is exactly six fields separated by single spaces:

```
<timestamp> <client> <method> <path> <status> <latency>ms
2026-11-03T14:05:12Z 10.0.4.17 GET /api/orders?page=2 200 87ms
```

- `timestamp`: a real UTC time written `YYYY-MM-DDTHH:MM:SSZ`.
- `client`: any non-empty token without spaces.
- `method`: upper-case letters only.
- `path`: starts with `/`. Group by the path without its query string
  (`/api/orders?page=2` counts as `/api/orders`).
- `status`: three digits, 200 to 599.
- `latency`: a non-negative whole number followed directly by `ms`.

Blank lines are ignored. Every other line that isn't valid is counted in
`malformed_lines` and otherwise skipped.

- `total_requests`: the number of valid lines. `status_classes` always has all four keys.
- `p95_latency_ms`: for each path, the nearest-rank 95th percentile: sort its latencies
  in ascending order and take the value at 1-based position `ceil(0.95 × n)`.
- `top_5xx_paths`: the paths with at least one 5xx response, most first, ties broken
  by path in alphabetical order; at most 3 entries.
- `incident`: group valid requests by the minute of their timestamp. A minute is *bad*
  if it has at least 20 requests and more than 25% of them are 5xx. With no bad minutes,
  `incident` is `null`. Otherwise `start` is the first bad minute and `end` is the
  minute after the last bad minute, both written like `2026-11-03T14:21:00Z`; minutes
  in between count as part of the incident whether they were bad or not.

The script will also be run on other log directories in the same format, so don't
hard-code anything about this data. Use only the standard library.

## 2. Run it, then write `/app/out/summary.md`

Run the script on `/app/logs` so `/app/out/report.json` exists. Then write
`/app/out/summary.md`: an incident summary for the on-call channel, under 200 words,
saying what broke, when, how badly, and one concrete next step the logs support.
Label anything you infer but can't see in the logs as a hypothesis.
