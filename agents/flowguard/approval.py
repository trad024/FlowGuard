"""Human approval (D17): a simulated approver for batch evaluation, a local page for live runs.

SimulatedApprover approves a proposal when the policy it is given says the action is correct
and denies it otherwise, with a seeded error rate (default 0, 10% in a sensitivity run) and a
delay. Every review is recorded in `reviews` (D58): the channel, who answered and how long the
proposal waited; the orchestrator writes them to the audit log's approvals row. The policy is
supplied by the evaluation harness; agent code never sees what a scenario injected.

ApprovalPage serves the pending proposals on 127.0.0.1 only, most risky first, with approve and
deny buttons and a required name field: the page has no login, so the person answering names
themselves, and that name is the approvals row's approver (D58). A proposal nobody answers
within the timeout (10 minutes) is a timeout, which counts as a denial. Every field shown is
HTML-escaped (a rationale can carry text an attacker wrote into a log), and a form must carry the
page's random token.
"""

from __future__ import annotations

import html
import random
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from flowguard.contracts import ActionProposal, GateDecision

TIMEOUT_S = 600


class SimulatedApprover:
    def __init__(
        self,
        policy: Callable[[ActionProposal], bool],
        error_rate: float = 0.0,
        delay_s: float = 0.0,
        seed: int = 0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.policy = policy
        self.error_rate = error_rate
        self.delay_s = delay_s
        self.rng = random.Random(seed)
        self.sleep = sleep
        self.reviews: dict[str, dict] = {}

    def review(self, proposal: ActionProposal, decision: GateDecision) -> GateDecision:
        self.reviews[proposal.id] = {"channel": "simulated", "wait_seconds": self.delay_s}
        if self.delay_s:
            self.sleep(self.delay_s)
        correct = bool(self.policy(proposal))
        if self.rng.random() < self.error_rate:
            correct = not correct
        verdict = "approved" if correct else "denied"
        return decision.model_copy(update={"human_verdict": verdict, "approved_by": "simulated"})


class ApprovalPage:
    def __init__(self, port: int = 0, timeout_s: float = TIMEOUT_S):
        self.timeout_s = timeout_s
        self.token = secrets.token_urlsafe(16)
        self._pending: dict[str, tuple[ActionProposal, GateDecision]] = {}
        self._verdicts: dict[str, tuple[str, str]] = {}  # proposal id -> (verdict, name)
        self.reviews: dict[str, dict] = {}
        self._cond = threading.Condition()
        page = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                body = page.render().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"
                )
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                form = parse_qs(
                    self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                )
                ok = page.answer(
                    form.get("proposal", [""])[0],
                    form.get("verdict", [""])[0],
                    form.get("token", [""])[0],
                    form.get("name", [""])[0],
                )
                self.send_response(303 if ok else 403)
                self.send_header("Location", "/")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, proposal_id: str, verdict: str, token: str, name: str = "") -> bool:
        name = " ".join(name.split())[:64]
        if not secrets.compare_digest(token, self.token) or verdict not in ("approved", "denied"):
            return False
        if not name:  # an answer must say who gave it (D58)
            return False
        with self._cond:
            if proposal_id not in self._pending:
                return False
            self._verdicts[proposal_id] = (verdict, name)
            self._cond.notify_all()
        return True

    def review(self, proposal: ActionProposal, decision: GateDecision) -> GateDecision:
        """Block until a human answers on the page, or the timeout (a denial) passes."""
        start = time.monotonic()
        with self._cond:
            self._pending[proposal.id] = (proposal, decision)
            answered = self._cond.wait_for(lambda: proposal.id in self._verdicts, self.timeout_s)
            verdict, name = self._verdicts.pop(proposal.id) if answered else ("timeout", None)
            self._pending.pop(proposal.id, None)
        self.reviews[proposal.id] = {
            "channel": "page",
            "wait_seconds": round(time.monotonic() - start, 3),
        }
        return decision.model_copy(update={"human_verdict": verdict, "approved_by": name})

    def render(self) -> str:
        with self._cond:
            items = sorted(self._pending.values(), key=lambda pd: -pd[1].risk_score)
        rows = []
        for p, d in items:
            e = html.escape
            buttons = "".join(
                f'<form method="post" style="display:inline">'
                f'<input type="hidden" name="proposal" value="{e(p.id)}">'
                f'<input type="hidden" name="token" value="{e(self.token)}">'
                f'<input name="name" placeholder="your name" required maxlength="64">'
                f'<button name="verdict" value="{v}">{label}</button></form>'
                for v, label in (("approved", "Approve"), ("denied", "Deny"))
            )
            rows.append(
                f"<tr><td>{e(str(d.tier))}</td><td>{d.risk_score:.2f}</td><td>{e(str(p.agent))}</td>"
                f"<td>{e(p.action)}</td><td>{e(p.target)}</td><td>{e(p.environment)}</td>"
                f"<td>{e(p.rationale)}</td><td>{e(d.reason)}</td><td>{buttons}</td></tr>"
            )
        table = "".join(rows) or '<tr><td colspan="9">Nothing waiting.</td></tr>'
        return (
            "<!doctype html><meta charset=utf-8><title>Approvals</title>"
            "<style>body{font:14px sans-serif;margin:16px}td,th{padding:4px 8px;"
            "border-bottom:1px solid #ccc;text-align:left}</style>"
            f"<h1>Waiting for approval</h1><p>No answer within {int(self.timeout_s // 60)} minutes"
            " counts as a denial.</p><table><tr><th>Tier</th><th>Risk</th><th>Agent</th>"
            "<th>Action</th><th>Target</th><th>Env</th><th>Rationale</th><th>Gate</th><th></th>"
            f"</tr>{table}</table>"
        )
