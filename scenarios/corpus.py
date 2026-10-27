"""Failure corpus (D6, D34): every variant of every CI failure template, run locally like app-ci.

    python scenarios/corpus.py run            # resumable: finished variants are skipped
    python scenarios/corpus.py split          # assign templates to train/validation/test
    python scenarios/corpus.py summary

Each variant is applied to an export of HEAD and the jobs run in app-ci's order (build, unit,
then the variant's optional job) until the first failure, as CI does. The failing job's log
and its metadata go to evaluation/runs/corpus/<variant>/ (gitignored): the class and template
are ground truth and never part of the log. A variant whose first failing job or error text
does not match its template is kept but marked unverified and left out of the split.

The split is by template (D6, D34), seeded, within each class: about 60% of the templates for
training, 20% validation, 20% test, so no variant of a test template is ever seen in training.
It is written to evaluation/datasets/corpus-split.json (committed) and the test templates to
the pre-registration's families.ci.templates.
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios")]

import ci_failures  # noqa: E402
import verify_templates as vt  # noqa: E402

OUT = ROOT / "evaluation" / "runs" / "corpus"
SPLIT_FILE = ROOT / "evaluation" / "datasets" / "corpus-split.json"
PREREG = ROOT / "docs" / "preregistration.yaml"
SPLIT_SEED = "corpus-split-20261027"
OPTIONAL = {"optional (lint)", "optional (integration)"}


def jobs_for(variant: ci_failures.Template) -> list[str]:
    """app-ci's order up to the variant's job: build, unit, then its optional job."""
    return ["build", "unit"] + ([variant.failing_job] if variant.failing_job in OPTIONAL else [])


def run_variant(variant: ci_failures.Template, attempts: int) -> dict:
    """Run the jobs until the first failure; retry a run that fails nowhere (flaky tests)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        vt.export_head(src)
        variant.apply(src)
        tag = f"podinfo-check:{secrets.token_hex(4)}"
        toolchain = "auto" if variant.id.startswith("environment-05") else "local"
        tries = []
        for attempt in range(1, attempts + 1):
            failed_job, log, durations = None, "", {}
            for job in jobs_for(variant):
                started = time.monotonic()
                code, out = vt.check(src, job, tag, toolchain, no_cache=False)
                durations[job] = round(time.monotonic() - started, 1)
                if code != 0:
                    failed_job, log = job, out
                    break
            tries.append({"attempt": attempt, "failed_job": failed_job, "seconds": durations})
            if failed_job:
                break
        vt.run(["docker", "rmi", "-f", tag])
    matches = failed_job == variant.failing_job and variant.expect in log
    return {
        "variant": variant.id,
        "template": ci_failures.template_of(variant.id),
        "root_cause": variant.root_cause,
        "failing_job": variant.failing_job,
        "first_failed_job": failed_job,
        "expected_text_seen": bool(failed_job) and variant.expect in log,
        "verified": matches,
        "attempts": tries,
        "log": log,
    }


def run(ids: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for vid in ids:
        out = OUT / vid
        if (out / "meta.json").is_file():
            continue
        variant = ci_failures.ALL_VARIANTS[vid]
        attempts = 3 if variant.root_cause == "flaky_test" else 1
        result = run_variant(variant, attempts)
        out.mkdir(parents=True, exist_ok=True)
        if result["first_failed_job"]:
            safe = result["first_failed_job"].replace("optional (", "").rstrip(")")
            (out / f"{safe}.log").write_text(result["log"], encoding="utf-8")
        meta = {k: v for k, v in result.items() if k != "log"}
        (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print(
            json.dumps({k: meta[k] for k in ("variant", "first_failed_job", "verified")}),
            flush=True,
        )


def results() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(OUT.glob("*/meta.json"))]


def split(templates_by_class: dict[str, list[str]], seed: str = SPLIT_SEED) -> dict[str, str]:
    """template id -> train / validation / test, by template within each class."""
    out = {}
    for cls in sorted(templates_by_class):
        ids = sorted(templates_by_class[cls])
        random.Random(f"{seed}:{cls}").shuffle(ids)
        n_test = max(1, round(0.2 * len(ids)))
        n_val = max(1, round(0.2 * len(ids)))
        for i, tid in enumerate(ids):
            out[tid] = "test" if i < n_test else "validation" if i < n_test + n_val else "train"
    return out


def write_split() -> dict:
    verified = [r for r in results() if r["verified"]]
    by_class: dict[str, set[str]] = {}
    for r in verified:
        by_class.setdefault(r["root_cause"], set()).add(r["template"])
    assignment = split({c: sorted(t) for c, t in by_class.items()})
    variants = {
        r["variant"]: {"template": r["template"], "root_cause": r["root_cause"]} for r in verified
    }
    doc = {
        "seed": SPLIT_SEED,
        "templates": dict(sorted(assignment.items())),
        "variants": dict(sorted(variants.items())),
        "unverified": sorted(r["variant"] for r in results() if not r["verified"]),
    }
    SPLIT_FILE.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8", newline="\n")
    test = sorted(v for v, info in variants.items() if assignment[info["template"]] == "test")
    text = PREREG.read_text(encoding="utf-8")
    old = (
        "    templates: []                "
        "# test-split template ids (D6 split by template), set in 4b\n"
    )
    if old in text:
        rows = "".join(f"      - {v}\n" for v in test)
        new = (
            "    templates:                   # test-split variants (D6, D34; corpus-split.json)\n"
            + rows
        )
        PREREG.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return doc


def summary() -> dict:
    rs = results()
    return {
        "variants_run": len(rs),
        "verified": sum(r["verified"] for r in rs),
        "by_class": dict(Counter(r["root_cause"] for r in rs if r["verified"])),
        "unverified": {r["variant"]: r["first_failed_job"] for r in rs if not r["verified"]},
        "seconds_per_variant_median": sorted(
            sum(sum(a["seconds"].values()) for a in r["attempts"]) for r in rs
        )[len(rs) // 2]
        if rs
        else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("variants", nargs="*")
    sub.add_parser("split")
    sub.add_parser("summary")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        run(args.variants or sorted(ci_failures.ALL_VARIANTS))
        print(json.dumps(summary(), indent=1))
    elif args.cmd == "split":
        doc = write_split()
        print(json.dumps(Counter(doc["templates"].values())))
    else:
        print(json.dumps(summary(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
