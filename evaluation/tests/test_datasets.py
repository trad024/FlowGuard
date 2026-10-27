"""External datasets: checksum verification, safe extraction, the LogChunks parser, the
seeded sample. Synthetic data only; no download."""

import csv
import hashlib
import zipfile

import pytest

import datasets


def make_logchunks(root, repos):
    """A miniature LogChunks tree: repos = {"Lang/owner@name": number of failed logs}."""
    for i, (repo, n) in enumerate(sorted(repos.items())):
        lang = repo.split("/")[0]
        examples = []
        for j in range(n):
            rel = f"{repo}/failed/{1000 * i + j}.log"
            log = root / "logs" / rel
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(f"build {rel}\nERROR something\n", encoding="utf-8")
            examples.append(
                f"<Example><Log>{rel}</Log><Category>{j % 3}</Category>"
                "<Chunk>ERROR something</Chunk></Example>"
            )
        # A duplicate entry and one pointing at a missing log: both ignored
        examples.append(f"<Example><Log>{repo}/failed/{1000 * i}.log</Log></Example>")
        examples.append(f"<Example><Log>{repo}/failed/missing.log</Log></Example>")
        xml = root / "build-failure-reason" / lang / f"{repo.split('/')[1]}.xml"
        xml.parent.mkdir(parents=True, exist_ok=True)
        xml.write_text("<Examples>" + "".join(examples) + "</Examples>", encoding="utf-8")


def test_verify_checks_size_and_md5(tmp_path):
    f = tmp_path / "a.zip"
    f.write_bytes(b"hello")
    good = datasets.Archive("a.zip", "x", 5, hashlib.md5(b"hello").hexdigest(), "")
    datasets.verify(f, good)
    with pytest.raises(ValueError, match="size"):
        datasets.verify(f, datasets.Archive("a.zip", "x", 6, good.md5, ""))
    with pytest.raises(ValueError, match="md5"):
        datasets.verify(f, datasets.Archive("a.zip", "x", 5, "0" * 32, ""))


def test_pinned_archives_match_the_published_metadata():
    assert datasets.LOGCHUNKS.md5 == "aafa45079bdae44e340f4474ca5c4340"
    assert datasets.LOGCHUNKS.size == 24108826
    assert datasets.LOGSIEVE.md5 == "211faad8b6eee91d077ac268e6750074"


def test_safe_extract_skips_mac_metadata_and_refuses_zip_slip(tmp_path):
    good = tmp_path / "good.zip"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("LogChunks/readme.md", "x")
        z.writestr("__MACOSX/LogChunks/._readme.md", "junk")
        z.writestr("LogChunks/.DS_Store", "junk")
    assert datasets.safe_extract(good, tmp_path / "out") == 1
    assert (tmp_path / "out" / "LogChunks" / "readme.md").read_text() == "x"
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe"):
        datasets.safe_extract(evil, tmp_path / "out2")
    assert not (tmp_path / "escape.txt").exists()


def test_labelled_logs_are_failed_existing_and_unique(tmp_path):
    make_logchunks(tmp_path, {"C++/a@x": 3, "Python/b@y": 2})
    logs = datasets.labelled_logs(tmp_path)
    assert len(logs) == 5
    assert {log.repository for log in logs} == {"a@x", "b@y"}
    assert all(log.log_path.split("/")[2] == "failed" for log in logs)


def test_sample_is_reproducible_and_spread_over_repositories(tmp_path):
    make_logchunks(tmp_path, {f"Go/r{i}@p": 6 for i in range(10)})
    logs = datasets.labelled_logs(tmp_path)
    first = datasets.draw_sample(logs, size=25)
    assert first == datasets.draw_sample(logs, size=25)  # same seed, same sample
    assert first != datasets.draw_sample(logs, size=25, seed=1)
    per_repo = {}
    for log in first:
        per_repo[log.repository] = per_repo.get(log.repository, 0) + 1
    assert len(first) == 25 and len(set(first)) == 25
    assert max(per_repo.values()) - min(per_repo.values()) <= 1  # round-robin


def test_sample_never_exceeds_the_available_logs(tmp_path):
    make_logchunks(tmp_path, {"Go/a@b": 2})
    assert len(datasets.draw_sample(datasets.labelled_logs(tmp_path), size=150)) == 2


def test_sample_csv(tmp_path):
    make_logchunks(tmp_path, {"Go/a@b": 2, "Rust/c@d": 2})
    picked = datasets.draw_sample(datasets.labelled_logs(tmp_path), size=3)
    sample = tmp_path / "sample.csv"
    datasets.write_sample(picked, tmp_path, sample)
    rows = list(csv.DictReader(sample.open(encoding="utf-8")))
    assert [r["sample_id"] for r in rows] == ["LC001", "LC002", "LC003"]
    log = tmp_path / "logs" / rows[0]["log_path"]
    assert rows[0]["log_sha256"] == hashlib.sha256(log.read_bytes()).hexdigest()


def test_committed_sample_has_150_rows_from_many_repositories():
    rows = list(csv.DictReader(datasets.SAMPLE_CSV.open(encoding="utf-8")))
    assert len(rows) == 150
    assert len({r["repository"] for r in rows}) >= 75
    assert len({r["sample_id"] for r in rows}) == 150


def test_raw_downloads_are_gitignored():
    assert "evaluation/datasets/raw/" in (datasets.ROOT / ".gitignore").read_text().splitlines()
