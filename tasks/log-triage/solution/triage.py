"""Summarize API access logs: request counts, p95 latency per path, 5xx hot spots, and
the incident window. Reference solution for pyswe/log-triage.

    python3 triage.py --logs /app/logs --out /app/out
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LINE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) (\S+) ([A-Z]+) (/\S*) ([2-5]\d\d) (\d+)ms")
MIN_REQUESTS = 20
BAD_SHARE = 0.25


def read_lines(logs: Path):
    for path in sorted(logs.iterdir()):
        if path.name.endswith(".log.gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                yield from f
        elif path.name.endswith(".log"):
            with path.open(encoding="utf-8") as f:
                yield from f


def parse(line: str):
    m = LINE.fullmatch(line)
    if not m:
        return None
    ts, _ip, _method, path, status, latency = m.groups()
    try:
        when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return when, path.split("?", 1)[0], int(status), int(latency)


def p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def minute(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:00Z")


def triage(logs: Path) -> dict:
    total = malformed = 0
    classes = Counter({"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0})
    latencies: dict[str, list[int]] = defaultdict(list)
    errors_by_path: Counter = Counter()
    per_minute: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])  # [requests, 5xx]

    for raw in read_lines(logs):
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        parsed = parse(line)
        if parsed is None:
            malformed += 1
            continue
        when, path, status, latency = parsed
        total += 1
        classes[f"{status // 100}xx"] += 1
        latencies[path].append(latency)
        bucket = per_minute[when.replace(second=0)]
        bucket[0] += 1
        if status >= 500:
            errors_by_path[path] += 1
            bucket[1] += 1

    bad = sorted(m for m, (n, err) in per_minute.items() if n >= MIN_REQUESTS and err / n > BAD_SHARE)
    incident = None
    if bad:
        incident = {"start": minute(bad[0]), "end": minute(bad[-1] + timedelta(minutes=1))}
    top = sorted(errors_by_path.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return {
        "total_requests": total,
        "malformed_lines": malformed,
        "status_classes": dict(classes),
        "p95_latency_ms": {path: p95(v) for path, v in sorted(latencies.items())},
        "top_5xx_paths": [{"path": p, "count": c} for p, c in top],
        "incident": incident,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--logs", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = triage(args.logs)
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
