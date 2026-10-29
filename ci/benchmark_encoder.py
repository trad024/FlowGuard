"""Measure a trained encoder on the target CPU with a labelled smoke or validation workload."""

import argparse
import json
import math
import platform
import statistics
import threading
import time
from pathlib import Path

from encoder_data import load_examples
from flowguard.encoder import EncoderPredictor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--log-root", type=Path)
    args = parser.parse_args()
    if args.iterations < 10:
        raise SystemExit("at least ten measured iterations required")
    if (args.index is None) != (args.log_root is None):
        raise SystemExit("index and log-root must be supplied together")
    texts = ["go: checksum mismatch while downloading a dependency module"]
    variants = []
    if args.index is not None:
        base = json.loads((Path(__file__).parent / "encoder/base-model.json").read_text())
        examples = load_examples(
            Path("evaluation/datasets/corpus-split.json"),
            args.index,
            args.log_root,
            base["split_sha256"],
        )
        validation = [e for e in examples if e.split == "validation"]
        if not validation:
            raise SystemExit("validation logs required")
        texts = [e.text for e in validation]
        variants = [e.variant for e in validation]
    import psutil
    import torch

    torch.set_num_threads(2)
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak, stop = [baseline], threading.Event()

    def sample():
        while not stop.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        predictor = EncoderPredictor.from_directory(args.model)
        for i in range(5):
            predictor.predict(texts[i % len(texts)], "warmup")
        timings = []
        for i in range(args.iterations):
            start = time.perf_counter()
            predictor.predict(texts[i % len(texts)], "benchmark")
            timings.append(1000 * (time.perf_counter() - start))
        peak[0] = max(peak[0], process.memory_info().rss)
    finally:
        stop.set()
        sampler.join()
    result = {
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "threads": 2,
        "iterations": len(timings),
        "warmup_iterations": 5,
        "latency_median_ms": statistics.median(timings),
        "latency_p95_ms": sorted(timings)[math.ceil(len(timings) * 0.95) - 1],
        "baseline_rss_mib": baseline / 2**20,
        "sampled_peak_rss_mib": peak[0] / 2**20,
        "peak_increment_mib": (peak[0] - baseline) / 2**20,
        "scope": "process RSS sampled every 10ms, including checkpoint load",
        "workload": "verified validation logs, cyclic order" if variants else "fixed short log",
        "validation_variants": variants,
        "input_characters": [len(text) for text in texts],
        "latencies_ms": timings,
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
