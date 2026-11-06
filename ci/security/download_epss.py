"""Download and cache a dated FIRST daily EPSS CSV with compressed/plaintext SHA-256."""

import argparse
import gzip
import hashlib
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.request import urlopen

from flowguard.epss import load, parse


def fetch(day, cache, expected_sha256=None, open_url=urlopen):
    day = date.fromisoformat(day).isoformat()
    cache = Path(cache)
    csv_path = cache / f"{day}.csv"
    compressed = cache / f"{day}.csv.gz"
    manifest = csv_path.with_suffix(".json")
    if any(p.exists() for p in (csv_path, compressed, manifest)):
        snapshot = load(csv_path)
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
        if digest != metadata["gzip_sha256"] or snapshot.date != day:
            raise ValueError("cached EPSS source checksum/date mismatch")
    else:
        url = f"https://epss.empiricalsecurity.com/epss_scores-{day}.csv.gz"
        with open_url(url, timeout=60) as response:
            raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            raise ValueError("EPSS compressed download exceeds limit")
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise ValueError("downloaded EPSS source checksum mismatch")
        data = gzip.decompress(raw)
        if len(data) > 100_000_000:
            raise ValueError("EPSS plaintext exceeds limit")
        snapshot = parse(data, day)
        metadata = {
            "format": 1,
            "date": day,
            "model_version": snapshot.model_version,
            "source_url": url,
            "source_documentation": "https://www.first.org/epss/data.html",
            "fetched_at": datetime.now(UTC).isoformat(),
            "gzip_sha256": digest,
            "csv_sha256": snapshot.sha256,
            "rows": len(snapshot.scores),
            "sample": False,
        }
        cache.mkdir(parents=True, exist_ok=True)
        compressed.write_bytes(raw)
        csv_path.write_bytes(data)
        manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("cached EPSS source differs from expected checksum")
    # Date-stamped originals stay available; the runtime default selects this verified copy.
    shutil.copyfile(csv_path, cache / "current.csv")
    shutil.copyfile(manifest, cache / "current.json")
    return csv_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--cache", type=Path, default=Path("state/epss"))
    parser.add_argument("--sha256", help="expected compressed source SHA-256, if already known")
    args = parser.parse_args()
    path = fetch(args.date, args.cache, args.sha256)
    print(path.with_suffix(".json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
