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
            # The message is built but never logged: go vet's unusedresult check, which is not
            # in the subset go test runs, so build and unit pass and only lint fails
            (
                f"{HTTP}/delay.go",
                'delay, err := strconv.Atoi(vars["wait"])',
                'fmt.Sprintf("delay %s requested", vars["wait"])\n'
                '\tdelay, err := strconv.Atoi(vars["wait"])',
            ),
        ),
        "result of fmt.Sprintf call not used",
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
        "missing go.sum entry",
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
            "--repositories-file /dev/null "
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

# ---------------------------------------------------------------- variants (D34)
# The same defect in other files, functions, modules, versions or hosts. A variant's id is
# "<template id>.v<n>"; the template itself is variant 1. The corpus is split by template, so
# no variant of a test template is ever seen in training.

GRPC = f"{APP}/pkg/api/grpc"
SUM_LINES = {
    "github.com/gorilla/websocket v1.5.3": "saDtZ6Pbx/0u+bgYQ3q96pZgCzfhKXGPqt7kZ72aNNg=",
    "github.com/spf13/cobra v1.8.1": "e5/vxKd/rZsfSJMUX1agtjeTDf+qv1/JdBF8gg5k9ZM=",
    "go.uber.org/zap v1.27.0": "aJMhYGrd5QSmlpLMr2MftRKl7t8J8PTZPA732ud/XR8=",
}
HEALTHZ = (
    'if atomic.LoadInt32(&healthy) == 1 {\n\t\ts.JSONResponse(w, r, map[string]string{"status": '
    '"OK"})\n\t\treturn\n\t}\n\tw.WriteHeader(http.StatusServiceUnavailable)'
)
READYZ = HEALTHZ.replace("&healthy", "&ready")


def flipped(digest: str) -> str:
    """The same checksum with one character changed."""
    last = digest[-2]
    return digest[:-2] + ("A" if last != "A" else "B") + digest[-1]


def unused_sprintf(rel: str, anchor: str, arg: str) -> Callable[[Path], list[str]]:
    """A message built with fmt.Sprintf and never used (go vet unusedresult)."""
    return edits(
        (rel, 'import (\n\t"', 'import (\n\t"fmt"\n\t"'),
        (rel, anchor, f'fmt.Sprintf("{arg} %s requested", vars["{arg}"])\n\t' + anchor),
    )


def gomod_line(old: str, new: str) -> Callable[[Path], list[str]]:
    return edit(GOMOD, f"\t{old}\n", f"\t{new}\n")


def replace_directive(module: str, target: str) -> Callable[[Path], list[str]]:
    return edit(
        GOMOD, "// Fix CVE-2022-28948\n", f"replace {module} => {target}\n\n// Fix CVE-2022-28948\n"
    )


def _flaky(rel: str, package: str, name: str, message: str, limit: int = 10, spread: int = 50):
    return race_test(rel, package, name, message, limit, spread), name


