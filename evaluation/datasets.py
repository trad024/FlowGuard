"""External datasets (D14): download with checksums, a reproducible LogChunks sample, and the
blank labelling sheet for the two human labellers.

    python evaluation/datasets.py download                  # LogChunks (24 MB)
    python evaluation/datasets.py download --with-logsieve  # also LogSieve Data.zip (1 GB)
    python evaluation/datasets.py sample                    # 150 LogChunks logs, seeded
    python evaluation/datasets.py sheet                     # blank labelling sheet

Raw files go to evaluation/datasets/raw/ (gitignored). Every archive is checked against the
size and MD5 published by its repository before it is extracted. The data is only read as
text, never executed. What is committed: the sample list (identifiers and SHA-256 of each log,
no log text) and the blank sheet.

Datasets, both CC BY 4.0:
  LogChunks  Brandt et al., MSR 2020, Zenodo 10.5281/zenodo.3632351: 797 failed Travis CI logs
             from 80 repositories, each with a hand-labelled chunk explaining the failure.
  LogSieve   Figshare 10.6084/m9.figshare.30811382 (Data.zip): GitHub Actions logs with
             line-level relevance labels.
Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import random
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evaluation" / "datasets"
RAW = DATA / "raw"
SAMPLE_CSV = DATA / "logchunks-sample-150.csv"
SHEET_CSV = DATA / "labelling" / "logchunks-labels-blank.csv"
SEED = 20261026
SAMPLE_SIZE = 150
ROOT_CAUSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")


@dataclass(frozen=True)
class Archive:
    name: str
    url: str
    size: int
    md5: str
    citation: str


LOGCHUNKS = Archive(
    "LogChunks.zip",
    "https://zenodo.org/api/records/3632351/files/LogChunks.zip/content",
    24108826,
    "aafa45079bdae44e340f4474ca5c4340",
    "Brandt, Panichella, Zaidman, Beller. LogChunks: A Data Set for Build Log Analysis. "
    "MSR 2020. doi:10.5281/zenodo.3632351. CC BY 4.0",
)
LOGSIEVE = Archive(
    "LogSieve-Data.zip",
    "https://ndownloader.figshare.com/files/60281384",
    996738697,
    "211faad8b6eee91d077ac268e6750074",
    "LogSieve: Task-Aware CI Log Reduction for Sustainable LLM-Based Analysis (Replication "
    "Package). doi:10.6084/m9.figshare.30811382.v1. CC BY 4.0",
)


# ---------------------------------------------------------------- download


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(archive: Archive, dest_dir: Path = RAW) -> Path:
    """Download unless a verified copy is already there; verify size and MD5 either way."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / archive.name
    if not (dest.is_file() and dest.stat().st_size == archive.size):
        tmp = dest.with_suffix(".part")
        req = urllib.request.Request(archive.url, headers={"User-Agent": "flowguard-datasets"})
        with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as out:
            for block in iter(lambda: r.read(1 << 20), b""):
                out.write(block)
        tmp.replace(dest)
    verify(dest, archive)
    return dest


def verify(path: Path, archive: Archive) -> None:
    size = path.stat().st_size
    if size != archive.size:
        raise ValueError(f"{path.name}: size {size}, expected {archive.size}")
    digest = md5_of(path)
    if digest != archive.md5:
        raise ValueError(f"{path.name}: md5 {digest}, expected {archive.md5}")


