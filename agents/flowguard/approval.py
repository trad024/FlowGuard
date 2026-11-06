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
        """The page: one card per pending proposal, most risky first. Every field is escaped;
        no script runs (the CSP allows inline styles only). While nothing waits, the page
        reloads itself every 5 s; with a card open it does not, so a name being typed is kept."""
        with self._cond:
            items = sorted(self._pending.values(), key=lambda pd: -pd[1].risk_score)
        e = html.escape
        cards = []
        for p, d in items:
            tier = str(d.tier)
            risk = max(0.0, min(1.0, d.risk_score))
            cards.append(
                f'<article class="card t-{e(tier)}">'
                f'<header><span class="tier">{e(tier)} risk</span>'
                f'<span class="env">{e(p.environment)}</span>'
                f'<span class="agent">{e(str(p.agent))} agent</span></header>'
                f'<h2><code>{e(p.action)}</code> <span class="on">on</span> '
                f"<code>{e(p.target)}</code></h2>"
                f'<p class="why">{e(p.rationale)}</p>'
                f'<div class="meta"><div><small>Gate decision</small><b>{e(d.reason)}</b></div>'
                f"<div><small>Risk score</small><b>{risk:.2f}</b>"
                f'<i class="bar"><i style="width:{risk * 100:.0f}%"></i></i></div></div>'
                f'<form method="post">'
                f'<input type="hidden" name="proposal" value="{e(p.id)}">'
                f'<input type="hidden" name="token" value="{e(self.token)}">'
                f'<input name="name" placeholder="Your name (recorded in the audit log)" '
                f'required maxlength="64" autocomplete="name">'
                f'<button class="ok" name="verdict" value="approved">Approve</button>'
                f'<button class="no" name="verdict" value="denied">Deny</button></form>'
                f'<p class="id">Proposal {e(p.id)}</p></article>'
            )
        refresh = "" if cards else '<meta http-equiv="refresh" content="5">'
        body = "".join(cards) or (
            '<div class="empty"><b>Nothing waiting.</b>'
            "<p>Agents act alone only on low-risk actions, and on medium-risk ones in staging"
            " with enough confidence. Anything else appears here for a person to decide."
            "</p><p class=small>This page refreshes every 5 seconds.</p></div>"
        )
        n = len(cards)
        return (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"{refresh}<title>FlowGuard · Approvals</title><style>{PAGE_CSS}</style>"
            '<div class="top"><div class="brand"><span class="dot"></span>FlowGuard'
            '<span class="sub">Human approval</span></div>'
            f'<div class="count">{n} waiting</div></div><main>'
            "<h1>Decisions waiting for a person</h1>"
            f'<p class="lead">The risk gate sent these proposals to a human. No answer within '
            f"{int(self.timeout_s // 60)} minutes counts as a denial. The executor re-checks every"
            " verdict in the audit log before it acts.</p>"
            f"{body}</main>"
        )


PAGE_CSS = (
    ":root{--ink:#13203A;--muted:#5B6678;--line:#E3E6EC;--bg:#F5F6F8;--card:#FFFFFF;"
    "--blue:#2F64D6;--ok:#1F7A4D;--no:#B42318;--high:#B42318;--medium:#B76E00;--low:#1F7A4D}"
    "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    ".top{display:flex;justify-content:space-between;align-items:center;padding:14px 28px;"
    "background:var(--ink);color:#EEF2F7}.brand{font-weight:700;font-size:17px;display:flex;"
    "align-items:center;gap:10px}.brand .sub{font-weight:400;color:#9FB0C8;font-size:14px;"
    "padding-left:10px;border-left:1px solid #33425E}.dot{width:10px;height:10px;"
    "border-radius:50%;background:#E8833A;display:inline-block}.count{background:#22314F;"
    "padding:4px 12px;border-radius:999px;font-size:13px}"
    "main{max-width:900px;margin:0 auto;padding:28px 20px 60px}h1{font-size:24px;margin:0 0 6px}"
    ".lead{color:var(--muted);margin:0 0 22px}"
    ".card{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--low);"
    "border-radius:12px;padding:18px 20px;margin:0 0 16px;box-shadow:0 1px 2px #0000000d}"
    ".card.t-high{border-left-color:var(--high)}.card.t-medium{border-left-color:var(--medium)}"
    ".card header{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}"
    ".card header span{font-size:12px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.5px;padding:3px 9px;border-radius:999px;background:#EEF1F6;color:var(--muted)}"
    ".t-high .tier{background:#FDECEA;color:var(--high)}"
    ".t-medium .tier{background:#FFF4E0;color:var(--medium)}"
    ".t-low .tier{background:#E7F5EE;color:var(--low)}"
    ".card h2{font-size:19px;margin:4px 0 6px;font-weight:600}.card h2 .on{color:var(--muted);"
    "font-weight:400}code{font:600 16px ui-monospace,Consolas,monospace;background:#F1F3F7;"
    "padding:1px 6px;border-radius:5px}.why{margin:0 0 12px;color:#2B3A55}"
    ".meta{display:grid;grid-template-columns:1fr 180px;gap:16px;padding:10px 0;"
    "border-top:1px solid var(--line);border-bottom:1px solid var(--line);margin-bottom:14px}"
    ".meta small{display:block;color:var(--muted);font-size:12px}"
    ".bar{display:block;height:6px;background:#EEF1F6;border-radius:3px;margin-top:6px;"
    "overflow:hidden}.bar i{display:block;height:6px;background:var(--blue)}"
    "form{display:flex;gap:10px;flex-wrap:wrap}"
    "input[name=name]{flex:1;min-width:220px;padding:10px 12px;border:1px solid #C9CFDA;"
    "border-radius:8px;font:inherit}input[name=name]:focus{outline:2px solid var(--blue);"
    "border-color:transparent}button{border:0;border-radius:8px;padding:10px 20px;font:600 15px "
    "system-ui,sans-serif;color:#fff;cursor:pointer}.ok{background:var(--ok)}.no{background:var(--no)}"
    "button:hover{filter:brightness(1.1)}.id{margin:10px 0 0;color:var(--muted);font-size:12px}"
    ".empty{background:var(--card);border:1px dashed #C9CFDA;border-radius:12px;"
    "padding:36px;text-align:center;color:var(--muted)}.empty b{color:var(--ink);font-size:18px}"
    ".small{font-size:13px}"
)
