"""CI failure templates (DSO2): realistic commit-sized changes that make app-ci fail, several
per root-cause class. The class and the template id are ground truth only: nothing a
template writes (file names, code, test names, messages, hosts, commit messages) names the
class or the experiment, so CI output reveals nothing beyond what a real failure would.

The classes are contracts.RootCause: code, dependency, environment, flaky_test,
infrastructure. Template ids (e.g. "dependency-03") allow the corpus to be split by template
(D6); the full corpus is Step 4b.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
APP = "app/podinfo"


@dataclass(frozen=True)
class Template:
    id: str
    root_cause: str
    failing_job: str  # the job expected to fail first
    commit_message: str
    apply: Callable[[Path], list[str]]  # edits a checkout, returns the changed paths
    expect: str = ""  # a fragment of the expected error, for local verification


def replace_once(root: Path, rel: str, old: str, new: str) -> str:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise ValueError(f"{rel}: expected exactly one occurrence of {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")
    return rel


def write_file(root: Path, rel: str, text: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return rel


def edit(rel: str, old: str, new: str) -> Callable[[Path], list[str]]:
    return lambda root: [replace_once(root, rel, old, new)]


def edits(*changes: tuple[str, str, str]) -> Callable[[Path], list[str]]:
    def apply(root: Path) -> list[str]:
        return sorted({replace_once(root, rel, old, new) for rel, old, new in changes})

    return apply


def add_file(rel: str, text: str) -> Callable[[Path], list[str]]:
    return lambda root: [write_file(root, rel, text)]


# ---------------------------------------------------------------- code

HTTP = f"{APP}/pkg/api/http"
CODE = [
    Template(
        "code-01",
        "code",
        "unit",
        "Fix version response",
        edit(f"{HTTP}/version.go", '"commit":  version.REVISION,', '"commit":  version.VERSION,'),
        "TestVersionHandler",
    ),
    Template(
        "code-02",
        "code",
        "unit",
        "Return 200 from echo",
        edit(
            f"{HTTP}/echo.go", "w.WriteHeader(http.StatusAccepted)", "w.WriteHeader(http.StatusOK)"
        ),
        "TestEchoHandler",
    ),
    Template(
        "code-03",
        "code",
        "unit",
        "Tidy info response",
        edit(
            f"{HTTP}/info.go",
            "Color:        s.config.UIColor,",
            "Color:        s.config.UIMessage,",
        ),
        "TestInfoHandler",
    ),
    Template(
        "code-04",
        "code",
        "unit",
        "Normalize echo payload",
        edits(
            (f"{APP}/pkg/api/grpc/echo.go", '"context"\n', '"context"\n\t"strings"\n'),
            (
                f"{APP}/pkg/api/grpc/echo.go",
                "return &echo.Message{Body: message.Body}, nil",
                "return &echo.Message{Body: strings.ToUpper(message.Body)}, nil",
            ),
        ),
        "TestGrpcEcho",
    ),
    Template(
        "code-05",
        "code",
        "build",
        "Simplify goroutine count",
        edit(
            f"{HTTP}/info.go",
            "strconv.FormatInt(int64(runtime.NumGoroutine()), 10)",
            "strconv.Itoa(runtime.NumGoroutine(), 10)",
        ),
        "too many arguments in call to strconv.Itoa",
    ),
    Template(
        "code-06",
        "code",
        "optional (lint)",
        "Log requested delay",
        edits(
            (f"{HTTP}/delay.go", 'import (\n\t"math/rand"\n', 'import (\n\t"fmt"\n\t"math/rand"\n'),
            (
                f"{HTTP}/delay.go",
                'delay, err := strconv.Atoi(vars["wait"])',
                's.logger.Debug(fmt.Sprintf("delay %d requested", vars["wait"]))\n'
                '\tdelay, err := strconv.Atoi(vars["wait"])',
            ),
        ),
        "wrong type string",
    ),
]

# ---------------------------------------------------------------- dependency

GOMOD, GOSUM = f"{APP}/go.mod", f"{APP}/go.sum"
DEPENDENCY = [
    Template(
        "dependency-01",
        "dependency",
        "build",
        "Bump websocket",
        edit(GOMOD, "github.com/gorilla/websocket v1.5.3", "github.com/gorilla/websocket v1.9.9"),
        "v1.9.9",
    ),
    Template(
        "dependency-02",
        "dependency",
        "build",
        "Update go.sum",
        edit(
            GOSUM,
            "github.com/gorilla/mux v1.8.1 h1:TuBL49tXwgrFYWhqrNgrUNEY92u81SPhu7sTdzQEiWY=",
            "github.com/gorilla/mux v1.8.1 h1:TuBL49tXwgrFYWhqrNgrUNEY92u81SPhu7sTdzQEiWX=",
        ),
        "checksum mismatch",
    ),
    Template(
        "dependency-03",
        "dependency",
        "build",
        "Use patched mux fork",
        edit(
            GOMOD,
            "// Fix CVE-2022-28948\n",
            "replace github.com/gorilla/mux => github.com/gorilla-labs/mux v1.8.2\n\n"
            "// Fix CVE-2022-28948\n",
        ),
        "gorilla-labs/mux",
    ),
    Template(
        "dependency-04",
        "dependency",
        "build",
        "Move to jwt v5",
        edit(GOMOD, "github.com/golang-jwt/jwt/v4 v4.5.2", "github.com/golang-jwt/jwt/v5 v5.2.1"),
        "github.com/golang-jwt/jwt/v4",
    ),
    Template(
        "dependency-05",
        "dependency",
        "build",
        "Bump viper",
        edit(GOMOD, "github.com/spf13/viper v1.19.0", "github.com/spf13/viper v2.0.0"),
        "v2.0.0",
    ),
    Template(
        "dependency-06",
        "dependency",
        "build",
        "Pin mux to tested release",
        edit(
            GOMOD,
            "// Fix CVE-2022-28948\n",
            "replace github.com/gorilla/mux => github.com/gorilla/handlers v1.5.2\n\n"
            "// Fix CVE-2022-28948\n",
        ),
        "declares its path as",
    ),
    Template(
        "dependency-07",
        "dependency",
        "build",
        "Add telemetry client",
        edit(
            GOMOD,
            "\tgithub.com/gorilla/websocket v1.5.3\n",
            "\tgithub.com/gorilla/websocket v1.5.3\n"
            "\tgithub.com/acme-platform/go-telemetry v0.4.1\n",
        ),
        "acme-platform/go-telemetry",
    ),
]

# ---------------------------------------------------------------- environment

DOCKERFILE = f"{APP}/Dockerfile"
GO_BUILD = (
    'RUN CGO_ENABLED=0 go build -ldflags "-s -w \\\n'
    '    -X github.com/stefanprodan/podinfo/pkg/version.REVISION=${REVISION}" \\\n'
    "    -a -o bin/podinfo"
)
ENVIRONMENT = [
    Template(
        "environment-01",
        "environment",
        "build",
        "Pin build image",
        edit(
            DOCKERFILE,
            "FROM golang:1.26.10-alpine AS builder",
            "FROM golang:1.22-alpine AS builder",
        ),
        "go.mod requires go >= 1.26",
    ),
    Template(
        "environment-02",
        "environment",
        "build",
        "Enable cgo resolver",
        edit(
            DOCKERFILE,
            GO_BUILD,
            GO_BUILD.replace("CGO_ENABLED=0 go build", "CGO_ENABLED=1 go build -race"),
        ),
        "gcc",
    ),
    Template(
        "environment-03",
        "environment",
        "build",
        "Build from vendored modules",
        edit(
            DOCKERFILE, "RUN go mod download\n", "ENV GOFLAGS=-mod=vendor\n\nRUN go mod download\n"
        ),
        "vendor",
    ),
    Template(
        "environment-04",
        "environment",
        "build",
        "Disable public module proxy",
        edit(DOCKERFILE, "RUN go mod download\n", "ENV GOPROXY=off\n\nRUN go mod download\n"),
        "GOPROXY=off",
    ),
    Template(
        "environment-05",
        "environment",
        "unit",
        "Pin toolchain",
        edit(GOMOD, "toolchain go1.26.10", "toolchain go1.99.1"),
        "1.99.1",
    ),
    Template(
        "environment-06",
        "environment",
        "build",
        "Update runtime base image",
        edit(DOCKERFILE, "FROM alpine:3.24", "FROM alpine:3.99"),
        "alpine:3.99",
    ),
]

# ---------------------------------------------------------------- flaky_test

RACE_TEST = """package {package}

