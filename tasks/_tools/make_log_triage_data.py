"""Generate the log files and expected reports for tasks/log-triage.

    python3 tasks/_tools/make_log_triage_data.py

Writes three deterministic log sets: the one the agent sees (environment/app/logs) and
two hidden ones the grader re-runs the agent's script on (tests/holdout_*). Each
set's expected report is computed twice, from the generator's own record of what it
wrote and by the reference solution parsing the files, and the two must agree.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import math
import random
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent / "log-triage"


@dataclass
class LogSet:
    lines: list[str] = field(default_factory=list)
    valid: list[tuple[datetime, str, int, int]] = field(default_factory=list)  # (time, path, status, ms)
    malformed: int = 0

    def add(self, rng: random.Random, when: datetime, path: str, status: int, ms: int, query: str = "") -> None:
        ip = f"10.0.{rng.randrange(8)}.{rng.randrange(2, 250)}"
        method = rng.choice(["GET", "GET", "GET", "POST"]) if path != "/healthz" else "GET"
        self.lines.append(f"{when:%Y-%m-%dT%H:%M:%SZ} {ip} {method} {path}{query} {status} {ms}ms")
        self.valid.append((when, path, status, ms))


def latency(rng: random.Random, base: float, sigma: float = 0.45) -> int:
    return max(1, int(base * rng.lognormvariate(0, sigma)))


def normal_status(rng: random.Random) -> int:
    return rng.choices([200, 201, 304, 401, 404, 500, 502], [86, 4, 3, 2, 4, 0.6, 0.4])[0]


def at(start: datetime, minute: int, rng: random.Random) -> datetime:
    return start + timedelta(minutes=minute, seconds=rng.randrange(60))


MALFORMED = [
    "{t} 10.0.1.9 GET /api/orders 200",
    "{d} 14:05:12 10.0.1.9 GET /api/orders 200 12ms",
    "{t} 10.0.1.9 GET /api/orders 2OO 31ms",
    "{t} 10.0.1.9 GET /api/orders 200 31",
    "{t} 10.0.1.9 GET /api/orders 200 31 ms",
    "{t} 10.0.1.9 get /api/orders 200 31ms",
    "{t} 10.0.1.9 GET api/orders 200 31ms",
    "{d}T25:05:12Z 10.0.1.9 GET /api/orders 200 31ms",
    "{t} 10.0.1.9 GET /api/orders 200 -5ms",
    "{t} 10.0.1.9 GET /api/orders 099 31ms",
    "{t}  10.0.1.9 GET /api/orders 200 31ms",
    "{t} 10.0.1.9 GET /api/orders 200 31ms extra",
    "### log rotated ###",
    "{d}T14:0",
]


def sprinkle_malformed(rng: random.Random, logs: LogSet, day: str, count: int, blanks: int = 0) -> None:
    """Insert malformed lines (and blank ones, which don't count) at random positions."""
    for i in range(count):
        t = f"{day}T{rng.randrange(24):02d}:{rng.randrange(60):02d}:{rng.randrange(60):02d}Z"
        logs.malformed += 1
        logs.lines.insert(rng.randrange(len(logs.lines) + 1), MALFORMED[i % len(MALFORMED)].format(t=t, d=day))
    for _ in range(blanks):
        logs.lines.insert(rng.randrange(len(logs.lines) + 1), "")


def main_set() -> LogSet:
    """2026-11-03 14:00-15:00: /api/checkout fails with 503s from about 14:21 to 14:33,
    with a one-minute partial recovery at 14:27 and a low-traffic blip at 14:45."""
    rng = random.Random(20261103)
    start = datetime(2026, 11, 3, 14, 0)
    paths = {"/api/orders": 80, "/api/checkout": 180, "/api/search": 120, "/api/users/me": 40,
             "/api/cart": 60, "/healthz": 3}
    weights = {"/api/orders": 25, "/api/checkout": 12, "/api/search": 22, "/api/users/me": 18,
               "/api/cart": 13, "/healthz": 10}
    logs = LogSet()
    for m in range(60):
        incident = 21 <= m <= 33
        recovering = m == 27
        w = dict(weights)
        if incident:
            w["/api/checkout"] = 70  # clients retrying checkout
        n = 12 if m == 45 else rng.randint(36, 52)
        for i in range(n):
            path = rng.choices(list(w), list(w.values()))[0]
            status, ms = normal_status(rng), latency(rng, paths[path])
            if path == "/api/checkout" and incident and rng.random() < (0.1 if recovering else 0.8):
                status, ms = 503, rng.randint(2000, 6000)
            if m == 20 and path == "/api/checkout" and rng.random() < 0.3:
                status, ms = 503, rng.randint(1500, 3000)
            if m == 45 and i < 6:
                status = 500
            query = ""
            if path in ("/api/orders", "/api/search") and rng.random() < 0.25:
                query = f"?page={rng.randint(1, 9)}" if path == "/api/orders" else f"?q=item{rng.randint(1, 500)}"
            logs.add(rng, at(start, m, rng), path, status, ms, query)
    sprinkle_malformed(rng, logs, "2026-11-03", 18, blanks=4)
    return logs


def holdout_incident_set() -> LogSet:
    """2026-12-08 09:00-09:40, different paths: /v2/search times out (504/500) 09:12-09:19."""
    rng = random.Random(20261208)
    start = datetime(2026, 12, 8, 9, 0)
    paths = {"/v2/search": 150, "/v2/items": 70, "/v2/login": 220, "/metrics": 5}
    weights = {"/v2/search": 30, "/v2/items": 40, "/v2/login": 15, "/metrics": 15}
    logs = LogSet()
    for m in range(40):
        incident = 12 <= m <= 19
        for _ in range(rng.randint(25, 40)):
            path = rng.choices(list(weights), list(weights.values()))[0]
            status, ms = normal_status(rng), latency(rng, paths[path], 0.6)
            if incident and path == "/v2/search" and rng.random() < 0.95:
                status, ms = rng.choice([504, 504, 500]), rng.randint(9000, 10000)
            query = f"?q=w{rng.randint(1, 99)}" if path == "/v2/search" and rng.random() < 0.5 else ""
            logs.add(rng, at(start, m, rng), path, status, ms, query)
    sprinkle_malformed(rng, logs, "2026-12-08", 9)
    return logs


def holdout_quiet_set() -> LogSet:
    """2027-01-15 22:00-22:20, no incident: 22:05 has exactly 20 requests and exactly 25%
    5xx (not more than 25%), and 22:11 has 19 requests, all 502 (under 20 requests).
    /api/pay and /api/zeta tie on 5xx count; /api/alpha comes fourth."""
    rng = random.Random(20270115)
    start = datetime(2027, 1, 15, 22, 0)
    paths = {"/api/pay": 90, "/api/feed": 60, "/api/zeta": 30, "/api/alpha": 45}
    logs = LogSet()
    zeta_errors, alpha_errors = [3, 7, 9, 14, 17], [4, 15]
    for m in range(20):
        if m == 5:
            for i in range(20):
                logs.add(rng, at(start, m, rng), "/api/pay", 503 if i < 5 else 200, latency(rng, 90))
            continue
        if m == 11:
            for _ in range(19):
                logs.add(rng, at(start, m, rng), "/api/feed", 502, rng.randint(100, 300))
            continue
        for i in range(rng.randint(26, 34)):
            path = rng.choice(list(paths))
            status = rng.choice([200, 200, 200, 304, 404])
            if i == 0 and m in zeta_errors:
                path, status = "/api/zeta", 500
            elif i == 0 and m in alpha_errors:
                path, status = "/api/alpha", 500
            logs.add(rng, at(start, m, rng), path, status, latency(rng, paths[path]))
    return logs


def expected_from_truth(logs: LogSet) -> dict:
    """The report computed from what the generator wrote, without parsing any text."""
    classes = Counter({"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0})
    by_path, errors, minutes = defaultdict(list), Counter(), defaultdict(lambda: [0, 0])
    for when, path, status, ms in logs.valid:
        classes[f"{status // 100}xx"] += 1
        by_path[path].append(ms)
        key = when.replace(second=0)
        minutes[key][0] += 1
        if status >= 500:
            errors[path] += 1
            minutes[key][1] += 1
    bad = sorted(k for k, (n, e) in minutes.items() if n >= 20 and e / n > 0.25)
    return {
        "total_requests": len(logs.valid),
        "malformed_lines": logs.malformed,
        "status_classes": dict(classes),
        "p95_latency_ms": {p: sorted(v)[math.ceil(0.95 * len(v)) - 1] for p, v in sorted(by_path.items())},
        "top_5xx_paths": [{"path": p, "count": c} for p, c in sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))[:3]],
        "incident": {"start": f"{bad[0]:%Y-%m-%dT%H:%M:00Z}",
                     "end": f"{bad[-1] + timedelta(minutes=1):%Y-%m-%dT%H:%M:00Z}"} if bad else None,
    }


def write(logs: LogSet, out: Path, files: list[str], rng: random.Random, *, rotate_at: str | None = None) -> None:
    """Split the lines across files. With rotate_at (a timestamp prefix), the log is
    kept in time order and rotates at the first line with that prefix; otherwise the
    lines are shuffled and split evenly, so nothing can rely on their order."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    lines = list(logs.lines)
    if rotate_at:
        cut = next(i for i, line in enumerate(lines) if line.startswith(rotate_at))
        chunks = [lines[:cut], lines[cut:]]
    else:
        rng.shuffle(lines)
        size = math.ceil(len(lines) / len(files))
        chunks = [lines[i * size:(i + 1) * size] for i in range(len(files))]
    assert len(chunks) == len(files)
    for name, chunk in zip(files, chunks):
        text = "\n".join(chunk) + "\n"
        if name.endswith(".gz"):
            with gzip.open(out / name, "wt", encoding="utf-8") as f:
                f.write(text)
        else:
            (out / name).write_text(text)


def reference():
    spec = importlib.util.spec_from_file_location("triage", TASK / "solution" / "triage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    triage = reference()
    rng = random.Random(7)
    sets = [
        ("expected_main.json", main_set(), TASK / "environment" / "app" / "logs",
         ["access-1.log", "access-2.log.gz"], "2026-11-03T14:30"),
        ("expected_holdout_incident.json", holdout_incident_set(), TASK / "tests" / "holdout_incident",
         ["a.log.gz", "b.log", "c.log"], None),
        ("expected_holdout_quiet.json", holdout_quiet_set(), TASK / "tests" / "holdout_quiet",
         ["quiet.log"], None),
    ]
    for name, logs, out, files, rotate_at in sets:
        write(logs, out, files, rng, rotate_at=rotate_at)
        truth = expected_from_truth(logs)
        parsed = triage.triage(out)
        assert parsed == truth, f"{name}: reference solution disagrees with the generator"
        (TASK / "tests" / name).write_text(json.dumps(truth, indent=2) + "\n")
        print(f"{name}: {truth['total_requests']} requests, {truth['malformed_lines']} malformed, "
              f"incident {truth['incident']}, top {truth['top_5xx_paths']}")
    # Not a log file: the spec says to read only *.log and *.log.gz.
    (TASK / "environment" / "app" / "logs" / "README.txt").write_text(
        "Access logs, rotated at 14:30. Line format example:\n"
        "2026-11-03T14:05:12Z 10.0.4.17 GET /api/orders?page=2 200 87ms\n"
    )


if __name__ == "__main__":
    main()
