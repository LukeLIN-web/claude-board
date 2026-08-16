"""Password gate for the board, so it can be published through a tunnel that
provides no authentication of its own (e.g. a `cloudflared` quick tunnel).

WHY THIS EXISTS AT THE APP LAYER: `POST /api/windows/<pid>/keys` types arbitrary
keystrokes into a tmux pane, so an unauthenticated public URL is a public shell
on this host. The ngrok setup (scripts/tunnel.sh) buys that guarantee at the
edge, but a `*.trycloudflare.com` quick tunnel has no edge policy to buy it
with — the gate has to live in the process being tunnelled.

THE TRAP THIS IS BUILT AROUND: cloudflared connects to 127.0.0.1, so every
request arriving through the tunnel looks like it came from loopback. There is
therefore **no local bypass anywhere in this file** — "trust local requests"
would hand the public internet a skeleton key while looking perfectly sane. The
person sitting at this machine logs in like everyone else.

`install(app)` is the whole public surface: it registers the login routes and
wraps the app in the middleware, deriving the exempt set from the routes it just
registered. Nothing else needs to know which paths are public, and there is no
hand-maintained list to drift out of sync with them.

Config (from .env.local, which run.sh sources into the environment):
  FLEET_AUTH_PASSWORD  enables the gate; unset ⇒ gate off (loopback-only use)
  FLEET_API_TOKEN      optional bearer token so scripts can call /api/* too

Deliberately dependency-free: stdlib hmac/hashlib only, no session store, no
python-multipart. The cookie carries its own expiry and signature, so a restart
does not log anyone out and there is nothing to persist.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import os
import time
from string import Template
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE_NAME = "fleet_auth"
# Long-lived on purpose: this is read on a phone, and a gate that demands a
# password every morning gets turned off by its owner.
COOKIE_MAX_AGE = 30 * 24 * 3600

# Brute-force budget per client, and the pause added to every failed attempt.
# There is intentionally no *global* lockout: the URL is public, so a global
# counter would let anyone who finds it lock the owner out permanently. Per-key
# lockout plus a one-second penalty caps a spoofing attacker at roughly one
# guess per second per connection, which a long password outruns comfortably.
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60
FAILURE_DELAY = 1.0

# Above this many tracked clients, a failed attempt also sweeps expired entries.
# The ledger is keyed by a spoofable header, so without this a scanner rotating
# `X-Forwarded-For` grows the dict once per guess and never gives the memory
# back — pruning on read only ever reclaims the key being read.
FAILURE_SWEEP_AT = 1024


class Gate:
    """Holds the secret material and the failure ledger. One instance per
    process; `from_env()` builds the one the app uses."""

    def __init__(self, password: str = "", api_token: str = "") -> None:
        self.password = password
        self.api_token = api_token
        self._failures: dict[str, list[float]] = {}

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Gate":
        e = os.environ if env is None else env
        return cls(
            password=(e.get("FLEET_AUTH_PASSWORD") or "").strip(),
            api_token=(e.get("FLEET_API_TOKEN") or "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    # ---------- cookie ----------

    def _signing_key(self) -> bytes:
        """Derived from the password rather than stored separately, so changing
        the password invalidates every outstanding cookie — one knob to turn
        when revoking access, instead of two that can drift apart.

        Recomputed per verification, which is fine *because this is a single
        HMAC* (~1 µs). If anyone ever "hardens" this into a real KDF (PBKDF2,
        scrypt, argon2), per-request cost jumps by five orders of magnitude and
        this must become a cached value — the derivation is deliberately not a
        password hash, since the password never leaves this process."""
        return hmac.new(b"claude-fleet-cookie-v1",
                        self.password.encode(), hashlib.sha256).digest()

    def issue_cookie(self, now: float | None = None) -> str:
        expiry = int((now if now is not None else time.time()) + COOKIE_MAX_AGE)
        return f"v1.{expiry}.{self._sign(expiry)}"

    def _sign(self, expiry: int) -> str:
        return hmac.new(self._signing_key(), str(expiry).encode(),
                        hashlib.sha256).hexdigest()

    def cookie_valid(self, raw: str | None, now: float | None = None) -> bool:
        if not raw:
            return False
        parts = raw.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        try:
            expiry = int(parts[1])
        except ValueError:
            return False
        # Signature first, expiry second: an unsigned value should never get as
        # far as being reasoned about.
        if not hmac.compare_digest(self._sign(expiry), parts[2]):
            return False
        return expiry > (now if now is not None else time.time())

    # ---------- decisions ----------

    def password_ok(self, attempt: str) -> bool:
        return bool(self.password) and hmac.compare_digest(attempt or "", self.password)

    def token_ok(self, header: str | None) -> bool:
        """Bearer token for scripts. This is the thing the ngrok+OAuth setup
        cannot do: there, curl just gets a 302 to Google."""
        if not self.api_token or not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(value.strip(), self.api_token)

    def authorized(self, request: Request) -> bool:
        if not self.enabled:
            return True
        if self.cookie_valid(request.cookies.get(COOKIE_NAME)):
            return True
        return self.token_ok(request.headers.get("authorization"))

    # ---------- throttle ----------

    def client_key(self, request: Request) -> str:
        """Identify the caller for rate-limiting, most trustworthy header first.
        Behind cloudflared the socket is always loopback, so forwarded headers
        are the only signal there is. They are spoofable — but this ledger only
        ever *restricts*, so forging one costs an attacker their own bucket and
        gains them nothing beyond the per-attempt delay."""
        for header in ("cf-connecting-ip", "x-forwarded-for"):
            value = request.headers.get(header)
            if value:
                return value.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def throttled(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        recent = [t for t in self._failures.get(key, []) if now - t < FAILURE_WINDOW]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return len(recent) >= MAX_FAILURES

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if len(self._failures) > FAILURE_SWEEP_AT:
            # Amortized O(n), and only on the failure path — which is already
            # paying FAILURE_DELAY, so it costs nothing anyone can feel.
            self._failures = {k: v for k, v in self._failures.items()
                              if v and now - v[-1] < FAILURE_WINDOW}
        self._failures.setdefault(key, []).append(now)

    def clear_failures(self, key: str) -> None:
        self._failures.pop(key, None)

    # ---------- responses ----------

    def challenge(self, request: Request) -> Response:
        """What an unauthenticated request gets. API callers need a status code
        they can branch on; a browser needs to land somewhere it can type."""
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "authentication required"}, status_code=401)
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        return RedirectResponse(login_url(path), status_code=303)


def safe_next(value: str | None) -> bool:
    """Whether a post-login redirect target stays on this board. `//evil.example`
    is a protocol-relative URL, not a local path, which is the classic way an
    open redirect sneaks through a startswith("/") check."""
    return bool(value) and value.startswith("/") and not value.startswith("//")


def next_or_root(value: str | None) -> str:
    """The single place an untrusted `next` becomes a usable destination."""
    return value if safe_next(value) else "/"


def login_url(next_value: str | None) -> str:
    dest = next_or_root(next_value)
    return "/login" if dest == "/" else "/login?" + urlencode({"next": dest})


def is_secure_request(request: Request) -> bool:
    """Whether to mark the cookie Secure. Through the tunnel the browser speaks
    HTTPS but uvicorn is handed plain HTTP, so the forwarded header is what
    tells us. Marking the cookie Secure unconditionally would break login over
    plain http://127.0.0.1 for whoever is sitting at this machine."""
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    return proto == "https" or request.url.scheme == "https"


