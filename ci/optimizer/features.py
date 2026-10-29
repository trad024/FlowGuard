"""Commit-only features shared by training and the CI plan job; standard library only."""

from pathlib import PurePosixPath

JOBS = ("build", "unit", "scans", "lint", "integration", "sbom", "multiarch")
FEATURES = (
    "files",
    "go_files",
    "test_files",
    "docs_files",
    "ci_files",
    "container_files",
    "web_files",
    "dependency_files",
    "additions",
    "deletions",
    "size_known",
    "kind_code",
    "kind_test",
    "kind_docs",
    "kind_dependency",
    "kind_ci",
    "kind_other",
    *(f"job_{job}" for job in JOBS),
)
DEPENDENCIES = {
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile",
    "Gemfile.lock",
    "pom.xml",
    "build.gradle",
}


def dependencies_changed(paths):
    names = [PurePosixPath(p.replace("\\", "/")).name for p in paths]
    return any(
        name in DEPENDENCIES or name.startswith("requirements") and name.endswith(".txt")
        for name in names
    )


def parse_numstat(text):
    stats = {}
    for line in text.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            stats[parts[2]] = {"additions": int(parts[0]), "deletions": int(parts[1])}
    return stats


def extract(paths, job, stats=None):
    if job not in JOBS:
        raise ValueError("unknown job")
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("changed paths must be a list of nonempty strings")
    paths = sorted(set(p.replace("\\", "/").removeprefix("./") for p in paths))
    values = dict.fromkeys(FEATURES, 0.0)
    values["files"] = len(paths)
    values["go_files"] = sum(p.endswith(".go") for p in paths)
    values["test_files"] = sum(p.endswith(("_test.go", ".test.js", "_test.py")) for p in paths)
    values["docs_files"] = sum(p.endswith((".md", ".rst", ".txt")) for p in paths)
    values["ci_files"] = sum(p.startswith((".github/", "ci/")) for p in paths)
    values["container_files"] = sum("Dockerfile" in PurePosixPath(p).name for p in paths)
    values["web_files"] = sum(p.endswith((".js", ".ts", ".vue", ".html", ".css")) for p in paths)
    values["dependency_files"] = sum(dependencies_changed([p]) for p in paths)
    if stats is not None and all(
        p in stats
        and all(
            type(stats[p].get(k)) is int and stats[p][k] >= 0 for k in ("additions", "deletions")
        )
        for p in paths
    ):
        values["size_known"] = 1
        values["additions"] = sum(stats[p]["additions"] for p in paths)
        values["deletions"] = sum(stats[p]["deletions"] for p in paths)
    if values["dependency_files"]:
        kind = "dependency"
    elif values["ci_files"]:
        kind = "ci"
    elif paths and values["test_files"] == len(paths):
        kind = "test"
    elif paths and values["docs_files"] == len(paths):
        kind = "docs"
    elif values["go_files"] or values["web_files"]:
        kind = "code"
    else:
        kind = "other"
    values[f"kind_{kind}"] = 1
    values[f"job_{job}"] = 1
    return [float(values[name]) for name in FEATURES]
