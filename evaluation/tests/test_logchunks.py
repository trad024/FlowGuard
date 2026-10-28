"""LogChunks targets (D32, D43): located, partly located and not located chunks; the H3
sample and the visibility check after the corpus reduction (D47, D48)."""

import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logchunks  # noqa: E402
from flowguard import logreduce  # noqa: E402

LOG = "\n".join(
    [
        "$ make test",
        "\x1b[31m+t/run --flag=1\x1b[0m",
        "",
        "progress 10%\rprogress 100%",
        "flag provided but not defined:   -flag",
        "The command exited with 1.",
    ]
)


def test_a_whole_chunk_is_located_on_consecutive_lines():
    chunk = "t/run --flag=1\nprogress 100%\nflag provided but not defined: -flag"
    assert logchunks.locate(LOG, chunk) == ("located", [2, 4, 5])


def test_carriage_return_overwrites_are_resolved():
    assert logchunks.locate(LOG, "progress 10%")[0] == "not_located"


def test_partly_and_not_located():
    assert logchunks.locate(LOG, "t/run --flag=1\nsomething never printed") == ("partly", [2])
    assert logchunks.locate(LOG, "nothing like this") == ("not_located", [])
    assert logchunks.locate(LOG, "  \n") == ("not_located", [])


def test_reduced_line_numbers_match_the_targets():
    status, target = logchunks.locate(LOG, "flag provided but not defined: -flag")
    kept = dict(logreduce.reduce(LOG))
    assert status == "located" and kept[target[0]] == "flag provided but not defined:   -flag"
    assert kept[4] == "progress 100%"  # the terminal view of a carriage-return line


def write(raw, rel, text):
    path = raw / "logs" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_sample_is_seeded_and_replaces_logs_whose_chunk_the_reduction_hides(tmp_path):
    raw, rows = tmp_path / "raw", []
    noise = "\n".join(f"a{i} b{i} c{i} d{i}" for i in range(400))  # no two alike
    for i in range(6):
        rel = f"Java/a@b/failed/{i}.log"
        if i % 2:  # target early in a long log: the reduction keeps only the end
            text, target = f"BUILD FAILED here\n{noise}\n", "1"
        else:
            text, target = f"{noise}\nBUILD FAILED here\n", "401"
        rows.append(
            {
                "log_path": rel,
                "repository": "a@b",
                "status": "located",
                "target_lines": target,
                "sha256": write(raw, rel, text),
            }
        )
    rows.append({**rows[0], "log_path": "Java/a@b/failed/x.log", "status": "partly"})
    located = tmp_path / "located.csv"
    with located.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "sample.csv"
    summary = logchunks.sample(raw, located, out, size=2)
    picked = list(csv.DictReader(out.open(encoding="utf-8")))
    order = logchunks.order([r["log_path"] for r in rows[:6]])
    assert [r["log_path"] for r in picked] == order[: len(picked)]
    assert summary["used"] == 2 and summary["located"] == 6
    assert summary["not_visible"] == sum(r["status"] == "not_visible" for r in picked)
    assert all(
        (r["status"] == "used") == r["log_path"].endswith(("0.log", "2.log", "4.log"))
        for r in picked
    )