def safe_extract(zip_path: Path, dest: Path, keep=lambda name: True) -> int:
    """Extract members that pass `keep`, refusing absolute paths and '..' (zip slip)."""
    dest = dest.resolve()
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if info.is_dir() or "__MACOSX" in parts or parts[-1] == ".DS_Store" or not keep(name):
                continue
            target = (dest / name).resolve()
            if name.startswith("/") or ".." in parts or dest not in target.parents:
                raise ValueError(f"unsafe path in archive: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:  # CRC-32 checked on read
                out.write(src.read())
            n += 1
    return n


# ---------------------------------------------------------------- LogChunks sample


@dataclass(frozen=True)
class LabelledLog:
    log_path: str  # relative to LogChunks/logs, e.g. C++/bitcoin@bitcoin/failed/567118690.log
    language: str
    repository: str
    build_id: str
    category: str


def labelled_logs(logchunks_dir: Path) -> list[LabelledLog]:
    """Every failed log that has a labelled chunk and exists on disk, deduplicated."""
    found: dict[str, LabelledLog] = {}
    for xml in sorted((logchunks_dir / "build-failure-reason").rglob("*.xml")):
        for example in ET.parse(xml).getroot().iter("Example"):
            rel = (example.findtext("Log") or "").strip()
            parts = rel.split("/")
            if len(parts) != 4 or parts[2] != "failed" or rel in found:
                continue
            if not (logchunks_dir / "logs" / rel).is_file():
                continue
            found[rel] = LabelledLog(
                rel,
                parts[0],
                parts[1],
                parts[3].removesuffix(".log"),
                (example.findtext("Category") or "").strip(),
            )
    return sorted(found.values(), key=lambda x: x.log_path)


def draw_sample(logs: list[LabelledLog], size: int = SAMPLE_SIZE, seed: int = SEED):
    """Seeded and spread over repositories: shuffle each repository's logs, then take one per
    repository in rounds (repository order shuffled too) until `size` logs are drawn."""
    rng = random.Random(seed)
    by_repo: dict[str, list[LabelledLog]] = defaultdict(list)
    for log in logs:
        by_repo[log.repository].append(log)
    repos = sorted(by_repo)
    rng.shuffle(repos)
    for repo in repos:
        rng.shuffle(by_repo[repo])
    picked: list[LabelledLog] = []
    while len(picked) < min(size, len(logs)):
        for repo in repos:
            if by_repo[repo] and len(picked) < size:
                picked.append(by_repo[repo].pop())
    return picked


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_sample(picked: list[LabelledLog], logchunks_dir: Path, out: Path = SAMPLE_CSV) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "sample_id",
                "log_path",
                "language",
                "repository",
                "build_id",
                "logchunks_category",
                "log_sha256",
            ]
        )
        for i, log in enumerate(picked, 1):
            w.writerow(
                [
                    f"LC{i:03d}",
                    log.log_path,
                    log.language,
                    log.repository,
                    log.build_id,
                    log.category,
                    sha256_of(logchunks_dir / "logs" / log.log_path),
                ]
            )


def write_sheet(sample_csv: Path = SAMPLE_CSV, out: Path = SHEET_CSV) -> None:
    """One row per sampled log; each labeller fills their own copy, without seeing the other."""
    with sample_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(
            [
                "sample_id",
                "log_path",
                "labeller",
                "root_cause",
                "confidence_1_to_3",
                "evidence_lines",
                "notes",
            ]
        )
        for row in rows:
            w.writerow([row["sample_id"], row["log_path"], "", "", "", "", ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_dl = sub.add_parser("download", help="download, verify and extract")
    p_dl.add_argument("--with-logsieve", action="store_true", help="also LogSieve Data.zip (1 GB)")
    sub.add_parser("sample", help=f"draw the {SAMPLE_SIZE}-log LogChunks sample (seed {SEED})")
    sub.add_parser("sheet", help="write the blank labelling sheet from the sample")
    args = parser.parse_args(argv)

    if args.command == "download":
        path = download(LOGCHUNKS)
        n = safe_extract(path, RAW)
        print(f"{LOGCHUNKS.name}: md5 ok, {n} files extracted")
        if args.with_logsieve:
            path = download(LOGSIEVE)
            print(f"{LOGSIEVE.name}: md5 ok ({path}); extract the labelled logs as needed")
        return 0
    logchunks_dir = RAW / "LogChunks"
    if args.command == "sample":
        logs = labelled_logs(logchunks_dir)
        picked = draw_sample(logs)
        write_sample(picked, logchunks_dir)
        repos = len({p.repository for p in picked})
        print(
            f"{len(picked)} of {len(logs)} labelled failed logs, {repos} repositories"
            f" -> {SAMPLE_CSV}"
        )
        return 0
    write_sheet()
    print(f"blank sheet -> {SHEET_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