_V = {
    "code-01": [
        (
            "Report build commit in version",
            edit(
                f"{GRPC}/version.go",
                "Version: version.VERSION, Commit:",
                "Version: version.REVISION, Commit:",
            ),
            "TestGrpcVersion",
        ),
    ],
    "code-02": [
        (
            "Report unhealthy as server error",
            edit(
                f"{HTTP}/health.go",
                HEALTHZ,
                HEALTHZ.replace("StatusServiceUnavailable", "StatusInternalServerError"),
            ),
            "TestHealthzHandler",
        ),
        (
            "Signal not ready as throttled",
            edit(
                f"{HTTP}/health.go",
                READYZ,
                READYZ.replace("StatusServiceUnavailable", "StatusTooManyRequests"),
            ),
            "TestReadyzHandler",
        ),
    ],
    "code-03": [
        (
            "Tidy gRPC info",
            edit(
                f"{GRPC}/info.go",
                "Color:        s.config.UIColor,",
                "Color:        s.config.UIMessage,",
            ),
            "TestGrpcInfo",
        ),
    ],
    "code-04": [
        (
            "Trim echo suffix",
            edits(
                (f"{GRPC}/echo.go", '"context"\n', '"context"\n\t"strings"\n'),
                (
                    f"{GRPC}/echo.go",
                    "return &echo.Message{Body: message.Body}, nil",
                    'return &echo.Message{Body: strings.TrimSuffix(message.Body, "-test")}, nil',
                ),
            ),
            "TestGrpcEcho",
        ),
    ],
    "code-05": [
        (
            "Simplify CPU count",
            edit(
                f"{HTTP}/info.go",
                "strconv.FormatInt(int64(runtime.NumCPU()), 10)",
                "strconv.Itoa(runtime.NumCPU(), 10)",
            ),
            "too many arguments in call to strconv.Itoa",
        ),
        (
            "Simplify gRPC goroutine count",
            edit(
                f"{GRPC}/info.go",
                "strconv.FormatInt(int64(runtime.NumGoroutine()), 10)",
                "strconv.Itoa(runtime.NumGoroutine(), 10)",
            ),
            "too many arguments in call to strconv.Itoa",
        ),
        (
            "Simplify gRPC CPU count",
            edit(
                f"{GRPC}/info.go",
                "strconv.FormatInt(int64(runtime.NumCPU()), 10)",
                "strconv.Itoa(runtime.NumCPU(), 10)",
            ),
            "too many arguments in call to strconv.Itoa",
        ),
    ],
    "code-06": [
        (
            "Log requested chunk delay",
            unused_sprintf(
                f"{HTTP}/chunked.go", 'delay, err := strconv.Atoi(vars["wait"])', "wait"
            ),
            "result of fmt.Sprintf call not used",
        ),
        # Not status.go: its route parameter is vars["code"], a class word on an added line
    ],
    "dependency-01": [
        (
            "Bump mux",
            gomod_line("github.com/gorilla/mux v1.8.1", "github.com/gorilla/mux v1.8.9"),
            "v1.8.9",
        ),
        ("Bump zap", gomod_line("go.uber.org/zap v1.27.0", "go.uber.org/zap v1.27.9"), "v1.27.9"),
        (
            "Bump redigo",
            gomod_line("github.com/gomodule/redigo v1.9.2", "github.com/gomodule/redigo v1.9.9"),
            "v1.9.9",
        ),
    ],
    "dependency-02": [
        (
            "Update go.sum",
            edit(GOSUM, f"{m} h1:{d}", f"{m} h1:{flipped(d)}"),
            "checksum mismatch",
        )
        for m, d in SUM_LINES.items()
    ],
    "dependency-03": [
        (
            "Use patched websocket fork",
            replace_directive(
                "github.com/gorilla/websocket", "github.com/gorilla-labs/websocket v1.5.4"
            ),
            "gorilla-labs/websocket",
        ),
        (
            "Use patched zap fork",
            replace_directive("go.uber.org/zap", "github.com/uber-go-labs/zap v1.27.1"),
            "uber-go-labs/zap",
        ),
    ],
    "dependency-04": [
        (
            "Move to redigo v2",
            gomod_line("github.com/gomodule/redigo v1.9.2", "github.com/gomodule/redigo/v2 v2.0.0"),
            "github.com/gomodule/redigo",
        ),
        (
            "Move to color v2",
            gomod_line("github.com/fatih/color v1.17.0", "github.com/fatih/color/v2 v2.0.0"),
            "github.com/fatih/color",
        ),
    ],
    "dependency-05": [
        ("Bump zap", gomod_line("go.uber.org/zap v1.27.0", "go.uber.org/zap v2.0.0"), "v2.0.0"),
        (
            "Bump mux",
            gomod_line("github.com/gorilla/mux v1.8.1", "github.com/gorilla/mux v2.0.0"),
            "v2.0.0",
        ),
        (
            "Bump cobra",
            gomod_line("github.com/spf13/cobra v1.8.1", "github.com/spf13/cobra v2.0.0"),
            "v2.0.0",
        ),
    ],
    "dependency-06": [
        (
            "Pin websocket to tested release",
            replace_directive("github.com/gorilla/websocket", "github.com/gorilla/handlers v1.5.2"),
            "missing go.sum entry",
        ),
        (
            "Pin redigo to tested release",
            replace_directive("github.com/gomodule/redigo", "github.com/gorilla/handlers v1.5.2"),
            "missing go.sum entry",
        ),
    ],
    "dependency-07": [
        (
            "Add feature flag client",
            gomod_line(
                "github.com/gorilla/websocket v1.5.3",
                "github.com/gorilla/websocket v1.5.3\n"
                "\tgithub.com/acme-platform/feature-flags v1.2.0",
            ),
            "acme-platform/feature-flags",
        ),
        (
            "Add metrics client",
            gomod_line(
                "github.com/gorilla/websocket v1.5.3",
                "github.com/gorilla/websocket v1.5.3\n\tgithub.com/internal-tools/go-metrics"
                " v0.3.0",
            ),
            "internal-tools/go-metrics",
        ),
    ],
    "environment-01": [
        (
            f"Pin build image to {v}",
            edit(
                DOCKERFILE,
                "FROM golang:1.26.10-alpine AS builder",
                f"FROM golang:{v}-alpine AS builder",
            ),
            "go.mod requires go >= 1.26",
        )
        for v in ("1.23", "1.24", "1.25")
    ],
    "environment-02": [
        (
            "Link statically",
            edit(
                DOCKERFILE,
                GO_BUILD,
                GO_BUILD.replace(
                    'CGO_ENABLED=0 go build -ldflags "-s -w',
                    'CGO_ENABLED=1 go build -ldflags "-linkmode external -extldflags -static -s -w',
                ),
            ),
            "gcc",
        ),
    ],
    "environment-03": [
        (
            "Build from vendored modules",
            # podinfo's build only: the Dockerfile also builds podcli
            edit(
                DOCKERFILE,
                GO_BUILD,
                GO_BUILD.replace("go build -ldflags", "go build -mod=vendor -ldflags"),
            ),
            "vendor",
        ),
    ],
    "environment-04": [
        (
            "Download modules offline",
            edit(DOCKERFILE, "RUN go mod download\n", "RUN GOPROXY=off go mod download\n"),
            "GOPROXY=off",
        ),
    ],
    "environment-05": [
        (
            f"Pin toolchain {v}",
            edit(GOMOD, "toolchain go1.26.10", f"toolchain {v}"),
            v.removeprefix("go"),
        )
        for v in ("go1.98.0", "go1.26.99")
    ],
    "environment-06": [
        (
            f"Update runtime base image to {v}",
            edit(DOCKERFILE, "FROM alpine:3.24", f"FROM alpine:{v}"),
            f"alpine:{v}",
        )
        for v in ("3.98", "4.1")
    ],
    "flaky_test-01": [
        _flaky(
            f"{HTTP}/session_idle_test.go",
            "http",
            "TestSessionExpiresAfterIdle",
            '"session still valid after %v (idle timeout %v)"',
        ),
        _flaky(
            f"{HTTP}/rate_window_test.go",
            "http",
            "TestRateWindowResetsAfterInterval",
            '"window not reset after %v (interval %v)"',
            15,
            60,
        ),
    ],
    "flaky_test-02": [
        _flaky(
            f"{HTTP}/key_rotation_test.go",
            "http",
            "TestSigningKeyRotation",
            '"rotation not finished after %v (budget %v)"',
        ),
        _flaky(
            f"{HTTP}/cookie_refresh_test.go",
            "http",
            "TestCookieRefreshedBeforeExpiry",
            '"cookie not refreshed: %v elapsed, expiry margin %v"',
            12,
            55,
        ),
    ],
    "flaky_test-03": [
        _flaky(
            f"{GRPC}/unary_deadline_test.go",
            "grpc",
            "TestUnaryCallHonoursDeadline",
            '"call returned after %v, deadline %v"',
        ),
        _flaky(
            f"{GRPC}/server_drain_test.go",
            "grpc",
            "TestServerDrainsOpenStreams",
            '"streams still open after %v (drain %v)"',
            15,
            60,
        ),
    ],
    "flaky_test-04": [
        _flaky(
            f"{GRPC}/health_watch_test.go",
            "grpc",
            "TestHealthWatchReconnects",
            '"no health update within %[2]v, waited %[1]v"',
        ),
        _flaky(
            f"{GRPC}/backoff_test.go",
            "grpc",
            "TestClientBackoffResets",
            '"backoff not reset after %v (expected within %v)"',
            12,
            55,
        ),
    ],
    "flaky_test-05": [
        _flaky(
            f"{APP}/pkg/fscache/reload_test.go",
            "fscache",
            "TestReloadPicksUpNewFile",
            '"new file not loaded after %v (poll %v)"',
        ),
        _flaky(
            f"{APP}/pkg/fscache/debounce_test.go",
            "fscache",
            "TestDebounceCoalescesWrites",
            '"writes not coalesced after %v (window %v)"',
            15,
            60,
        ),
    ],
    "flaky_test-06": [
        _flaky(
            f"{APP}/pkg/signals/sigterm_test.go",
            "signals",
            "TestSigtermStopsWithinGrace",
            '"stop took %v, grace %v"',
        ),
        _flaky(
            f"{APP}/pkg/signals/hangup_test.go",
            "signals",
            "TestHangupReloadsConfig",
            '"config not reloaded after %v (expected within %v)"',
            12,
            55,
        ),
    ],
    "infrastructure-01": [
        (
            f"Use {h} for fixtures",
            edit(IT, RUN_LINE, f"docker pull {h}/library/busybox:1.36\n" + RUN_LINE),
            h,
        )
        for h in ("registry-cache.corp.internal", "docker-mirror.internal:5000")
    ],
    "infrastructure-02": [
        (
            "Add tzdata from package cache",
            edit(
                DOCKERFILE,
                "    ca-certificates curl netcat-openbsd\n",
                "    ca-certificates curl netcat-openbsd\n\nRUN apk add --no-cache "
                "--repositories-file /dev/null "
                "--repository https://apk.cache.internal/v3.24/main tzdata\n",
            ),
            "apk.cache.internal",
        ),
        (
            "Add jq from internal mirror",
            edit(
                DOCKERFILE,
                "    ca-certificates curl netcat-openbsd\n",
                "    ca-certificates curl netcat-openbsd\n\nRUN apk add --no-cache "
                "--repositories-file /dev/null "
                "--repository https://alpine-mirror.internal/v3.24/community jq\n",
            ),
            "alpine-mirror.internal",
        ),
    ],
    "infrastructure-03": [
        (
            f"Route module downloads through {h}",
            edit(
                DOCKERFILE,
                "RUN go mod download\n",
                f"ENV GOPROXY=https://{h}\n\nRUN go mod download\n",
            ),
            h,
        )
        for h in ("athens.internal", "proxy.golang.internal")
    ],
    "infrastructure-04": [
        (
            "Seed integration fixtures",
            edit(
                IT,
                RUN_LINE,
                f"curl -fsS --max-time 20 http://{ip}/fixtures/seed.json -o /tmp/seed.json\n"
                + RUN_LINE,
            ),
            "timed out",
        )
        for ip in ("10.255.255.2", "192.0.2.10")
    ],
    "infrastructure-05": [
        (
            f"Constrain integration container memory to {m}",
            edit(IT, RUN_LINE, f'docker run -d --memory={m} --memory-swap={m} --name "$NAME"'),
            "FAIL",
        )
        for m in ("5m", "7m")
    ],
    "infrastructure-06": [
        (
            f"Pull runtime base from {h}",
            edit(DOCKERFILE, "FROM alpine:3.24", f"FROM {h}/library/alpine:3.24"),
            h,
        )
        for h in ("registry-cache.corp.internal", "docker-mirror.internal:5000")
    ],
}


def _variants() -> dict[str, list[Template]]:
    out = {}
    for tid, base in TEMPLATES.items():
        extra = []
        for n, spec in enumerate(_V.get(tid, []), start=2):
            if base.root_cause == "flaky_test":
                apply, name = spec
                message, expect = f"Cover {name.removeprefix('Test')}", name
            else:
                message, apply, expect = spec
            extra.append(
                Template(f"{tid}.v{n}", base.root_cause, base.failing_job, message, apply, expect)
            )
        out[tid] = [base, *extra]
    return out


VARIANTS: dict[str, list[Template]] = _variants()
ALL_VARIANTS: dict[str, Template] = {v.id: v for vs in VARIANTS.values() for v in vs}


def template_of(variant_id: str) -> str:
    """The template a variant belongs to (the unit of the train/validation/test split)."""
    return variant_id.split(".v")[0]


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
