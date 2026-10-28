"""LogChunks targets (D32, D43): located, partly located and not located chunks."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import logchunks  # noqa: E402

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
