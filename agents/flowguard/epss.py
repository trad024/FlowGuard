"""Offline FIRST EPSS snapshot reader. Incident handling never downloads threat data."""

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    date: str
    scores: dict[str, float]
    sha256: str
    model_version: str


def parse(data: bytes, expected_date: str):
    date.fromisoformat(expected_date)
    text = data.decode("utf-8")
    header = text.splitlines()[0] if text else ""
    published = re.search(r"score_date:([0-9]{4}-[0-9]{2}-[0-9]{2})", header)
    model = re.search(r"model_version:([^,\s]+)", header)
    if not published or published[1] != expected_date or not model:
        raise ValueError("EPSS publication date/model header mismatch")
    scores = {}
    rows = csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#"))
    if rows.fieldnames != ["cve", "epss", "percentile"]:
        raise ValueError("unsupported FIRST EPSS CSV columns")
    for row in rows:
        cve = row["cve"]
        probability, percentile = float(row["epss"]), float(row["percentile"])
        if (
            not re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", cve)
            or cve in scores
            or not all(math.isfinite(v) and 0 <= v <= 1 for v in (probability, percentile))
        ):
            raise ValueError("invalid or duplicate EPSS row")
        scores[cve] = probability
    if not scores:
        raise ValueError("empty EPSS snapshot")
    return Snapshot(expected_date, scores, hashlib.sha256(data).hexdigest(), model[1])


def load(path: Path):
    path = Path(path)
    metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("format") != 1:
        raise ValueError("unsupported EPSS provenance manifest")
    snapshot = parse(path.read_bytes(), metadata["date"])
    if snapshot.sha256 != metadata["csv_sha256"]:
        raise ValueError("EPSS CSV checksum mismatch")
    return snapshot
