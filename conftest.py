"""Test-suite guard: no unit test may reach the cluster, Docker or the network.

Every test runs with subprocess guarded (kubectl, docker, k3d, curl, gh and the infra scripts
are refused), with sockets limited to loopback (the fake GitHub and Prometheus servers the
tests start themselves) and with no HTTP proxy, which would otherwise relay a loopback
connection to the network. A test that really needs the cluster or the network is marked
`@pytest.mark.integration`; it is skipped unless FLOWGUARD_INTEGRATION=1, and only then runs
without the guard.
"""

import os
import re
import socket
import subprocess
import urllib.request

import pytest

BLOCKED_PROGRAMS = {"kubectl", "docker", "k3d", "curl", "gh", "helm"}
BLOCKED_PATHS = ("infra/scripts", "infra\\scripts")
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY")


class ExternalCallError(RuntimeError):
    """A unit test tried to reach the cluster, Docker or the network."""


def program(args) -> str:
    """The executable's bare name: a list's first item as is (paths may contain spaces), a
    shell string's first word; either path separator, on any OS."""
    if isinstance(args, (list, tuple)):
        first = str(args[0]) if args else ""
    else:
        first = (str(args).split() or [""])[0]
    name = re.split(r"[\\/]", first)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(ext)
    return name


def refused(args) -> str | None:
    words = [str(a) for a in args] if isinstance(args, (list, tuple)) else [str(args)]
    if program(args) in BLOCKED_PROGRAMS:
        return program(args)
    if any(p in w for w in words for p in BLOCKED_PATHS):
        return "an infra script"
    return None


def guard_message(what: str) -> str:
    return f"a unit test called {what}: mock it, or mark the test integration"


@pytest.fixture(autouse=True)
def no_cluster_or_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        if os.environ.get("FLOWGUARD_INTEGRATION") != "1":
            pytest.skip("integration test: set FLOWGUARD_INTEGRATION=1 to run it")
        return

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):
        def __init__(self, args, *a, **kw):
            if what := refused(args):
                raise ExternalCallError(guard_message(what))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    # A proxy on loopback would carry any request out: no proxy from the environment, and none
    # from the Windows registry either (urllib falls back to it when the variables are unset)
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", dict)

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def host_of(address) -> str:
        return str(address[0] if isinstance(address, tuple) else address)

    def check(address):
        if isinstance(address, (str, bytes)):  # an AF_UNIX path, local by definition
            return
        host = host_of(address)
        if host in LOOPBACK or host.startswith("127."):
            return
        raise ExternalCallError(guard_message(f"the network ({host})"))

    def connect(self, address):
        check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        check(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host, *a, **kw):
        check((host if host is not None else "",))
        return real_getaddrinfo(host, *a, **kw)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


# ---------------------------------------------------------------- D67: calibrators in tests


def fitted_calibrator():
    """A fitted conformal calibrator (30 examples) that is never confident: its nonconformity
    scores are all -1 (label scores of 2.0), so any real score in [0, 1] has a p-value of 1/31,
    under the credibility floor, and a confidence of 0. It satisfies the D67 guard and keeps a
    test's gate outcomes as they were before D67 (confidence 0)."""
    from flowguard.calibration import ConformalCalibrator

    return ConformalCalibrator().fit([{"a": 2.0}] * 30, ["a"] * 30)


def fitted_all():
    """`fitted_calibrator` for every calibrated agent (D67), for an Orchestrator in a test."""
    from flowguard.calibrators import CALIBRATED

    return {agent: fitted_calibrator() for agent in CALIBRATED}


@pytest.fixture(autouse=True)
def no_real_calibrations(monkeypatch, tmp_path_factory):
    """No test reads this machine's state/ calibrations (CI has none): the loader looks in an
    empty folder unless a test gives it files, or uses the `fitted` fixtures below."""
    try:
        from flowguard import calibrators
    except ImportError:  # a test run without agents/ on the path
        return
    monkeypatch.setattr(calibrators, "ROOT", tmp_path_factory.mktemp("no-state"))


@pytest.fixture
def fitted_levels(monkeypatch):
    """Entry points get fitted calibrators from the loader; `calls` records the levels asked."""
    from flowguard import calibrators

    calls = []

    def for_level(level, root=None):
        calls.append(level)
        return {} if level == "L0" else {a: fitted_calibrator() for a in calibrators.CALIBRATED}

    monkeypatch.setattr(calibrators, "for_level", for_level)
    return calls