def set_session_cookie(response: Response, gate: Gate, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        gate.issue_cookie(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request),
        path="/",
    )


class AuthMiddleware:
    """Pure ASGI, wrapping the entire app — not Starlette's BaseHTTPMiddleware.

    Two reasons. BaseHTTPMiddleware sits badly under `/api/events`: it wraps
    streaming responses in a way that has a long history of breaking SSE
    disconnect handling, and that endpoint is the board's heartbeat. And a
    per-route dependency would have to be remembered on every future route,
    where forgetting once publishes a shell — this way a new route is behind
    the gate by construction.

    `exempt` is exact-matched, never prefix-matched: a prefix test would open
    any future route whose path merely starts with a public one. `install()`
    derives it from the login routes, so it cannot name a path that no longer
    exists (or miss one that does).
    """

    def __init__(self, app, gate: Gate, exempt=frozenset()) -> None:
        self.app = app
        self.gate = gate
        self.exempt = frozenset(exempt)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.gate.enabled:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.url.path in self.exempt or self.gate.authorized(request):
            await self.app(scope, receive, send)
            return
        await self.gate.challenge(request)(scope, receive, send)


LOGIN_PAGE = Template("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Fleet</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#0f172a; color:#e2e8f0;
         font:16px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif; }
  form { width:min(22rem,90vw); background:#1e293b; padding:1.75rem;
         border-radius:.75rem; box-shadow:0 10px 30px rgba(0,0,0,.4); }
  h1 { margin:0 0 1.25rem; font-size:1.125rem; font-weight:700; }
  input { width:100%; box-sizing:border-box; padding:.6rem .7rem; font-size:1rem;
          border:1px solid #475569; border-radius:.4rem; background:#0f172a;
          color:inherit; }
  button { width:100%; margin-top:.9rem; padding:.6rem; font-size:1rem;
           font-weight:600; border:0; border-radius:.4rem; background:#2563eb;
           color:#fff; cursor:pointer; }
  p.err { margin:.9rem 0 0; color:#fca5a5; font-size:.875rem; }
</style></head>
<body>
  <form method="post" action="$action">
    <h1>Claude Fleet</h1>
    <input type="password" name="password" autocomplete="current-password"
           autofocus placeholder="Password" aria-label="Password">
    <button type="submit">Sign in</button>
    $error
  </form>
</body></html>
""")


def login_response(next_value: str = "", error: str = "", status: int = 200) -> HTMLResponse:
    """The login page. `Template` rather than `str.format` so the stylesheet's
    braces stay as written — doubling every one of them to survive formatting is
    a standing invitation to break the only page that can let anyone back in."""
    body = LOGIN_PAGE.substitute(
        action=login_url(next_value),
        error=f'<p class="err">{html.escape(error)}</p>' if error else "",
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def build_router(gate: Gate) -> APIRouter:
    """The public routes. They live here, next to the middleware that exempts
    them, so the two cannot disagree about which paths are open."""
    router = APIRouter()

    @router.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> Response:
        # Nothing to log into when the gate is off, and no point showing the
        # form to someone who already holds a valid cookie.
        if not gate.enabled or gate.authorized(request):
            return RedirectResponse("/", status_code=303)
        return login_response(request.query_params.get("next", ""))

    @router.post("/login")
    async def login_submit(request: Request) -> Response:
        if not gate.enabled:
            return RedirectResponse("/", status_code=303)
        nxt = request.query_params.get("next", "")

        key = gate.client_key(request)
        if gate.throttled(key):
            return login_response(nxt, "Too many attempts. Try again later.", status=429)

        # Parsed by hand rather than with fastapi.Form: that pulls in
        # python-multipart, and a login page that needs a new runtime dependency
        # to render is a login page that breaks on a fresh checkout.
        body = (await request.body()).decode("utf-8", "replace")
        submitted = parse_qs(body).get("password", [""])[0]

        if not gate.password_ok(submitted):
            gate.record_failure(key)
            # Costs a legitimate typo one second and an attacker their throughput.
            await asyncio.sleep(FAILURE_DELAY)
            return login_response(nxt, "Wrong password.", status=401)

        gate.clear_failures(key)
        resp = RedirectResponse(next_or_root(nxt), status_code=303)
        set_session_cookie(resp, gate, request)
        return resp

    @router.get("/logout")
    def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    return router


def install(app, gate: Gate | None = None) -> Gate:
    """Register the login routes and put the whole app behind the gate.

    The exempt set is read back off the router rather than written down, so
    renaming or adding a public route updates the middleware by construction —
    the alternative is a hand-kept list whose failure modes are a permanent
    redirect loop (missed rename) or a silently open path (missed removal)."""
    gate = gate if gate is not None else Gate.from_env()
    router = build_router(gate)
    app.include_router(router)
    app.add_middleware(AuthMiddleware, gate=gate,
                       exempt=frozenset(r.path for r in router.routes))
    return gate
