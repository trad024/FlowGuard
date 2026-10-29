"""Fix actions (D57): what pin_dependency and open_fix_pr carry, built from a diagnosis.

pin_dependency is a generic "set module X to version Y" in app/podinfo/go.mod. It is proposed
only for a dependency diagnosis whose evidence names the failing module and version, and only
where a pin fixes the failure: Y is the version the build used before, different from the one
that fails. `set_version` is the go.mod edit. go.sum is not recomputed: `go mod tidy` would
add X@Y's two hashes (the module's and its go.mod's) and drop the lines nothing needs any
more, which takes the go toolchain and the module graph. So a pin is complete only when go.sum
already holds both hashes for X@Y; otherwise the go.mod edit goes into a draft PR for a person
to finish.

open_fix_pr opens a draft pull request when a patch exists (the only checkable patch is such a
pin), otherwise an issue, carrying the diagnosis: root cause, quoted evidence lines and the
proposed fix. An executed open_fix_pr counts as assisted, never as resolved without a human
(H5).

Text functions only: nothing here reads the repository or writes anything. The executor is the
only component that writes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

GO_MOD = "app/podinfo/go.mod"
MAX_QUOTES = 5
MAX_QUOTE_CHARS = 300
MAX_FIX_CHARS = 1_000

_MODULE = r"[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}(?:/[\w.\-~+]+)+"  # a host with a dot, then a path
_VERSION = r"v\d+\.\d+\.\d+(?:-[\w.\-]+)?(?:\+[\w.\-]+)?"
MODULE = re.compile(_MODULE)
VERSION = re.compile(_VERSION)
# How the go command names a module version it cannot use
NAMED = (
    # go: github.com/x/y@v1.9.9: invalid version: unknown revision v1.9.9
    re.compile(rf"(?P<module>{_MODULE})@(?P<version>{_VERSION})(?![\w.\-+])"),
    # go.mod:15:2: require github.com/x/y: version "v2.0.0" invalid: should be v0 or v1, not v2
    re.compile(rf"require (?P<module>{_MODULE}): version \"(?P<version>{_VERSION})\" invalid"),
)


def named(lines: Iterable[str]) -> list[tuple[str, str]]:
    """(module, version) pairs the lines name, in order, without repeats."""
    found = []
    for line in lines:
        for pattern in NAMED:
            for m in pattern.finditer(line):
                pair = (m["module"], m["version"])
                if pair not in found:
                    found.append(pair)
    return found


def set_version(go_mod: str, module: str, version: str) -> str:
    """go.mod with `module` required at `version`. Refuses a module that is not required, is
    required twice, is already at that version, or is replaced (a pin would not take effect)."""
    if not MODULE.fullmatch(module) or not VERSION.fullmatch(version):
        raise ValueError(f"not a module path and version: {module} {version}")
    lines = go_mod.split("\n")
    hits, block = [], None
    for i, line in enumerate(lines):
        words = line.split("//", 1)[0].split()
        if block:
            if words == [")"]:
                block = None
                continue
            directive = block
        elif words[:1] in (["require"], ["replace"]):
            if words[1:] == ["("]:
                block = words[0]
                continue
            directive, words = words[0], words[1:]
        else:
            continue
        if not words or words[0] != module:
            continue
        if directive == "replace":
            raise ValueError(f"{module} is replaced in go.mod; a pin would not take effect")
        if len(words) == 2:
            hits.append(i)
    if len(hits) != 1:
        raise ValueError(f"{module} is required {len(hits)} times in go.mod, expected once")
    i = hits[0]
    m = re.match(rf"(\s*(?:require\s+)?{re.escape(module)}\s+)(\S+)", lines[i])
    if m[2] == version:
        raise ValueError(f"{module} is already at {version}")
    lines[i] = m[1] + version + lines[i][m.end() :]
    return "\n".join(lines)


def sum_has(go_sum: str, module: str, version: str) -> bool:
    """go.sum holds both hashes go needs for module@version: the module's and its go.mod's."""
    entries = {tuple(line.split()[:2]) for line in go_sum.splitlines()}
    return (module, version) in entries and (module, f"{version}/go.mod") in entries


def pin(evidence: Iterable[str], used: Mapping[str, str]) -> dict | None:
    """The first pin the evidence supports: a module it names at a failing version, set back
    to the version the build used before (`used`: module -> version). None if no pin fixes
    the failure."""
    for module, failing in named(evidence):
        good = used.get(module)
        if good and good != failing:
            return {"file": GO_MOD, "module": module, "version": good}
    return None


def _fenced(lines: list[str]) -> list[str]:
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", *lines, fence]


def request(root_cause: str, evidence: Iterable[str], fix: str, patch: dict | None = None):
    """open_fix_pr params: a draft PR when a patch exists, otherwise an issue, with the
    diagnosis as title and body. Evidence and fix sit in fenced blocks, so nothing an LLM
    wrote renders as markdown or mentions anyone."""
    quotes = [str(q)[:MAX_QUOTE_CHARS] for q in evidence if str(q).strip()][:MAX_QUOTES]
    fix = " ".join(str(fix).split())[:MAX_FIX_CHARS]
    title = f"CI failure, root cause {root_cause}"
    body = [f"**Root cause:** {root_cause}", "", "**Evidence**, quoted from the failed job's log:"]
    body += _fenced(quotes) if quotes else ["(none quoted)"]
    body += ["", "**Proposed fix:**"]
    body += _fenced([fix]) if fix else ["(none proposed)"]
    if patch:
        title = f"Set {patch['module']} to {patch['version']} ({title})"
        body += [
            "",
            f"This draft sets `{patch['module']}` to `{patch['version']}` in `{patch['file']}`; "
            "go.sum still needs `go mod tidy`.",
        ]
    body += ["", "Opened for review; nothing is merged without a person."]
    params = {
        "kind": "draft_pr" if patch else "issue",
        "title": title,
        "body": "\n".join(body),
        "root_cause": root_cause,
    }
    if patch:
        params["patch"] = dict(patch)
    return params


def fix_action(
    action: str,
    root_cause: str,
    evidence: Iterable[str],
    fix: str,
    used: Mapping[str, str],
    go_sum: str,
) -> tuple[str, dict]:
    """The proposal's action and params under D57, from the action Diagnose chose.

    - pin_dependency stays a pin only for a dependency diagnosis whose evidence supports one
      and whose go.sum is ready; a supported pin without the go.sum hashes becomes a draft PR,
      and an unsupported pin becomes an issue.
    - open_fix_pr carries the diagnosis: a draft PR when a pin is supported, else an issue.
    - Any other action passes through with no params.
    """
    evidence = list(evidence)
    if action not in ("pin_dependency", "open_fix_pr"):
        return action, {}
    found = pin(evidence, used) if root_cause == "dependency" else None
    if action == "pin_dependency" and found and sum_has(go_sum, found["module"], found["version"]):
        return action, found
    return "open_fix_pr", request(root_cause, evidence, fix, found)
