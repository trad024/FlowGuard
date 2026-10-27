"""The test-suite guard (conftest.py): unit tests cannot reach kubectl, Docker, the infra
scripts or the network; loopback servers and local programs still work."""

import socket
import subprocess
import sys
import urllib.request

import pytest


@pytest.mark.parametrize(
    "cmd",
    [
        ["kubectl", "-n", "staging", "get", "rollout"],
        [r"C:\Program Files\Kubernetes\kubectl.exe", "version"],
        ["/usr/local/bin/docker", "ps"],
        ["k3d", "cluster", "list"],
        ["curl", "-s", "https://example.com"],
        ["powershell", "-File", "infra/scripts/deploy-version.ps1", "good"],
        ["bash", r"infra\scripts\setup.sh"],
        "kubectl get pods",  # a shell string
    ],
)
def test_cluster_docker_and_infra_scripts_are_refused(cmd):
    with pytest.raises(RuntimeError, match="mark the test integration"):
        subprocess.run(cmd, capture_output=True)
    with pytest.raises(RuntimeError, match="mark the test integration"):
        subprocess.Popen(cmd)


def test_local_programs_still_run():
    out = subprocess.run([sys.executable, "-c", "print(42)"], capture_output=True, text=True)
    assert out.stdout.strip() == "42"


def test_the_network_is_refused_but_loopback_works():
    with pytest.raises(RuntimeError, match="the network"):
        urllib.request.urlopen("https://api.github.com", timeout=2)
    with pytest.raises(RuntimeError, match="the network"):
        socket.create_connection(("10.255.255.1", 80), timeout=1)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=2)
    client.close()
    server.close()


@pytest.mark.integration
def test_integration_tests_are_skipped_unless_enabled():
    # Reached only with FLOWGUARD_INTEGRATION=1, and then without the guard
    assert subprocess.Popen.__name__ == "Popen"
