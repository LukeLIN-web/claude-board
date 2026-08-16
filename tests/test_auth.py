"""Tests for the password gate.

The point of most of these is not that the happy path works — it's that the
ways a gate silently stops being a gate are pinned down: a cookie that survives
a password change, a `next=` that leaves the origin, a loopback exemption
sneaking back in (cloudflared makes every tunnelled request look local), and
routes added later escaping the middleware.

httpx/TestClient is not a project dependency, so the middleware is driven
through the raw ASGI interface instead.
"""
import asyncio
import unittest

from starlette.requests import Request

from core import auth


def make_request(path="/", headers=None, client=("127.0.0.1", 5000), scheme="http"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": scheme,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "headers": raw, "client": client, "server": ("127.0.0.1", 7879),
    })


def call_middleware(gate, path="/", headers=None, client=("127.0.0.1", 5000),
                    exempt=frozenset({"/login", "/logout"})):
    """Run one request through AuthMiddleware. Returns (status, reached_app)."""
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    mw = auth.AuthMiddleware(downstream, gate, exempt)
    req = make_request(path, headers, client)
    asyncio.run(mw(dict(req.scope), receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, bool(reached)


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.gate = auth.Gate(password="hunter2")

    def test_issued_cookie_validates(self):
        self.assertTrue(self.gate.cookie_valid(self.gate.issue_cookie()))

    def test_expired_cookie_rejected(self):
        old = self.gate.issue_cookie(now=0)
        self.assertFalse(self.gate.cookie_valid(old, now=auth.COOKIE_MAX_AGE + 1))

    def test_tampered_expiry_rejected(self):
        """Pushing the expiry out has to invalidate the signature, or the cookie
        is a bearer token with a user-chosen lifetime."""
        raw = self.gate.issue_cookie()
        _, expiry, sig = raw.split(".")
        forged = f"v1.{int(expiry) + 10**6}.{sig}"
        self.assertFalse(self.gate.cookie_valid(forged))

    def test_password_change_invalidates_existing_cookies(self):
        """Revoking access must take one action, not two. The signing key is
        derived from the password precisely so this holds."""
        issued = self.gate.issue_cookie()
        self.assertFalse(auth.Gate(password="different").cookie_valid(issued))

    def test_garbage_rejected(self):
        for bad in ["", None, "nonsense", "v1.abc.def", "v2." + "0" * 10 + ".x", "a.b"]:
            self.assertFalse(self.gate.cookie_valid(bad), bad)


class TokenTests(unittest.TestCase):
    def test_bearer_token_authorizes_api_calls(self):
        gate = auth.Gate(password="pw", api_token="tok123")
        self.assertTrue(gate.token_ok("Bearer tok123"))
        self.assertTrue(gate.token_ok("bearer tok123"))

    def test_wrong_or_malformed_token_rejected(self):
        gate = auth.Gate(password="pw", api_token="tok123")
        for bad in ["Bearer nope", "tok123", "Basic tok123", "", None]:
            self.assertFalse(gate.token_ok(bad), bad)

    def test_unset_token_never_authorizes(self):
        """An empty configured token must not mean 'Bearer ' opens the door."""
        gate = auth.Gate(password="pw", api_token="")
        for probe in ["Bearer ", "Bearer", "Bearer x"]:
            self.assertFalse(gate.token_ok(probe), probe)


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.gate = auth.Gate(password="hunter2", api_token="tok123")

    def test_disabled_gate_is_a_passthrough(self):
        status, reached = call_middleware(auth.Gate(), "/api/windows")
        self.assertEqual(status, 200)
        self.assertTrue(reached)

    def test_anonymous_page_request_redirects_to_login(self):
        status, reached = call_middleware(self.gate, "/")
        self.assertEqual(status, 303)
        self.assertFalse(reached)

    def test_anonymous_api_request_gets_401(self):
        status, reached = call_middleware(self.gate, "/api/windows")
        self.assertEqual(status, 401)
        self.assertFalse(reached)

    def test_loopback_client_is_not_trusted(self):
        """The one that matters. cloudflared connects to 127.0.0.1, so every
        request through the tunnel arrives with a loopback client address — any
        local exemption is a public bypass."""
        for client in [("127.0.0.1", 5000), ("::1", 5000), ("localhost", 5000)]:
            status, reached = call_middleware(self.gate, "/api/windows/1/keys", client=client)
            self.assertEqual(status, 401, client)
            self.assertFalse(reached, client)

    def test_forwarded_headers_do_not_authorize(self):
        """Headers are attacker-controlled; none of them may stand in for a
        credential."""
        for hdrs in [{"x-forwarded-for": "127.0.0.1"},
                     {"cf-connecting-ip": "127.0.0.1"},
                     {"x-forwarded-proto": "https"},
                     {"host": "localhost"}]:
            status, _ = call_middleware(self.gate, "/api/windows", headers=hdrs)
            self.assertEqual(status, 401, hdrs)

    def test_valid_cookie_passes_through(self):
        cookie = f"{auth.COOKIE_NAME}={self.gate.issue_cookie()}"
        status, reached = call_middleware(self.gate, "/", headers={"cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(reached)

    def test_bearer_token_passes_through(self):
        status, reached = call_middleware(
            self.gate, "/api/windows", headers={"authorization": "Bearer tok123"})
        self.assertEqual(status, 200)
        self.assertTrue(reached)

    def test_login_is_exempt_but_nothing_that_merely_starts_with_it(self):
        status, _ = call_middleware(self.gate, "/login")
        self.assertEqual(status, 200)
        # Exempt paths are exact-matched; a prefix match here would expose any
        # future route whose path happens to begin with "/login".
        status, reached = call_middleware(self.gate, "/login/../api/windows")
        self.assertEqual(status, 303)
        self.assertFalse(reached)

    def test_sse_endpoint_is_gated(self):
        status, reached = call_middleware(self.gate, "/api/events")
        self.assertEqual(status, 401)
        self.assertFalse(reached)


class RedirectTargetTests(unittest.TestCase):
    def test_offsite_next_values_rejected(self):
        for bad in ["//evil.example", "https://evil.example", "http://evil.example",
                    "evil.example", "", None]:
            self.assertFalse(auth.safe_next(bad), bad)

    def test_local_paths_accepted(self):
        for good in ["/", "/api/windows", "/?x=1#frag"]:
            self.assertTrue(auth.safe_next(good), good)

    def test_login_url_drops_unsafe_next(self):
        self.assertEqual(auth.login_url("//evil.example"), "/login")
        self.assertEqual(auth.login_url("/"), "/login")
        self.assertIn("next=%2Fapi%2Fwindows", auth.login_url("/api/windows"))

    def test_next_or_root_is_the_single_normalizer(self):
        self.assertEqual(auth.next_or_root("/api/windows"), "/api/windows")
        self.assertEqual(auth.next_or_root("//evil.example"), "/")
        self.assertEqual(auth.next_or_root(""), "/")

    def test_challenge_preserves_the_query_string(self):
        req = make_request("/?tab=history")
        resp = auth.Gate(password="pw").challenge(req)
        self.assertIn("tab%3Dhistory", resp.headers["location"])


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.gate = auth.Gate(password="pw")

    def test_locks_out_after_the_budget(self):
        for _ in range(auth.MAX_FAILURES):
            self.assertFalse(self.gate.throttled("1.2.3.4"))
            self.gate.record_failure("1.2.3.4")
        self.assertTrue(self.gate.throttled("1.2.3.4"))

    def test_lockout_is_per_client(self):
        for _ in range(auth.MAX_FAILURES):
            self.gate.record_failure("1.2.3.4")
        self.assertFalse(self.gate.throttled("5.6.7.8"))

    def test_failures_age_out(self):
        for _ in range(auth.MAX_FAILURES):
            self.gate.record_failure("1.2.3.4", now=0)
        self.assertFalse(self.gate.throttled("1.2.3.4", now=auth.FAILURE_WINDOW + 1))

    def test_success_clears_the_ledger(self):
        for _ in range(auth.MAX_FAILURES):
            self.gate.record_failure("1.2.3.4")
        self.gate.clear_failures("1.2.3.4")
        self.assertFalse(self.gate.throttled("1.2.3.4"))

    def test_client_key_prefers_cloudflare_header(self):
        req = make_request("/", {"cf-connecting-ip": "9.9.9.9",
                                 "x-forwarded-for": "8.8.8.8, 1.1.1.1"})
        self.assertEqual(self.gate.client_key(req), "9.9.9.9")

    def test_client_key_falls_back_through_forwarded_then_socket(self):
        self.assertEqual(
            self.gate.client_key(make_request("/", {"x-forwarded-for": "8.8.8.8, 1.1.1.1"})),
            "8.8.8.8")
        self.assertEqual(self.gate.client_key(make_request("/")), "127.0.0.1")


class CookieFlagTests(unittest.TestCase):
    def _cookie_header(self, request):
        from starlette.responses import RedirectResponse
        resp = RedirectResponse("/", status_code=303)
        auth.set_session_cookie(resp, auth.Gate(password="pw"), request)
        return resp.headers["set-cookie"]

    def test_cookie_flags(self):
        header = self._cookie_header(make_request("/", {"x-forwarded-proto": "https"}))
        self.assertIn("HttpOnly", header)
        self.assertIn("Secure", header)
        self.assertIn("SameSite=lax", header)
        self.assertNotIn("Secure", self._cookie_header(make_request("/")))

    def test_secure_only_when_the_browser_spoke_https(self):
        """Through the tunnel uvicorn sees plain HTTP, so the forwarded header
        is the only signal. Marking it Secure unconditionally would break login
        over http://127.0.0.1 for whoever is at this machine."""
        self.assertTrue(auth.is_secure_request(
            make_request("/", {"x-forwarded-proto": "https"})))
        self.assertTrue(auth.is_secure_request(make_request("/", scheme="https")))
        self.assertFalse(auth.is_secure_request(make_request("/")))
        self.assertFalse(auth.is_secure_request(
            make_request("/", {"x-forwarded-proto": "http"})))


class InstallTests(unittest.TestCase):
    """`install()` is what keeps the public-path list honest. If the exempt set
    ever goes back to being hand-written, these are the failures that catch it:
    a renamed login route redirects to itself forever, and a removed one stays
    open."""

    def _install(self):
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/api/windows")
        def _windows():
            return {}

        auth.install(app, auth.Gate(password="pw"))
        return app

    def _middleware(self, app):
        for mw in app.user_middleware:
            if mw.cls is auth.AuthMiddleware:
                return mw
        self.fail("AuthMiddleware was not installed")

    def test_exempt_set_is_exactly_the_routes_install_registers(self):
        """The derivation itself. FastAPI (0.137) resolves included routers at
        request time and never flattens them into app.routes, so the assertion
        is against the router install actually builds — equality, not
        membership, since a hand-maintained list can promise membership."""
        exempt = self._middleware(self._install()).kwargs["exempt"]
        registered = {r.path for r in auth.build_router(auth.Gate(password="pw")).routes}
        self.assertEqual(set(exempt), registered)

    def test_only_the_login_routes_are_exempt(self):
        exempt = self._middleware(self._install()).kwargs["exempt"]
        self.assertEqual(set(exempt), {"/login", "/logout"})

    def test_install_returns_a_gate_built_from_the_environment(self):
        from fastapi import FastAPI
        gate = auth.install(FastAPI())
        self.assertIsInstance(gate, auth.Gate)

    def test_installed_app_serves_login_and_gates_everything_else(self):
        """End to end through the real app, because the two halves can each look
        right in isolation: the login route has to be reachable *and* the route
        next to it has to be shut. Also proves the routes are mounted at all —
        install() would otherwise be free to register nothing."""
        app = self._install()
        self.assertEqual(self._request(app, "/login"), 200)
        self.assertEqual(self._request(app, "/logout"), 303)
        self.assertEqual(self._request(app, "/api/windows"), 401)

    def _request(self, app, path):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "root_path": "",
            "app": app,
        }
        asyncio.run(app(scope, receive, send))
        return next(m["status"] for m in sent if m["type"] == "http.response.start")


class GateConfigTests(unittest.TestCase):
    def test_no_password_means_disabled(self):
        self.assertFalse(auth.Gate.from_env({}).enabled)
        self.assertFalse(auth.Gate.from_env({"FLEET_AUTH_PASSWORD": "   "}).enabled)

    def test_password_enables_and_is_stripped(self):
        gate = auth.Gate.from_env({"FLEET_AUTH_PASSWORD": " pw \n"})
        self.assertTrue(gate.enabled)
        self.assertTrue(gate.password_ok("pw"))

    def test_disabled_gate_authorizes_nothing_by_password(self):
        """An empty password must not make the empty string a valid login."""
        self.assertFalse(auth.Gate().password_ok(""))


if __name__ == "__main__":
    unittest.main()