import (
\t"math/rand/v2"
\t"testing"
\t"time"
)

func {name}(t *testing.T) {{
\tlimit := {limit} * time.Millisecond
\tstart := time.Now()
\tdone := make(chan struct{{}})
\tgo func() {{
\t\ttime.Sleep(time.Duration(rand.IntN({spread})) * time.Millisecond)
\t\tclose(done)
\t}}()
\tselect {{
\tcase <-done:
\tcase <-time.After(limit):
\t\tt.Fatalf({message}, time.Since(start).Round(time.Millisecond), limit)
\t}}
}}
"""


def race_test(rel: str, package: str, name: str, message: str, limit: int = 10, spread: int = 50):
    """A test that waits on a goroutine with a random duration: it fails in most runs."""
    text = RACE_TEST.format(package=package, name=name, limit=limit, spread=spread, message=message)
    return add_file(rel, text)


FLAKY = [
    Template(
        "flaky_test-01",
        "flaky_test",
        "unit",
        "Add cache expiry test",
        race_test(
            f"{HTTP}/cache_ttl_test.go",
            "http",
            "TestCacheEntryExpiresAfterTTL",
            '"entry still present after %v (ttl %v)"',
        ),
        "TestCacheEntryExpiresAfterTTL",
    ),
    Template(
        "flaky_test-02",
        "flaky_test",
        "unit",
        "Cover token refresh",
        race_test(
            f"{HTTP}/token_refresh_test.go",
            "http",
            "TestTokenRefreshBeforeExpiry",
            '"refresh did not complete: context deadline exceeded after %v (budget %v)"',
        ),
        "TestTokenRefreshBeforeExpiry",
    ),
    Template(
        "flaky_test-03",
        "flaky_test",
        "unit",
        "Test stream deadlines",
        race_test(
            f"{APP}/pkg/api/grpc/stream_deadline_test.go",
            "grpc",
            "TestServerStreamHonoursDeadline",
            '"stream closed after %v, deadline was %v"',
        ),
        "TestServerStreamHonoursDeadline",
    ),
    Template(
        "flaky_test-04",
        "flaky_test",
        "unit",
        "Test client keepalive",
        race_test(
            f"{APP}/pkg/api/grpc/keepalive_test.go",
            "grpc",
            "TestClientKeepaliveReconnect",
            '"expected reconnect within %[2]v, still waiting after %[1]v"',
        ),
        "TestClientKeepaliveReconnect",
    ),
    Template(
        "flaky_test-05",
        "flaky_test",
        "unit",
        "Test watcher events",
        race_test(
            f"{APP}/pkg/fscache/watch_test.go",
            "fscache",
            "TestWatcherReportsUpdate",
            '"watcher reported no update after %v (poll %v)"',
        ),
        "TestWatcherReportsUpdate",
    ),
    Template(
        "flaky_test-06",
        "flaky_test",
        "unit",
        "Test graceful shutdown",
        race_test(
            f"{APP}/pkg/signals/shutdown_test.go",
            "signals",
            "TestShutdownDrainsWithinGrace",
            '"shutdown took %v, grace period %v"',
        ),
        "TestShutdownDrainsWithinGrace",
    ),
]

# ---------------------------------------------------------------- infrastructure

IT = "ci/integration-test.sh"
RUN_LINE = 'docker run -d --name "$NAME"'
INFRASTRUCTURE = [
    Template(
        "infrastructure-01",
        "infrastructure",
        "optional (integration)",
        "Use registry mirror for fixtures",
        edit(
            IT, RUN_LINE, "docker pull mirror.registry.internal/library/busybox:1.36\n" + RUN_LINE
        ),
        "mirror.registry.internal",
    ),
    Template(
        "infrastructure-02",
        "infrastructure",
        "build",
        "Add tzdata from internal mirror",
        edit(
            DOCKERFILE,
            "    ca-certificates curl netcat-openbsd\n",
            "    ca-certificates curl netcat-openbsd\n\nRUN apk add --no-cache "
            "--repository https://alpine-mirror.internal/v3.24/main tzdata\n",
        ),
        "alpine-mirror.internal",
    ),
    Template(
        "infrastructure-03",
        "infrastructure",
        "build",
        "Route module downloads through internal proxy",
        edit(
            DOCKERFILE,
            "RUN go mod download\n",
            "ENV GOPROXY=https://goproxy.internal\n\nRUN go mod download\n",
        ),
        "goproxy.internal",
    ),
    Template(
        "infrastructure-04",
        "infrastructure",
        "optional (integration)",
        "Seed integration fixtures",
        edit(
            IT,
            RUN_LINE,
            "curl -fsS --max-time 20 http://10.255.255.1/fixtures/seed.json "
            "-o /tmp/seed.json\n" + RUN_LINE,
        ),
        "timed out",
    ),
    Template(
        "infrastructure-05",
        "infrastructure",
        "optional (integration)",
        "Constrain integration container memory",
        edit(IT, RUN_LINE, 'docker run -d --memory=6m --memory-swap=6m --name "$NAME"'),
        "FAIL",
    ),
    Template(
        "infrastructure-06",
        "infrastructure",
        "build",
        "Pull runtime base from mirror",
        edit(DOCKERFILE, "FROM alpine:3.24", "FROM mirror.registry.internal/library/alpine:3.24"),
        "mirror.registry.internal",
    ),
]

TEMPLATES: dict[str, Template] = {
    t.id: t for t in CODE + DEPENDENCY + ENVIRONMENT + FLAKY + INFRASTRUCTURE
}
BY_CLASS: dict[str, list[Template]] = {
    c: [t for t in TEMPLATES.values() if t.root_cause == c] for c in CLASSES
}


def pick(root_cause: str, rng: random.Random, template_id: str | None = None) -> Template:
    """The named template, or a random one of the class."""
    if root_cause not in BY_CLASS:
        raise ValueError(f"root cause must be one of {CLASSES}, got {root_cause!r}")
    if template_id:
        template = TEMPLATES.get(template_id)
        if template is None or template.root_cause != root_cause:
            raise ValueError(f"no template {template_id!r} for {root_cause}")
        return template
    return rng.choice(BY_CLASS[root_cause])
