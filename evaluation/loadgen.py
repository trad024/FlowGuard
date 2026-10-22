"""Load generator with a per-request log, the raw data for DSO3 canary decisions.

Sends GET / to each target at a fixed rate (open loop: a slow response never delays the next
request) and writes one JSON line per request:

    {"ts": "...Z", "namespace": "staging", "status": 200, "latency_ms": 3.1,
     "pod": "podinfo-6d8f7c9b5-x2x9k", "error": null}

Every request opens a new connection, so the NodePort spreads requests over all ready pods,
canary and stable alike. The pod name comes from the "hostname" field of podinfo's response.

    python evaluation/loadgen.py run --duration 600
    python evaluation/loadgen.py summary evaluation/runs/<file>.jsonl --namespace staging

Standard library only, so it runs on any laptop Python without installing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_TARGETS = {"staging": "http://localhost:9898/", "prod": "http://localhost:9899/"}
RUNS_DIR = Path(__file__).resolve().parent / "runs"


@dataclass
class Record:
    ts: str
    namespace: str
    status: int | None
    latency_ms: float
    pod: str | None
    error: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def pod_from_body(body: bytes) -> str | None:
    """The pod name from podinfo's JSON response, or None when the body is not that JSON."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    hostname = data.get("hostname") if isinstance(data, dict) else None
    return hostname if isinstance(hostname, str) and hostname else None


def pod_template_hash(pod: str | None) -> str | None:
    """'podinfo-6d8f7c9b5-x2x9k' -> '6d8f7c9b5': the ReplicaSet, so canary or stable."""
    if not pod:
        return None
    parts = pod.rsplit("-", 2)
    return parts[1] if len(parts) == 3 else None


def probe(namespace: str, url: str, timeout: float) -> Record:
    ts = utc_now()
    start = time.perf_counter()
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    status: int | None = None
    body = b""
    error = None
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error = type(exc).__name__ + ": " + str(getattr(exc, "reason", exc))
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    return Record(ts, namespace, status, latency_ms, pod_from_body(body), error)


class JsonlWriter:
    """Thread-safe JSONL writer, flushed per line so a killed run keeps its data."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, record: Record) -> None:
        line = json.dumps(asdict(record), separators=(",", ":"))
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        self._file.close()


def run(targets: dict[str, str], rate: float, duration: float, out: Path, timeout: float) -> int:
    """Send `rate` requests per second to each target for `duration` seconds (0 = until killed)."""
    writer = JsonlWriter(out)
    workers = max(8, int(rate * len(targets) * (timeout + 1)))
    sent = 0
    start = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while duration <= 0 or time.monotonic() - start < duration:
                for namespace, url in targets.items():
                    future = pool.submit(probe, namespace, url, timeout)
                    future.add_done_callback(lambda f: writer.write(f.result()))
                sent += 1
                # Fixed schedule from the start time, so drift never accumulates
                delay = start + sent / rate - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
    return sent * len(targets)


def read_records(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if line:
            yield json.loads(line)


def summarize(
    records: Iterable[dict],
    namespace: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Requests, 5xx and transport errors per pod-template hash within a time window."""
    summary: dict = {"requests": 0, "transport_errors": 0, "by_hash": {}}
    for r in records:
        if namespace and r["namespace"] != namespace:
            continue
        ts = parse_ts(r["ts"])
        if (since and ts < since) or (until and ts > until):
            continue
        summary["requests"] += 1
        if r["status"] is None:
            summary["transport_errors"] += 1
            continue
        key = pod_template_hash(r["pod"]) or "unknown"
        bucket = summary["by_hash"].setdefault(key, {"requests": 0, "status_5xx": 0, "other": 0})
        bucket["requests"] += 1
        if r["status"] >= 500:
            bucket["status_5xx"] += 1
        elif r["status"] >= 400:
            bucket["other"] += 1
    return summary


def parse_targets(values: list[str] | None) -> dict[str, str]:
    if not values:
        return dict(DEFAULT_TARGETS)
    targets = {}
    for value in values:
        namespace, sep, url = value.partition("=")
        if not sep or not namespace or not url:
            raise argparse.ArgumentTypeError(f"target must be namespace=url, got {value!r}")
        targets[namespace] = url
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="generate load and log every request")
    p_run.add_argument("--rate", type=float, default=10.0, help="requests/s per target")
    p_run.add_argument("--duration", type=float, default=0, help="seconds, 0 = until killed")
    p_run.add_argument("--target", action="append", help="namespace=url, repeatable")
    p_run.add_argument("--timeout", type=float, default=5.0, help="per-request timeout, s")
    p_run.add_argument("--out", type=Path, help="JSONL file (default evaluation/runs/...)")

    p_sum = sub.add_parser("summary", help="count requests per pod-template hash")
    p_sum.add_argument("file", type=Path)
    p_sum.add_argument("--namespace")
    p_sum.add_argument("--since", type=parse_ts, help="ISO 8601 UTC")
    p_sum.add_argument("--until", type=parse_ts, help="ISO 8601 UTC")

    args = parser.parse_args(argv)
    if args.command == "run":
        if args.rate <= 0:
            parser.error("--rate must be positive")
        out = args.out or RUNS_DIR / f"loadgen-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
        targets = parse_targets(args.target)
        print(f"Logging to {out}", flush=True)
        total = run(targets, args.rate, args.duration, out, args.timeout)
        print(f"Sent {total} requests", flush=True)
    else:
        with args.file.open(encoding="utf-8") as f:
            summary = summarize(read_records(f), args.namespace, args.since, args.until)
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
