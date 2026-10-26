"""Every .gitleaksignore entry must still point at the demo secret it was written for: a line
shift in the vendored file would silently re-open the finding in CI."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def entries():
    for line in (ROOT / ".gitleaksignore").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            path, rule, number = line.rsplit(":", 2)
            yield path.removeprefix("/repo/"), rule, int(number)


def test_ignore_entries_point_at_the_demo_jwt_secret():
    found = list(entries())
    assert len(found) == 2
    for path, rule, number in found:
        text = (ROOT / path).read_text(encoding="utf-8").splitlines()[number - 1]
        assert rule == "generic-api-key"
        # podinfo's demo secret: a long quoted literal assigned as the JWT secret
        assert re.search(r'(jwt-secret|JWTSecret)"?,?\s*=?\s*"[A-Za-z0-9._-]{20,}"', text), (
            f"{path}:{number} no longer holds the demo secret: {text.strip()!r}"
        )
