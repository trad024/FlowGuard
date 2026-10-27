"""Test-suite guard: no unit test may reach the cluster, Docker or the network.

Every test runs with subprocess guarded (kubectl, docker, k3d, curl, gh and the infra scripts
are refused) and with sockets limited to loopback (the fake GitHub and Prometheus servers the
tests start themselves). A test that really needs the cluster or the network is marked
`@pytest.mark.integration`; it is skipped unless FLOWGUARD_INTEGRATION=1, and only then runs
without the guard.
"""

import os
import re
import socket
import subprocess

import pytest

BLOCKED_PROGRAMS = {"kubectl", "docker", "k3d", "curl", "gh", "helm"}
BLOCKED_PATHS = ("infra/scripts", "infra\\scripts")
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


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
