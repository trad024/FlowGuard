"""Scored results as Prometheus gauges, for the agents-vs-L0 Grafana dashboard.

    python evaluation/results_exporter.py [--host 127.0.0.1] [--port 9465]     # GET /metrics

Reads the result files the evaluation writes in runs/ (l0-baseline.json, dso1-replay.json, and
any later <name>.json listed in SOURCES) at every scrape. Each rate block (k, n, rate, ci95), each
per-hour block (episodes, hours, rate, ci95) and each median becomes

    flowguard_result{source, objective, level, band, variant, metric, stat, path}

with stat one of value, ci_low, ci_high, k, n (k = episodes and n = hours for a per-hour rate).
`path` is the dotted position in the file, so every series is unique; the other labels are
parsed from it for the dashboard. Scoring side: nothing under agents/ reads this.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evaluation" / "runs"
PORT = 9465
SOURCES = {"l0-baseline": "l0-baseline.json", "dso1-replay": "dso1-replay.json"}
LEVELS = {"L0", "L1", "L2", "L3", "L4", "L5", "detector"}
VARIANTS = {"without_carry_over", "leftover_sensitivity", "sensitivity"}


def labels_of(source: str, path: list[str]) -> dict[str, str]:
    level = next((p for p in path if p in LEVELS), "L0" if source == "l0-baseline" else "")
    band = next((path[i + 1] for i, p in enumerate(path[:-1]) if "by_band" in p), "")
    objective = next((p for p in path if p.startswith("DSO")), "")
    if source == "dso1-replay":
        objective = "DSO1 CI" if path[:1] == ["ci"] else "DSO1 runtime"
    variant = next((p for p in path if p in VARIANTS), "primary")
    metric = next((p for p in reversed(path) if p not in LEVELS and p != band), path[-1])
    if metric.endswith("by_band"):
        metric = metric.removesuffix("_by_band") or "recall"
    return {
        "source": source,
        "objective": objective,
        "level": level,
        "band": band,
        "variant": variant,
        "metric": metric,
    }


def flatten(source: str, node, path: list[str] | None = None) -> list[tuple[dict, float]]:
    path = path or []
    out: list[tuple[dict, float]] = []
    if isinstance(node, dict):
        if {"rate", "ci95"} <= node.keys() and node["rate"] is not None:
            base = labels_of(source, path)
            ci = node["ci95"] or [None, None]
            stats = {"value": node["rate"], "ci_low": ci[0], "ci_high": ci[1]}
            stats["k"] = node.get("k", node.get("episodes"))
            stats["n"] = node.get("n", node.get("hours"))
            for stat, value in stats.items():
                if isinstance(value, (int, float)):
                    out.append(({**base, "stat": stat, "path": ".".join(path)}, float(value)))
            return out
        for key, value in node.items():
            out += flatten(source, value, [*path, str(key)])
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and path:
        if "median" in path[-1] or path[-1] in ("p_value", "mcnemar_exact_p"):
            base = labels_of(source, path)
            out.append(({**base, "stat": "value", "path": ".".join(path)}, float(node)))
    return out


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(runs: Path = RUNS) -> str:
    lines = [
        "# HELP flowguard_result A scored result (rate, interval, count or median) by level.",
        "# TYPE flowguard_result gauge",
    ]
    present = []
    for source, name in SOURCES.items():
        path = runs / name
        if not path.is_file():
            continue
        present.append(source)
        for labels, value in flatten(source, json.loads(path.read_text(encoding="utf-8"))):
            inner = ",".join(f'{k}="{escape(v)}"' for k, v in labels.items())
            lines.append(f"flowguard_result{{{inner}}} {value:g}")
    lines += [
        "# HELP flowguard_result_files Result files found.",
        "# TYPE flowguard_result_files gauge",
    ]
    lines.append(f"flowguard_result_files {len(present)}")
    return "\n".join([*lines, ""])


def serve(host: str = "127.0.0.1", port: int = PORT, runs: Path = RUNS):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            if self.path.split("?")[0] != "/metrics":
                self.send_error(404)
                return
            body = render(runs).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    server = serve(args.host, args.port)
    print(f"serving http://{args.host}:{server.server_address[1]}/metrics", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
