"""Real tiny EPSS sample; all downloader network access is injected and mocked."""

import gzip
import hashlib
import io
from pathlib import Path

import pytest

from flowguard.epss import load, parse
from security.download_epss import fetch

SAMPLE = Path(__file__).resolve().parents[1] / "security/epss-sample.csv"


def test_real_sample_and_cached_download_never_need_the_network(tmp_path):
    sample = load(SAMPLE)
    assert sample.date == "2026-10-28"
    assert sample.scores["CVE-2026-56851"] == 0.00466
    assert sample.scores["CVE-2026-56852"] == 0.00475
    compressed = gzip.compress(SAMPLE.read_bytes(), mtime=0)
    digest = hashlib.sha256(compressed).hexdigest()
    calls = []

    def mocked(url, timeout):
        calls.append(url)
        return io.BytesIO(compressed)

    path = fetch(sample.date, tmp_path, digest, mocked)
    assert len(calls) == 1 and load(path).scores == sample.scores
    fetch(sample.date, tmp_path, digest, mocked)
    assert len(calls) == 1
    assert load(tmp_path / "current.csv").scores == sample.scores
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="checksum"):
        fetch(sample.date, tmp_path, digest, mocked)
    assert len(calls) == 1


def test_wrong_date_checksum_and_probability_are_rejected(tmp_path):
    data = SAMPLE.read_bytes()
    with pytest.raises(ValueError, match="date"):
        parse(data, "2026-10-27")
    with pytest.raises(ValueError, match="invalid"):
        parse(data.replace(b"0.00466", b"1.00466"), "2026-10-28")
    with pytest.raises(ValueError, match="checksum"):
        fetch("2026-10-28", tmp_path, "0" * 64, lambda *a, **kw: io.BytesIO(gzip.compress(data)))
    assert not list(tmp_path.iterdir())
